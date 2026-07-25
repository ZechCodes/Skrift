"""OAuth2 Authorization Server controller.

Provides ``/oauth/authorize``, ``/oauth/token``, ``/oauth/userinfo``,
``/oauth/revoke``, and ``/oauth/introspect`` endpoints so a Skrift
instance can act as an identity hub for spoke sites.
"""

import base64
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID
from urllib.parse import urlencode, urlsplit

from litestar import Controller, Request, get, post
from litestar.exceptions import NotFoundException
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.client_secret import verify_client_secret
from skrift.auth.jwt_tokens import create_access_token_jwt, verify_access_token_jwt
from skrift.auth.scopes import SCOPE_DEFINITIONS
from skrift.auth.session_keys import SESSION_USER_EMAIL, SESSION_USER_ID, SESSION_USER_NAME, SESSION_USER_PICTURE_URL
from skrift.auth.tokens import create_signed_token, verify_signed_token
from skrift.config import get_settings
from skrift.db.models.user import User
from skrift.db.services import (
    oauth2_consent_service,
    oauth2_service,
    oauth2_signing_key_service,
    oauth_service,
)
from skrift.forms import verify_csrf
from skrift.lib.client_ip import get_client_ip
from skrift.middleware.security import add_form_action_source, apply_csp_nonce, csp_nonce_var

# Token lifetimes
AUTH_CODE_TTL = 600        # 10 minutes
ACCESS_TOKEN_TTL = 900     # 15 minutes
REFRESH_TOKEN_TTL = 2592000  # 30 days

# Longest client_name accepted at Dynamic Client Registration. client_name is
# attacker-controlled and echoed on the consent screen, so it is length-capped
# here and rendered through the templating engine's autoescape.
CLIENT_NAME_MAX_LENGTH = 255

# The only grant/response types this server supports for dynamically-registered
# (public, PKCE-only) clients. RFC 7591 §2: unsupported requested values are
# rejected rather than echoed back as if they were registered.
DYNAMIC_CLIENT_GRANT_TYPES = ("authorization_code", "refresh_token")
DYNAMIC_CLIENT_RESPONSE_TYPES = ("code",)

# Scopes a dynamic registration gets when it omits `scope`. An empty
# allowed_scopes list means "unrestricted" at /oauth/authorize, so omission
# must fall back to this minimal identity set — never to every registered
# scope.
DYNAMIC_CLIENT_DEFAULT_SCOPES = ("openid", "profile", "email")


def _json_error(error: str, description: str, status_code: int = 400) -> Response:
    """Return an OAuth2 JSON error response."""
    return Response(
        content={"error": error, "error_description": description},
        status_code=status_code,
        media_type="application/json",
    )


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify a PKCE code_verifier against the stored code_challenge (S256)."""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return computed == code_challenge


def _resolve_issuer(settings, request: Request) -> str:
    """The token issuer: configured value, or this request's base URL."""
    return settings.oauth2_issuer or str(request.base_url).rstrip("/")


def _extract_resource_values(params) -> list[str]:
    """Collect RFC 8707 ``resource`` values from query or form parameters.

    Litestar multi-dicts expose ``getall`` (needed to detect the invalid
    multiple-``resource`` case); plain mappings fall back to the single value.
    """
    getall = getattr(params, "getall", None)
    if getall is None:
        value = params.get("resource", "")
        return [value] if value else []
    return [value for value in getall("resource", []) if value]


def _extract_granted_scopes(form_data) -> list[str]:
    """Collect the scopes a user left checked on the consent form.

    Browsers submit one ``scope`` field per checked checkbox — and nothing at
    all when every box is cleared — so this reads every value. A single
    space-delimited value is accepted too, matching how ``scope`` travels
    everywhere else in OAuth2. Duplicates and ordering are left to the caller,
    which intersects the result with the requested scopes.
    """
    getall = getattr(form_data, "getall", None)
    values = getall("scope", []) if getall is not None else [form_data.get("scope", "")]
    return [scope for value in values for scope in value.split()]


def _consent_denied_redirect(redirect_uri: str, state: str) -> Redirect:
    """Send the user-agent back to the client with an ``access_denied`` error."""
    sep = "&" if "?" in redirect_uri else "?"
    return Redirect(path=f"{redirect_uri}{sep}" + urlencode({"error": "access_denied", "state": state}))


def _validate_resource_indicator(resource: str, allowed_resources: list[str]) -> bool:
    """Validate an RFC 8707 resource indicator.

    Must be an absolute URI without a fragment and — when an allowlist is
    configured — one of the allowed resources.
    """
    parts = urlsplit(resource)
    if not parts.scheme or parts.fragment:
        return False
    if allowed_resources and resource not in allowed_resources:
        return False
    return True


def _is_loopback_host(host: str) -> bool:
    """Loopback hosts that may use plain http in a redirect_uri (RFC 8252)."""
    return host == "127.0.0.1" or host == "localhost"


