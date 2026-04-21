"""OAuth2 Authorization Server controller.

Provides ``/oauth/authorize``, ``/oauth/token``, ``/oauth/userinfo``,
``/oauth/revoke``, and ``/oauth/introspect`` endpoints so a Skrift
instance can act as an identity hub for spoke sites.
"""

import base64
import hashlib
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.client_secret import verify_client_secret
from skrift.auth.scopes import SCOPE_DEFINITIONS
from skrift.auth.session_keys import SESSION_USER_EMAIL, SESSION_USER_ID, SESSION_USER_NAME, SESSION_USER_PICTURE_URL
from skrift.auth.tokens import create_signed_token, verify_signed_token
from skrift.config import get_settings
from skrift.db.services import oauth2_service
from skrift.forms import verify_csrf

# Token lifetimes
AUTH_CODE_TTL = 600        # 10 minutes
ACCESS_TOKEN_TTL = 900     # 15 minutes
REFRESH_TOKEN_TTL = 2592000  # 30 days


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
            })
            next_url = f"/oauth/authorize?{query}"
            return Redirect(path=f"/auth/login?next={next_url}")

        # Store params in session for POST consent
        request.session["oauth_authorize"] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
            "code_challenge": code_challenge,
        }

        # Build scope descriptions for the consent screen
        scope_descriptions = []
        for s in requested_scopes:
            defn = SCOPE_DEFINITIONS.get(s)
            if defn:
                scope_descriptions.append({"name": s, "description": defn.description})
            else:
                scope_descriptions.append({"name": s, "description": s})

        return TemplateResponse(
            "oauth/authorize.html",
            context={
                "client_id": client_id,
                "display_name": client.display_name,
                "scopes": requested_scopes,
                "scope_descriptions": scope_descriptions,
                "request": request,
            },
        )

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

        # User denied
        if action == "deny":
            sep = "&" if "?" in redirect_uri else "?"
            deny_url = f"{redirect_uri}{sep}" + urlencode({"error": "access_denied", "state": state})
            return Redirect(path=deny_url)

        # User approved — create auth code
        settings = get_settings()
        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            return _json_error("invalid_request", "User not logged in")

        code_payload = {
            "type": "code",
            "user_id": user_id,
            "email": request.session.get(SESSION_USER_EMAIL, ""),
            "name": request.session.get(SESSION_USER_NAME, ""),
            "picture_url": request.session.get(SESSION_USER_PICTURE_URL, ""),
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "code_challenge": code_challenge,
        }

        code = create_signed_token(code_payload, settings.secret_key, AUTH_CODE_TTL)

        sep = "&" if "?" in redirect_uri else "?"
        callback_url = f"{redirect_uri}{sep}" + urlencode({"code": code, "state": state})
        return Redirect(path=callback_url)

    @post("/token")
    async def token_exchange(self, request: Request, db_session: AsyncSession) -> Response:
        """Token endpoint — exchange auth code or refresh token for access token."""
        form_data = await request.form()
        grant_type = form_data.get("grant_type", "")

        if grant_type == "authorization_code":
            return await self._handle_authorization_code(form_data, db_session)
        elif grant_type == "refresh_token":
            return await self._handle_refresh_token(form_data, db_session)
        else:
            return _json_error("unsupported_grant_type", f"Unsupported grant_type: {grant_type}")

    async def _handle_authorization_code(self, form_data, db_session: AsyncSession) -> Response:
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

        # Revoke the auth code before issuing tokens so a concurrent replay fails.
        code_jti = payload.get("jti")
        if code_jti:
            code_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(db_session, code_jti, "code", code_exp)

        scope = payload.get("scope", "")

        # Stamp a fresh family id so later refresh rotations can detect reuse
        # (presenting a previously-rotated token) and mass-revoke the lineage.
        family_id = uuid.uuid4().hex

        # Issue tokens
        access_payload = {
            "type": "access",
            "user_id": payload["user_id"],
            "email": payload["email"],
            "name": payload["name"],
            "picture_url": payload["picture_url"],
            "client_id": client_id,
            "scope": scope,
        }
        refresh_payload = {
            "type": "refresh",
            "user_id": payload["user_id"],
            "client_id": client_id,
            "scope": scope,
            "family_id": family_id,
        }

        access_token = create_signed_token(access_payload, settings.secret_key, ACCESS_TOKEN_TTL)
        refresh_token = create_signed_token(refresh_payload, settings.secret_key, REFRESH_TOKEN_TTL)

        return Response(
            content={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "scope": scope,
            },
            status_code=200,
            media_type="application/json",
        )

    async def _handle_refresh_token(self, form_data, db_session: AsyncSession) -> Response:
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

        # Revoke the old refresh token (normal rotation path).
        if old_jti:
            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(db_session, old_jti, "refresh", expires_at)

        # Issue new access + refresh tokens (token rotation); stay on the
        # same family so future rotations remain linkable for reuse checks.
        access_payload = {
            "type": "access",
            "user_id": payload["user_id"],
            "email": "",
            "name": "",
            "picture_url": "",
            "client_id": client_id,
            "scope": effective_scope,
        }
        refresh_payload = {
            "type": "refresh",
            "user_id": payload["user_id"],
            "client_id": client_id,
            "scope": effective_scope,
            "family_id": family_id,
        }

        access_token = create_signed_token(access_payload, settings.secret_key, ACCESS_TOKEN_TTL)
        new_refresh_token = create_signed_token(refresh_payload, settings.secret_key, REFRESH_TOKEN_TTL)

        return Response(
            content={
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "token_type": "bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "scope": effective_scope,
            },
            status_code=200,
            media_type="application/json",
        )

    @get("/userinfo")
    async def userinfo(self, request: Request, db_session: AsyncSession) -> Response:
        """UserInfo endpoint — return user data from a valid access token."""
        settings = get_settings()

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return _json_error("invalid_token", "Missing or invalid Bearer token", status_code=401)

        token = auth_header[7:]  # Strip "Bearer "
        payload = await verify_oauth_token(token, settings.secret_key, db_session)
        if not payload or payload.get("type") != "access":
            return _json_error("invalid_token", "Invalid or expired access token", status_code=401)

        # Build claims strictly from granted scopes. A token minted with
        # no scopes gets only `sub` — prior backwards-compat code returned
        # the full profile + email, which silently defeated scope filtering.
        scope_str = payload.get("scope", "")
        granted_scopes = scope_str.split() if scope_str else []

        allowed_claims: set[str] = set()
        for s in granted_scopes:
            defn = SCOPE_DEFINITIONS.get(s)
            if defn:
                allowed_claims.update(defn.claims)

        # Always include sub — the minimum subject identifier required by OIDC.
        claims: dict = {"sub": payload["user_id"]}

        if "email" in allowed_claims:
            claims["email"] = payload.get("email", "")
        if "name" in allowed_claims:
            claims["name"] = payload.get("name", "")
        if "picture" in allowed_claims:
            claims["picture"] = payload.get("picture_url", "")

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

        if client.client_secret and not verify_client_secret(client_secret, client.client_secret):
            return _json_error("invalid_client", "Invalid client_secret")

        if not token_str:
            return Response(content={"active": False}, status_code=200, media_type="application/json")

        settings = get_settings()
        payload = await verify_oauth_token(token_str, settings.secret_key, db_session)

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
        else:
            result = {
                "active": True,
                "token_type": payload.get("type", ""),
                "exp": payload.get("exp"),
            }

        return Response(content=result, status_code=200, media_type="application/json")
