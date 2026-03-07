"""OAuth2 Authorization Server controller.

Provides ``/oauth/authorize``, ``/oauth/token``, ``/oauth/userinfo``,
``/oauth/revoke``, and ``/oauth/introspect`` endpoints so a Skrift
instance can act as an identity hub for spoke sites.
"""

import base64
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlencode

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

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

        # Public clients must use PKCE
        if not client.client_secret and not code_challenge:
            return _json_error("invalid_request", "Public clients must use PKCE (code_challenge required)")

        if code_challenge and code_challenge_method != "S256":
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

        # Verify auth code (no revocation check needed — codes are single-use by expiry)
        payload = verify_signed_token(code, settings.secret_key)
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

        # Confidential client: validate secret
        if client.client_secret:
            if client_secret != client.client_secret:
                return _json_error("invalid_client", "Invalid client_secret")

        # PKCE validation
        stored_challenge = payload.get("code_challenge", "")
        if stored_challenge:
            if not code_verifier:
                return _json_error("invalid_grant", "code_verifier required")
            if not _verify_pkce(code_verifier, stored_challenge):
                return _json_error("invalid_grant", "PKCE verification failed")

        scope = payload.get("scope", "")

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
        """Handle grant_type=refresh_token."""
        settings = get_settings()

        refresh_token_str = form_data.get("refresh_token", "")
        client_id = form_data.get("client_id", "")
        client_secret = form_data.get("client_secret", "")

        # Verify refresh token (with revocation check)
        payload = await verify_oauth_token(refresh_token_str, settings.secret_key, db_session)
        if not payload or payload.get("type") != "refresh":
            return _json_error("invalid_grant", "Invalid or expired refresh token")

        if payload["client_id"] != client_id:
            return _json_error("invalid_grant", "client_id mismatch")

        # Look up client
        client = await oauth2_service.get_client_by_client_id(db_session, client_id)
        if not client:
            return _json_error("invalid_client", "Unknown client_id")

        # Confidential client: validate secret
        if client.client_secret:
            if client_secret != client.client_secret:
                return _json_error("invalid_client", "Invalid client_secret")

        scope = payload.get("scope", "")

        # Revoke the old refresh token
        old_jti = payload.get("jti")
        if old_jti:
            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(db_session, old_jti, "refresh", expires_at)

        # Issue new access + refresh tokens (token rotation)
        access_payload = {
            "type": "access",
            "user_id": payload["user_id"],
            "email": "",
            "name": "",
            "picture_url": "",
            "client_id": client_id,
            "scope": scope,
        }
        refresh_payload = {
            "type": "refresh",
            "user_id": payload["user_id"],
            "client_id": client_id,
            "scope": scope,
        }

        access_token = create_signed_token(access_payload, settings.secret_key, ACCESS_TOKEN_TTL)
        new_refresh_token = create_signed_token(refresh_payload, settings.secret_key, REFRESH_TOKEN_TTL)

        return Response(
            content={
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "token_type": "bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "scope": scope,
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

        # Build claims based on granted scopes
        scope_str = payload.get("scope", "")
        granted_scopes = scope_str.split() if scope_str else []

        # Collect allowed claims from scope definitions
        allowed_claims: set[str] = set()
        for s in granted_scopes:
            defn = SCOPE_DEFINITIONS.get(s)
            if defn:
                allowed_claims.update(defn.claims)

        # If no scopes specified, return all claims (backwards compatibility)
        if not granted_scopes:
            allowed_claims = {"sub", "email", "name", "picture"}

        # Always include sub
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

        if client.client_secret and client_secret != client.client_secret:
            return _json_error("invalid_client", "Invalid client_secret")

        if not token_str:
            return Response(content={"active": False}, status_code=200, media_type="application/json")

        settings = get_settings()
        payload = await verify_oauth_token(token_str, settings.secret_key, db_session)

        if not payload:
            return Response(content={"active": False}, status_code=200, media_type="application/json")

        result = {
            "active": True,
            "token_type": payload.get("type", ""),
            "client_id": payload.get("client_id", ""),
            "sub": payload.get("user_id", ""),
            "scope": payload.get("scope", ""),
            "exp": payload.get("exp"),
        }

        return Response(content=result, status_code=200, media_type="application/json")