# A registered host may only contain characters that are unambiguously part of
# a DNS name or dotted-quad IP. This deliberately excludes spaces, ';', '*',
# '@', and every other character that could smuggle extra tokens or directives
# into the consent-page ``form-action`` CSP allowlist (see _consent_csp_headers)
# or confuse a URL parser.
_REDIRECT_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


def _redirect_uri_origin(uri: str) -> str | None:
    """Return the safe ``scheme://host[:port]`` origin of a redirect_uri.

    Returns ``None`` when the URI cannot be reduced to a trustworthy origin —
    the single choke point both the registration validator and the consent CSP
    builder rely on so neither can be fed an origin that injects CSP tokens.

    Rejected: URIs with any whitespace/control character (which URL parsers
    silently strip, letting a newline smuggle a second, unvalidated URI through
    the newline-delimited ``redirect_uris`` storage), embedded userinfo
    (``user:pass@host``), a host outside :data:`_REDIRECT_HOST_RE`, or an
    unparseable port.
    """
    if not uri or any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in uri):
        return None
    parts = urlsplit(uri)
    if not parts.scheme or not parts.netloc:
        return None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None
    host = parts.hostname
    if not host or not _REDIRECT_HOST_RE.match(host):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    origin = f"{parts.scheme}://{host}"
    if port is not None:
        origin = f"{origin}:{port}"
    return origin


def _validate_registration_redirect_uri(uri: str) -> bool:
    """Validate a redirect_uri submitted to Dynamic Client Registration.

    Hardening rules (audited): the URI must reduce to a safe origin (see
    :func:`_redirect_uri_origin` — absolute, no whitespace/control chars, no
    embedded credentials, strict host charset), carry no fragment, and use
    ``https`` — the sole exception being loopback ``http://127.0.0.1`` /
    ``http://localhost`` (with an optional port), which native/CLI clients rely
    on. Anything else is rejected.
    """
    if _redirect_uri_origin(uri) is None:
        return False
    parts = urlsplit(uri)
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return _is_loopback_host(parts.hostname or "")
    return False


async def verify_oauth_token(token: str, secret: str, db_session: AsyncSession) -> dict | None:
    """Verify a signed token and check revocation status.

    Returns the payload dict if valid and not revoked, or None.
    """
    payload = verify_signed_token(token, secret)
    if payload is None:
        return None

    jti = payload.get("jti")
    if jti and await oauth2_service.is_token_revoked(db_session, jti):
        return None

    return payload


def _issue_authorization_code(
    request: Request,
    settings,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str,
    code_challenge: str,
    resource: str,
) -> Redirect:
    """Mint an authorization code for the logged-in user and redirect back.

    Shared by the consent-approval path and the remembered-consent skip path
    so both bind the same PKCE challenge, resource, and session profile onto
    the code.
    """
    code_payload = {
        "type": "code",
        "user_id": request.session.get(SESSION_USER_ID),
        "email": request.session.get(SESSION_USER_EMAIL, ""),
        "name": request.session.get(SESSION_USER_NAME, ""),
        "picture_url": request.session.get(SESSION_USER_PICTURE_URL, ""),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "resource": resource,
    }
    code = create_signed_token(code_payload, settings.secret_key, AUTH_CODE_TTL)
    sep = "&" if "?" in redirect_uri else "?"
    callback_url = f"{redirect_uri}{sep}" + urlencode({"code": code, "state": state})
    return Redirect(path=callback_url)


class OAuth2Controller(Controller):
    path = "/oauth"

    @get("/authorize")
    async def authorize_get(self, request: Request, db_session: AsyncSession) -> TemplateResponse | Redirect | Response:
        """Authorization endpoint — show consent screen or redirect to login."""
        params = request.query_params
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        response_type = params.get("response_type", "")
        state = params.get("state", "")
        scope = params.get("scope", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")

        # Validate response_type
        if response_type != "code":
            return _json_error("unsupported_response_type", "Only response_type=code is supported")

        # Validate client
        client = await oauth2_service.get_client_by_client_id(db_session, client_id)
        if not client:
            return _json_error("invalid_request", "Unknown client_id")

        # Validate redirect_uri
        if redirect_uri not in client.redirect_uri_list:
            return _json_error("invalid_request", "redirect_uri not registered for this client")

        # PKCE is required for every client (OAuth 2.1). S256 is the only
        # accepted method — `plain` is explicitly rejected.
        if not code_challenge:
            return _json_error("invalid_request", "code_challenge is required")
        if code_challenge_method != "S256":
            return _json_error("invalid_request", "Only code_challenge_method=S256 is supported")

        # Validate requested scopes
        requested_scopes = scope.split() if scope else []
        allowed = client.allowed_scope_list
        for s in requested_scopes:
            if s not in SCOPE_DEFINITIONS:
                return _json_error("invalid_scope", f"Unknown scope: {s}")
            if allowed and s not in allowed:
                return _json_error("invalid_scope", f"Scope not allowed for this client: {s}")

        # RFC 8707 resource indicator — optional, at most one, and must be a
        # valid (allowlisted) target so the access token's `aud` is trustworthy.
        resource_values = _extract_resource_values(params)
        if len(resource_values) > 1:
            return _json_error("invalid_target", "Multiple resource parameters are not supported")
        resource = resource_values[0] if resource_values else ""
        if resource and not _validate_resource_indicator(resource, get_settings().oauth2_allowed_resources):
            return _json_error("invalid_target", "Invalid or disallowed resource")

        # Check if user is logged in
        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            # Preserve the full authorize URL so we can return after login
            query = urlencode({
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": response_type,
                "state": state,
                "scope": scope,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "resource": resource,
            })
            next_url = f"/oauth/authorize?{query}"
            # next_url carries its own query string; it must be encoded as a
            # single value or its `&`-separated params (response_type, ...) leak
            # out of `next` and are lost across the login round-trip.
            return Redirect(path=f"/auth/login?{urlencode({'next': next_url})}")

        # Remembered consent: if a prior grant for this (user, client) already
        # covers every requested scope, skip the consent screen and issue the
        # code directly — still binding PKCE and the resource onto it. A request
        # for scopes beyond the grant falls through to the consent screen.
        grant = await oauth2_consent_service.find_grant(db_session, user_id, client_id)
        if grant is not None and set(requested_scopes).issubset(set(grant.scope_list)):
            return _issue_authorization_code(
                request,
                get_settings(),
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                scope=scope,
                code_challenge=code_challenge,
                resource=resource,
            )

        # Store params in session for POST consent
        request.session["oauth_authorize"] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
            "code_challenge": code_challenge,
            "resource": resource,
        }

        # Build scope descriptions for the consent screen
        scope_descriptions = []
        for s in requested_scopes:
            defn = SCOPE_DEFINITIONS.get(s)
            if defn:
                scope_descriptions.append(
                    {"name": s, "description": defn.description, "required": defn.required}
                )
            else:
                scope_descriptions.append({"name": s, "description": s, "required": False})

        return TemplateResponse(
            "oauth/authorize.html",
            context={
                "client_id": client_id,
                "display_name": client.display_name,
                "scopes": requested_scopes,
                "scope_descriptions": scope_descriptions,
                "request": request,
            },
            headers=self._consent_csp_headers(redirect_uri),
        )

    @staticmethod
    def _consent_csp_headers(redirect_uri: str) -> dict[str, str]:
        """Build a per-request CSP that permits the post-consent redirect.

        Browsers enforce ``form-action`` against a form submission's redirect
        destination, not just the form's ``action`` attribute. Since consent
        approval redirects the user-agent to the client's ``redirect_uri`` —
        which is on an arbitrary registered origin — the global
        ``form-action 'self'`` blocks the final navigation.

        ``redirect_uri`` has already been validated against the client's
        registered allowlist by the caller, so appending its origin introduces
        no open-redirect risk: only registered origins can ever appear here. The
        deployment-configured CSP (including any extra ``form-action`` sources)
        is preserved; this only widens ``form-action`` for this one response.

        Returns an empty dict when CSP is disabled, leaving the default headers
        from :class:`SecurityHeadersMiddleware` in place.
        """
        security = get_settings().security_headers
        if not security.enabled or not security.content_security_policy:
            return {}

        # Build the form-action source from a sanitized origin, never the raw
        # redirect_uri. A host containing a space, ';' or '*' would otherwise
        # inject extra CSP tokens/directives (e.g. widening form-action to '*').
        # Fail closed: if the origin cannot be trusted, don't widen the policy.
        origin = _redirect_uri_origin(redirect_uri)
        if origin is None:
            return {}
        csp = add_form_action_source(security.content_security_policy, origin)

        # The middleware skips its own CSP (and nonce injection) once a response
        # carries one, so re-apply the nonce here to keep nonced markup working.
        if security.csp_nonce:
            nonce = csp_nonce_var.get("")
            if nonce:
                csp = apply_csp_nonce(csp, nonce)

        return {"content-security-policy": csp}

    @post("/authorize")
    async def authorize_post(self, request: Request, db_session: AsyncSession) -> Redirect | Response:
        """Consent form submission — issue authorization code."""
        if not await verify_csrf(request):
            return _json_error("invalid_request", "Invalid CSRF token")

        form_data = await request.form()
        action = form_data.get("action", "")

        # Retrieve stored authorize params
        authorize_data = request.session.pop("oauth_authorize", None)
        if not authorize_data:
            return _json_error("invalid_request", "Authorization session expired")

        client_id = authorize_data["client_id"]
        redirect_uri = authorize_data["redirect_uri"]
        state = authorize_data["state"]
        scope = authorize_data.get("scope", "")
        code_challenge = authorize_data.get("code_challenge", "")
        resource = authorize_data.get("resource", "")
        requested_scopes = scope.split() if scope else []

        # User denied — the button wins over any still-checked checkbox the
        # browser submitted alongside it.
        if action == "deny":
            return _consent_denied_redirect(redirect_uri, state)

        # RFC 6749 §3.3: the user may approve a subset of the request, never a
        # superset. The stored authorization request is the ceiling, so a
        # submitted scope outside it is a tampered form — rejected outright
        # rather than quietly dropped, matching the refresh grant's handling of
        # an out-of-grant scope.
        granted_scopes = set(_extract_granted_scopes(form_data))
        if not granted_scopes.issubset(requested_scopes):
            return _json_error("invalid_scope", "Granted scope exceeds the requested scope")

        # Required scopes are not user-declinable: the consent screen renders
        # them locked, but a locked checkbox submits nothing and a tampered
        # form could strip the accompanying hidden field, so they are
        # re-asserted here. Only requested scopes are ever added — the request
        # stays the ceiling, so a required scope the client never asked for is
        # not smuggled into the grant.
        granted_scopes |= {
            requested_scope
            for requested_scope in requested_scopes
            if (definition := SCOPE_DEFINITIONS.get(requested_scope)) and definition.required
        }

        # Approving while keeping nothing grants nothing, which is the same
        # answer as denying — never an empty-scope token. A request that asked
        # for no scopes at all has nothing to decline and still approves.
        # Keeping only required scopes is an approval: declining the rest is
        # what the checkboxes are for, and refusing those too is the Deny
        # button's job.
        if requested_scopes and not granted_scopes:
            return _consent_denied_redirect(redirect_uri, state)

        # Keep the requested ordering so the granted scope string reads the way
        # the consent screen did.
        granted_scope = " ".join(
            requested_scope
            for requested_scope in requested_scopes
            if requested_scope in granted_scopes
        )
        declined_scopes = [
            requested_scope
            for requested_scope in requested_scopes
            if requested_scope not in granted_scopes
        ]

        # User approved — remember this consent, then issue the auth code.
        settings = get_settings()
        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            return _json_error("invalid_request", "User not logged in")

        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=user_id,
            client_id=client_id,
            scopes=granted_scope.split(),
            declined_scopes=declined_scopes,
            granted_at=datetime.now(tz=timezone.utc),
        )

        return _issue_authorization_code(
            request,
            settings,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scope=granted_scope,
            code_challenge=code_challenge,
            resource=resource,
        )

    @post("/token")
    async def token_exchange(self, request: Request, db_session: AsyncSession) -> Response:
        """Token endpoint — exchange auth code or refresh token for access token."""
        form_data = await request.form()
        grant_type = form_data.get("grant_type", "")
        issuer = _resolve_issuer(get_settings(), request)

        if grant_type == "authorization_code":
            return await self._handle_authorization_code(form_data, db_session, issuer)
        elif grant_type == "refresh_token":
            return await self._handle_refresh_token(form_data, db_session, issuer)
        else:
            return _json_error("unsupported_grant_type", f"Unsupported grant_type: {grant_type}")

    async def _handle_authorization_code(self, form_data, db_session: AsyncSession, issuer: str) -> Response:
        """Handle grant_type=authorization_code."""
        settings = get_settings()

        code = form_data.get("code", "")
        redirect_uri = form_data.get("redirect_uri", "")
        client_id = form_data.get("client_id", "")
        client_secret = form_data.get("client_secret", "")
        code_verifier = form_data.get("code_verifier", "")

        # Verify auth code — revocation-aware so replays of a consumed code fail.
        payload = await verify_oauth_token(code, settings.secret_key, db_session)
        if not payload or payload.get("type") != "code":
            return _json_error("invalid_grant", "Invalid or expired authorization code")

        # Validate client_id and redirect_uri match
        if payload["client_id"] != client_id:
            return _json_error("invalid_grant", "client_id mismatch")
        if payload["redirect_uri"] != redirect_uri:
            return _json_error("invalid_grant", "redirect_uri mismatch")

        # Look up client
        client = await oauth2_service.get_client_by_client_id(db_session, client_id)
        if not client:
            return _json_error("invalid_client", "Unknown client_id")

        # Confidential client: validate secret (constant-time)
        if client.client_secret:
            if not verify_client_secret(client_secret, client.client_secret):
                return _json_error("invalid_client", "Invalid client_secret")

        # PKCE validation: `code_challenge` is stamped on every code by
        # `authorize_get`, so the code-grant path always requires a
        # `code_verifier` that matches. Missing or mismatched verifiers are
        # `invalid_grant` errors.
        stored_challenge = payload.get("code_challenge", "")
        if not stored_challenge:
            return _json_error("invalid_grant", "code_challenge missing from token")
        if not code_verifier:
            return _json_error("invalid_grant", "code_verifier required")
        if not _verify_pkce(code_verifier, stored_challenge):
            return _json_error("invalid_grant", "PKCE verification failed")

        # RFC 8707: a token-endpoint `resource` may narrow the authorized
        # target (equal it, or restrict an unrestricted grant) but never widen.
        granted_resource = payload.get("resource", "")
        resource_values = _extract_resource_values(form_data)
        if len(resource_values) > 1:
            return _json_error("invalid_target", "Multiple resource parameters are not supported")
        requested_resource = resource_values[0] if resource_values else ""
        if requested_resource:
            if not _validate_resource_indicator(requested_resource, settings.oauth2_allowed_resources):
                return _json_error("invalid_target", "Invalid or disallowed resource")
            if granted_resource and requested_resource != granted_resource:
                return _json_error("invalid_target", "resource was not authorized by this grant")
        effective_resource = requested_resource or granted_resource

        # Revoke the auth code before issuing tokens so a concurrent replay fails.
        code_jti = payload.get("jti")
        if code_jti:
            code_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(db_session, code_jti, "code", code_exp)

        scope = payload.get("scope", "")

        # Stamp a fresh family id so later refresh rotations can detect reuse
        # (presenting a previously-rotated token) and mass-revoke the lineage.
        family_id = uuid.uuid4().hex

        # Issue tokens — the access token is an asymmetric JWT third parties
        # can verify via /oauth/jwks; the refresh token stays HMAC and carries
        # the resource so refreshed access tokens keep the same audience.
        signing_key = await oauth2_signing_key_service.get_or_create_active_key(db_session)
        access_token = create_access_token_jwt(
            subject=payload["user_id"],
            client_id=client_id,
            scope=scope,
            audience=effective_resource or None,
            issuer=issuer,
            signing_key=signing_key,
            expires_in=settings.oauth2_access_token_ttl,
        )
        refresh_payload = {
            "type": "refresh",
            "user_id": payload["user_id"],
            "client_id": client_id,
            "scope": scope,
            "family_id": family_id,
            "resource": effective_resource,
        }
        refresh_token = create_signed_token(refresh_payload, settings.secret_key, REFRESH_TOKEN_TTL)

        # Stamp liveness so the dynamic-client pruning job never retires a
        # client that has actually issued a token.
        await oauth2_service.mark_client_used(db_session, client, datetime.now(tz=timezone.utc))

        return Response(
            content={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": settings.oauth2_access_token_ttl,
                "scope": scope,
            },
            status_code=200,
            media_type="application/json",
        )

    async def _handle_refresh_token(self, form_data, db_session: AsyncSession, issuer: str) -> Response:
        """Handle grant_type=refresh_token with RFC 6749 §10.4 reuse detection.

        Three outcomes for a presented refresh token:

        1. **Reuse detected** — the token's ``jti`` is already revoked (i.e.
           it has been rotated away on a previous call). Treat this as the
           compromise indicator described in §10.4: add the whole ``family_id``
           to :class:`RevokedFamily` so any sibling refresh still in the wild
           also stops working, then return ``invalid_grant``.
        2. **Family already revoked** — reuse was detected on a prior call.
           Reject without touching state.
        3. **Normal rotation** — revoke this jti, issue new access + refresh
           tokens carrying the same ``family_id`` so the chain is still
           trackable.
        """
        settings = get_settings()

        refresh_token_str = form_data.get("refresh_token", "")
        client_id = form_data.get("client_id", "")
        client_secret = form_data.get("client_secret", "")

        # Signature-only verify (don't conflate "expired/bad" with "revoked").
        payload = verify_signed_token(refresh_token_str, settings.secret_key)
        if not payload or payload.get("type") != "refresh":
            return _json_error("invalid_grant", "Invalid or expired refresh token")

        if payload["client_id"] != client_id:
            return _json_error("invalid_grant", "client_id mismatch")

        # Look up client
        client = await oauth2_service.get_client_by_client_id(db_session, client_id)
        if not client:
            return _json_error("invalid_client", "Unknown client_id")

        # Confidential client: validate secret (constant-time)
        if client.client_secret:
            if not verify_client_secret(client_secret, client.client_secret):
                return _json_error("invalid_client", "Invalid client_secret")

        old_jti = payload.get("jti")
        family_id = payload.get("family_id", "")

        # Reuse detection: a presented refresh whose jti has already been
        # rotated away is the §10.4 compromise indicator. Kill the whole
        # family so sibling tokens stop working too.
        if old_jti and await oauth2_service.is_token_revoked(db_session, old_jti):
            if family_id:
                await oauth2_service.revoke_family(db_session, family_id)
            return _json_error(
                "invalid_grant",
                "Refresh token reuse detected; token family revoked",
            )

        # Family-level revocation check covers the race where reuse was
        # detected on a concurrent request.
        if family_id and await oauth2_service.is_family_revoked(db_session, family_id):
            return _json_error(
                "invalid_grant",
                "Refresh token family has been revoked",
            )

        original_scope = payload.get("scope", "")
        original_scope_set = set(original_scope.split())

        # Scope binding: an optional `scope` form parameter must be a subset
        # of the originally granted scope. Downgrades are allowed; anything
        # outside the original grant is an `invalid_scope` error.
        requested_scope = form_data.get("scope", "").strip()
        if requested_scope:
            requested_scope_set = set(requested_scope.split())
            if not requested_scope_set.issubset(original_scope_set):
                return _json_error(
                    "invalid_scope",
                    "Requested scope exceeds originally granted scope",
                )
            effective_scope = " ".join(sorted(requested_scope_set))
        else:
            effective_scope = original_scope

        # Resource binding mirrors the code grant: equal/narrow only.
        granted_resource = payload.get("resource", "")
        resource_values = _extract_resource_values(form_data)
        if len(resource_values) > 1:
            return _json_error("invalid_target", "Multiple resource parameters are not supported")
        requested_resource = resource_values[0] if resource_values else ""
        if requested_resource:
            if not _validate_resource_indicator(requested_resource, settings.oauth2_allowed_resources):
                return _json_error("invalid_target", "Invalid or disallowed resource")
            if granted_resource and requested_resource != granted_resource:
                return _json_error("invalid_target", "resource was not authorized by this grant")
        effective_resource = requested_resource or granted_resource

        # Revoke the old refresh token (normal rotation path).
        if old_jti:
            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(db_session, old_jti, "refresh", expires_at)

        # Issue new access + refresh tokens (token rotation); stay on the
        # same family so future rotations remain linkable for reuse checks.
        signing_key = await oauth2_signing_key_service.get_or_create_active_key(db_session)
        access_token = create_access_token_jwt(
            subject=payload["user_id"],
            client_id=client_id,
            scope=effective_scope,
            audience=effective_resource or None,
            issuer=issuer,
            signing_key=signing_key,
            expires_in=settings.oauth2_access_token_ttl,
        )
        refresh_payload = {
            "type": "refresh",
            "user_id": payload["user_id"],
            "client_id": client_id,
            "scope": effective_scope,
            "family_id": family_id,
            "resource": effective_resource,
        }
        new_refresh_token = create_signed_token(refresh_payload, settings.secret_key, REFRESH_TOKEN_TTL)

        # Stamp liveness so refreshed dynamic clients stay out of the pruner.
        await oauth2_service.mark_client_used(db_session, client, datetime.now(tz=timezone.utc))

        return Response(
            content={
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "token_type": "bearer",
                "expires_in": settings.oauth2_access_token_ttl,
                "scope": effective_scope,
            },
            status_code=200,
            media_type="application/json",
        )

    @post("/register")
    async def register(self, request: Request, db_session: AsyncSession) -> Response:
        """Dynamic Client Registration endpoint (RFC 7591).

        Machine clients (e.g. Claude custom connectors) self-register a public
        client here. The route is CSRF-exempt like ``/oauth/token`` — there is
        no browser session to protect — and 404s unless dynamic registration is
        explicitly enabled on top of ``oauth2_enabled``.
        """
        settings = get_settings()
        if not settings.oauth2_enabled or not settings.oauth2_dynamic_registration_enabled:
            raise NotFoundException()

        try:
            body = await request.json()
        except ValueError:
            return _json_error("invalid_client_metadata", "Request body must be valid JSON")

        if not isinstance(body, dict):
            return _json_error("invalid_client_metadata", "Request body must be a JSON object")

        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _json_error("invalid_client_metadata", "redirect_uris is required")
        if not all(isinstance(uri, str) for uri in redirect_uris):
            return _json_error("invalid_redirect_uri", "Each redirect_uri must be a string")
        for uri in redirect_uris:
            if not _validate_registration_redirect_uri(uri):
                return _json_error("invalid_redirect_uri", f"Invalid redirect_uri: {uri}")

        # Public DCR only: the sole accepted auth method is `none` (PKCE).
        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            return _json_error(
                "invalid_client_metadata", "token_endpoint_auth_method must be 'none'"
            )

        grant_types = body.get("grant_types") or list(DYNAMIC_CLIENT_GRANT_TYPES)
        if not isinstance(grant_types, list) or not all(
            isinstance(grant_type, str) for grant_type in grant_types
        ):
            return _json_error("invalid_client_metadata", "grant_types must be a list of strings")
        for grant_type in grant_types:
            if grant_type not in DYNAMIC_CLIENT_GRANT_TYPES:
                return _json_error("invalid_client_metadata", f"Unsupported grant_type: {grant_type}")

        response_types = body.get("response_types") or list(DYNAMIC_CLIENT_RESPONSE_TYPES)
        if not isinstance(response_types, list) or not all(
            isinstance(response_type, str) for response_type in response_types
        ):
            return _json_error("invalid_client_metadata", "response_types must be a list of strings")
        for response_type in response_types:
            if response_type not in DYNAMIC_CLIENT_RESPONSE_TYPES:
                return _json_error(
                    "invalid_client_metadata", f"Unsupported response_type: {response_type}"
                )

        client_name = body.get("client_name", "")
        if not isinstance(client_name, str):
            return _json_error("invalid_client_metadata", "client_name must be a string")
        display_name = client_name[:CLIENT_NAME_MAX_LENGTH].strip() or "Dynamic Client"

        scope = body.get("scope", "")
        if not isinstance(scope, str):
            return _json_error("invalid_client_metadata", "scope must be a string")
        requested_scopes = scope.split()
        for requested_scope in requested_scopes:
            if requested_scope not in SCOPE_DEFINITIONS:
                return _json_error("invalid_client_metadata", f"Unknown scope: {requested_scope}")
        if not requested_scopes:
            requested_scopes = [
                default_scope
                for default_scope in DYNAMIC_CLIENT_DEFAULT_SCOPES
                if default_scope in SCOPE_DEFINITIONS
            ]

        # Per-IP registration cap — counts this IP's recent dynamic clients and
        # rejects once it reaches the configured limit within the window.
        client_ip = get_client_ip(request.scope)
        now = datetime.now(tz=timezone.utc)
        window_start = now - timedelta(seconds=settings.oauth2_dynamic_registration_ip_window_seconds)
        recent = await oauth2_service.count_recent_dynamic_registrations(
            db_session, client_ip, window_start
        )
        if recent >= settings.oauth2_dynamic_registration_ip_limit:
            return _json_error(
                "too_many_requests",
                "Registration rate limit exceeded for this client",
                status_code=429,
            )

        # Global cap — the per-IP window only bounds the rate, so a distributed
        # attacker could otherwise grow oauth2_clients without limit.
        total_dynamic = await oauth2_service.count_dynamic_clients(db_session)
        if total_dynamic >= settings.oauth2_dynamic_registration_total_limit:
            return _json_error(
                "too_many_requests",
                "Dynamic client registration capacity reached",
                status_code=429,
            )

        client = await oauth2_service.create_dynamic_client(
            db_session,
            display_name=display_name,
            redirect_uris=redirect_uris,
            allowed_scopes=requested_scopes,
            registered_by_ip=client_ip,
            issued_at=now,
        )

        return Response(
            content={
                "client_id": client.client_id,
                "client_id_issued_at": int(now.timestamp()),
                "token_endpoint_auth_method": "none",
                "grant_types": grant_types,
                "response_types": response_types,
                "redirect_uris": client.redirect_uri_list,
                "client_name": display_name,
                "scope": " ".join(requested_scopes),
            },
            status_code=201,
            media_type="application/json",
        )

    @get("/jwks")
    async def jwks(self, db_session: AsyncSession) -> Response:
        """JWK Set endpoint — public keys for access-token verification."""
        await oauth2_signing_key_service.get_or_create_active_key(db_session)
        keys = await oauth2_signing_key_service.list_jwks_keys(db_session)
        return Response(
            content={"keys": keys},
            status_code=200,
            media_type="application/json",
        )

    async def _verify_jwt_access_claims(
        self, request: Request, db_session: AsyncSession, token: str
    ) -> dict | None:
        """Verify a JWT access token against the JWKS and the revocation list.

        Audience is intentionally not enforced here — userinfo and
        introspection serve tokens minted for any resource.
        """
        if token.count(".") != 2:
            return None

        settings = get_settings()
        jwks = await oauth2_signing_key_service.list_jwks_keys(db_session)
        if not jwks:
            return None

        claims = verify_access_token_jwt(
            token, jwks=jwks, issuer=_resolve_issuer(settings, request), audience=None
        )
        if claims is None:
            return None

        jti = claims.get("jti", "")
        if jti and await oauth2_service.is_token_revoked(db_session, jti):
            return None

        return claims

    @get("/userinfo")
    async def userinfo(self, request: Request, db_session: AsyncSession) -> Response:
        """UserInfo endpoint — return user data from a valid access token."""
        settings = get_settings()

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return _json_error("invalid_token", "Missing or invalid Bearer token", status_code=401)

        token = auth_header[7:]  # Strip "Bearer "
        payload = await verify_oauth_token(token, settings.secret_key, db_session)
        if payload is not None and payload.get("type") == "access":
            # Legacy HMAC access token — the profile travels in the payload.
            subject = payload["user_id"]
            scope_str = payload.get("scope", "")
            profile = {
                "email": payload.get("email", ""),
                "name": payload.get("name", ""),
                "picture": payload.get("picture_url", ""),
            }
        else:
            jwt_claims = await self._verify_jwt_access_claims(request, db_session, token)
            if jwt_claims is None:
                return _json_error("invalid_token", "Invalid or expired access token", status_code=401)
            # JWT access tokens carry no profile claims — read them from the
            # user record so userinfo stays authoritative.
            subject = jwt_claims["sub"]
            scope_str = jwt_claims.get("scope", "")
            user = await db_session.get(User, UUID(subject))
            profile = {
                "email": (user.email if user else "") or "",
                "name": (user.name if user else "") or "",
                "picture": (user.picture_url if user else "") or "",
            }

        # Build claims strictly from granted scopes. A token minted with
        # no scopes gets only `sub` — prior backwards-compat code returned
        # the full profile + email, which silently defeated scope filtering.
        granted_scopes = scope_str.split() if scope_str else []

        allowed_claims: set[str] = set()
        for s in granted_scopes:
            defn = SCOPE_DEFINITIONS.get(s)
            if defn:
                allowed_claims.update(defn.claims)

        # Always include sub — the minimum subject identifier required by OIDC.
        claims: dict = {"sub": subject}

        if "email" in allowed_claims:
            claims["email"] = profile["email"]
            # Report verification from this server's own records — never a
            # blanket true. ``email_verified`` is set only when the user has a
            # linked identity whose email was attested as verified, mirroring
            # the gate that protects the email-match auto-link path.
            claims["email_verified"] = await oauth_service.is_email_verified_for_user(
                db_session, UUID(subject), profile["email"]
            )
        if "name" in allowed_claims:
            claims["name"] = profile["name"]
        if "picture" in allowed_claims:
            claims["picture"] = profile["picture"]

        return Response(
            content=claims,
            status_code=200,
            media_type="application/json",
        )

    @post("/revoke")
    async def revoke(self, request: Request, db_session: AsyncSession) -> Response:
        """Token revocation endpoint (RFC 7009). Always returns 200."""
        form_data = await request.form()
        token_str = form_data.get("token", "")

        if not token_str:
            return Response(content={}, status_code=200, media_type="application/json")

        settings = get_settings()
        payload = verify_signed_token(token_str, settings.secret_key)

        if payload and payload.get("jti"):
            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(
                db_session, payload["jti"], payload.get("type", "unknown"), expires_at
            )
        elif payload is None:
            jwt_claims = await self._verify_jwt_access_claims(request, db_session, token_str)
            if jwt_claims and jwt_claims.get("jti"):
                expires_at = datetime.fromtimestamp(jwt_claims["exp"], tz=timezone.utc)
                await oauth2_service.revoke_token(db_session, jwt_claims["jti"], "access", expires_at)

        # RFC 7009: always return 200, even if token was invalid
        return Response(content={}, status_code=200, media_type="application/json")

    @post("/introspect")
    async def introspect(self, request: Request, db_session: AsyncSession) -> Response:
        """Token introspection endpoint (RFC 7662). Requires client auth."""
        form_data = await request.form()
        token_str = form_data.get("token", "")
        client_id = form_data.get("client_id", "")
        client_secret = form_data.get("client_secret", "")

        # Require client authentication
        if not client_id:
            return _json_error("invalid_client", "client_id required")

        client = await oauth2_service.get_client_by_client_id(db_session, client_id)
        if not client:
            return _json_error("invalid_client", "Unknown client_id")

        # RFC 7662 §2.1: introspection must be limited to authenticated
        # callers. Public (secretless) clients — e.g. dynamically-registered
        # ones — cannot authenticate here, so they may not introspect at all;
        # anything less turns this endpoint into an unauthenticated
        # token-validity oracle.
        if not client.client_secret:
            return _json_error(
                "invalid_client", "Public clients may not introspect tokens", status_code=401
            )
        if not verify_client_secret(client_secret, client.client_secret):
            return _json_error("invalid_client", "Invalid client_secret")

        if not token_str:
            return Response(content={"active": False}, status_code=200, media_type="application/json")

        settings = get_settings()
        payload = await verify_oauth_token(token_str, settings.secret_key, db_session)
        if payload is None:
            jwt_claims = await self._verify_jwt_access_claims(request, db_session, token_str)
            if jwt_claims is not None:
                payload = {
                    "type": "access",
                    "user_id": jwt_claims.get("sub", ""),
                    "client_id": jwt_claims.get("client_id", ""),
                    "scope": jwt_claims.get("scope", ""),
                    "exp": jwt_claims.get("exp"),
                    "aud": jwt_claims.get("aud", ""),
                }

        if not payload:
            return Response(content={"active": False}, status_code=200, media_type="application/json")

        # RFC 7662 §2.2: `active` is the only required field; every other
        # field is optional. We return the full set only when the
        # introspecting client is the one that issued the token — any
        # other authenticated client sees a minimal response so it
        # cannot enumerate other clients' users or scopes.
        token_client_id = payload.get("client_id", "")
        if token_client_id == client.client_id:
            result = {
                "active": True,
                "token_type": payload.get("type", ""),
                "client_id": token_client_id,
                "sub": payload.get("user_id", ""),
                "scope": payload.get("scope", ""),
                "exp": payload.get("exp"),
            }
            if payload.get("aud"):
                result["aud"] = payload["aud"]
        else:
            result = {
                "active": True,
                "token_type": payload.get("type", ""),
                "exp": payload.get("exp"),
            }

        return Response(content=result, status_code=200, media_type="application/json")
