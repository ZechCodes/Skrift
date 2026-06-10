"""OAuth-style API permission grant flow for third-party services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Response
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.permissions import (
    DISALLOW_API_GRANTS,
    REQUIRE_ELEVATED_SECURITY,
    REQUIRE_KNOWN_SERVICE,
    get_permission_definition,
    strictest_clearance,
)
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.auth.tokens import create_signed_token, verify_signed_token
from skrift.config import get_settings
from skrift.controllers.oauth2 import _verify_pkce
from skrift.db.services import api_key_service, oauth2_service
from skrift.forms import verify_csrf
from skrift.lib.client_ip import get_client_ip
from skrift.lib.hooks import (
    API_GRANT_AUTHORIZE_CONSTRAINTS,
    API_GRANT_AUTHORIZE_CONTEXT,
    API_GRANT_AUTHORIZE_FRAGMENTS,
    API_GRANT_TOKEN_RESPONSE,
    hooks,
)


REQUEST_TOKEN_TTL = 600
AUTH_CODE_TTL = 600


def _json_error(error: str, description: str, status_code: int = 400) -> Response:
    return Response(
        content={"error": error, "error_description": description},
        status_code=status_code,
        media_type="application/json",
    )


def _extract_bearer(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


def _parse_permissions(value: Any) -> list[str]:
    if isinstance(value, str):
        return sorted({p.strip() for p in value.split() if p.strip()})
    if isinstance(value, list):
        return sorted({str(p).strip() for p in value if str(p).strip()})
    return []


def _valid_redirect_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _valid_return_url(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _origin_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _redirect_with_params(uri: str, params: dict[str, str]) -> Redirect:
    sep = "&" if "?" in uri else "?"
    return Redirect(path=f"{uri}{sep}{urlencode(params)}")


def _permission_context(permissions: list[str]) -> list[dict[str, str]]:
    return [
        {
            "slug": definition.slug,
            "display_name": definition.display_name,
            "description": definition.description,
            "service_clearance": definition.service_clearance,
        }
        for definition in (get_permission_definition(p) for p in permissions)
    ]


def _group_fragments(fragments: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        "before_permissions": [],
        "after_permissions": [],
        "before_actions": [],
    }
    for fragment in fragments:
        if not isinstance(fragment, dict) or not fragment.get("template"):
            continue
        slot = str(fragment.get("slot") or "after_permissions")
        grouped.setdefault(slot, []).append(fragment)
    return grouped


async def _verify_service_key(
    request: Request,
    db_session: AsyncSession,
):
    bearer = _extract_bearer(request)
    if not bearer or not bearer.startswith("sk_"):
        return None
    return await api_key_service.verify_api_key_with_permissions(
        db_session,
        bearer,
        client_ip=get_client_ip(request.scope),
    )


class APIGrantController(Controller):
    """Controller for user-approved API permission grants."""

    path = "/api/grants"

    @post("/request")
    async def create_request(self, request: Request, db_session: AsyncSession) -> Response:
        """Create a short-lived browser authorization request token.

        Known and elevated service requests must call this endpoint with a
        service API key. Anonymous requests may either use this endpoint or
        send equivalent parameters directly to ``/api/grants/authorize``.
        """
        body = await request.json()
        permissions = _parse_permissions(body.get("permissions", ""))
        redirect_uri = str(body.get("redirect_uri", "")).strip()
        service_name = str(body.get("service_name", "")).strip()
        service_url = str(body.get("service_url", "")).strip()
        state = str(body.get("state", "")).strip()
        code_challenge = str(body.get("code_challenge", "")).strip()
        code_challenge_method = str(body.get("code_challenge_method", "")).strip()

        grant_data = await self._validate_request_data(
            request,
            db_session,
            permissions=permissions,
            redirect_uri=redirect_uri,
            service_name=service_name,
            service_url=service_url,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            require_request_token=False,
        )
        if isinstance(grant_data, Response):
            return grant_data

        settings = get_settings()
        request_token = create_signed_token(
            {"type": "api_grant_request", **grant_data},
            settings.secret_key,
            REQUEST_TOKEN_TTL,
        )

        return Response(
            content={
                "request_token": request_token,
                "authorize_url": f"/api/grants/authorize?{urlencode({'request_token': request_token})}",
                "expires_in": REQUEST_TOKEN_TTL,
            },
            status_code=200,
            media_type="application/json",
        )

    @get("/authorize")
    async def authorize_get(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> TemplateResponse | Redirect:
        """Show the grant consent screen or redirect to login."""
        grant_data = await self._grant_data_from_authorize_request(request, db_session)
        if isinstance(grant_data, TemplateResponse):
            return grant_data

        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            if grant_data.get("request_token"):
                next_url = f"/api/grants/authorize?{urlencode({'request_token': grant_data['request_token']})}"
            else:
                query = urlencode({
                    "response_type": "code",
                    "redirect_uri": grant_data["redirect_uri"],
                    "permissions": " ".join(grant_data["permissions"]),
                    "service_name": grant_data["service_name"],
                    "service_url": grant_data.get("service_url", ""),
                    "state": grant_data.get("state", ""),
                    "code_challenge": grant_data["code_challenge"],
                    "code_challenge_method": "S256",
                })
                next_url = f"/api/grants/authorize?{query}"
            return Redirect(path=f"/auth/login?{urlencode({'next': next_url})}")

        request.session["api_grant_authorize"] = grant_data

        context = {
            "request": request,
            "service_name": grant_data["service_name"],
            "service_url": grant_data.get("service_url", ""),
            "service_origin": _origin_from_uri(grant_data.get("service_url", "")),
            "permissions": _permission_context(grant_data["permissions"]),
            "requires_elevated_security": (
                grant_data["service_clearance"] == REQUIRE_ELEVATED_SECURITY
            ),
        }
        context = await hooks.apply_filters(
            API_GRANT_AUTHORIZE_CONTEXT,
            context,
            request,
            grant_data,
        )
        fragments = await hooks.apply_filters(
            API_GRANT_AUTHORIZE_FRAGMENTS,
            [],
            request,
            grant_data,
            context,
        )
        context["grant_fragments"] = _group_fragments(fragments)

        return TemplateResponse(
            "api_grants/authorize.html",
            context=context,
        )

    @post("/authorize")
    async def authorize_post(self, request: Request, db_session: AsyncSession) -> Redirect | Response:
        """Process consent and redirect back with a one-time code."""
        if not await verify_csrf(request):
            return _json_error("invalid_request", "Invalid CSRF token")

        form_data = await request.form()
        action = form_data.get("action", "")
        grant_data = request.session.pop("api_grant_authorize", None)
        if not grant_data:
            return _json_error("invalid_request", "Grant authorization session expired")

        redirect_uri = grant_data["redirect_uri"]
        state = grant_data.get("state", "")

        if action == "deny":
            return _redirect_with_params(
                redirect_uri,
                {"error": "access_denied", "state": state},
            )

        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            return _json_error("invalid_request", "User not logged in")

        constraints = dict(grant_data.get("constraints", {}))
        constraints = await hooks.apply_filters(
            API_GRANT_AUTHORIZE_CONSTRAINTS,
            constraints,
            request,
            grant_data,
            form_data,
        )
        if isinstance(constraints, Response):
            return constraints

        settings = get_settings()
        code = create_signed_token(
            {
                "type": "api_grant_code",
                "user_id": str(user_id),
                "redirect_uri": redirect_uri,
                "permissions": " ".join(grant_data["permissions"]),
                "service_name": grant_data["service_name"],
                "service_url": grant_data.get("service_url", ""),
                "service_clearance": grant_data["service_clearance"],
                "parent_api_key_id": grant_data.get("parent_api_key_id", ""),
                "code_challenge": grant_data["code_challenge"],
                "constraints": constraints,
            },
            settings.secret_key,
            AUTH_CODE_TTL,
        )
        return _redirect_with_params(redirect_uri, {"code": code, "state": state})

    @post("/token")
    async def token_exchange(self, request: Request, db_session: AsyncSession) -> Response:
        """Exchange a grant authorization code for a scoped Skrift API key."""
        form_data = await request.form()
        if form_data.get("grant_type", "") != "authorization_code":
            return _json_error("unsupported_grant_type", "Only authorization_code is supported")

        code = form_data.get("code", "")
        redirect_uri = form_data.get("redirect_uri", "")
        code_verifier = form_data.get("code_verifier", "")

        settings = get_settings()
        payload = verify_signed_token(code, settings.secret_key)
        if not payload or payload.get("type") != "api_grant_code":
            return _json_error("invalid_grant", "Invalid or expired authorization code")

        code_jti = payload.get("jti")
        if code_jti and await oauth2_service.is_token_revoked(db_session, code_jti):
            return _json_error("invalid_grant", "Authorization code has already been used")

        if payload.get("redirect_uri") != redirect_uri:
            return _json_error("invalid_grant", "redirect_uri mismatch")

        stored_challenge = payload.get("code_challenge", "")
        if not stored_challenge or not code_verifier:
            return _json_error("invalid_grant", "PKCE verifier required")
        if not _verify_pkce(code_verifier, stored_challenge):
            return _json_error("invalid_grant", "PKCE verification failed")

        parent_api_key_id = str(payload.get("parent_api_key_id", "")).strip()
        if parent_api_key_id:
            verified = await _verify_service_key(request, db_session)
            if verified is None:
                return _json_error("invalid_client", "Service API key required", status_code=401)
            parent_key, parent_permissions = verified
            if str(parent_key.id) != parent_api_key_id:
                return _json_error("invalid_client", "Service API key does not match grant")
            if parent_key.principal_type != "service":
                return _json_error("invalid_client", "Requester must use a service API key", status_code=403)
            requested = set(_parse_permissions(payload.get("permissions", "")))
            if not requested.issubset(parent_permissions.permissions):
                return _json_error("invalid_scope", "Requested permissions exceed service key")

        if code_jti:
            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            await oauth2_service.revoke_token(db_session, code_jti, "api_grant_code", expires_at)

        permissions = _parse_permissions(payload.get("permissions", ""))
        expires_at = datetime.now(tz=timezone.utc) + timedelta(
            days=settings.api_keys.default_expiration_days
        )
        api_key, raw_key, raw_refresh = await api_key_service.create_api_key(
            db_session,
            payload["user_id"],
            f"{payload['service_name']} API grant",
            description="Issued by the API permission grant flow.",
            scoped_permissions=permissions,
            expires_at=expires_at,
            refresh_token_expiration_days=settings.api_keys.refresh_token_expiration_days,
            principal_type="service",
            service_name=payload["service_name"],
            service_url=payload.get("service_url") or None,
            parent_api_key_id=parent_api_key_id or None,
            grant_source="api-grant",
            constraints=payload.get("constraints") or None,
        )

        response_content = {
            "key": raw_key,
            "refresh_token": raw_refresh,
            "key_prefix": api_key.key_prefix,
            "token_type": "bearer",
            "principal_type": api_key.principal_type,
            "service_name": api_key.service_name,
            "permissions": permissions,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
            "refresh_token_expires_at": (
                api_key.refresh_token_expires_at.isoformat()
                if api_key.refresh_token_expires_at
                else None
            ),
        }
        response_content = await hooks.apply_filters(
            API_GRANT_TOKEN_RESPONSE,
            response_content,
            request,
            payload,
            api_key,
        )

        return Response(
            content=response_content,
            status_code=200,
            media_type="application/json",
        )

    async def _grant_data_from_authorize_request(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> dict[str, Any] | TemplateResponse:
        params = request.query_params
        request_token = params.get("request_token", "")
        if request_token:
            settings = get_settings()
            payload = verify_signed_token(request_token, settings.secret_key)
            if not payload or payload.get("type") != "api_grant_request":
                return self._error_template(request, "Invalid or expired grant request")
            parent_error = await self._validate_request_token_parent(db_session, payload)
            if parent_error:
                return self._error_template(request, parent_error, grant_data=payload)
            payload["request_token"] = request_token
            return dict(payload)

        if params.get("response_type", "") != "code":
            return self._error_template(request, "Only response_type=code is supported")

        permissions = _parse_permissions(params.get("permissions", ""))
        grant_data = await self._validate_request_data(
            request,
            db_session,
            permissions=permissions,
            redirect_uri=params.get("redirect_uri", ""),
            service_name=params.get("service_name", ""),
            service_url=params.get("service_url", ""),
            state=params.get("state", ""),
            code_challenge=params.get("code_challenge", ""),
            code_challenge_method=params.get("code_challenge_method", ""),
            require_request_token=True,
        )
        if isinstance(grant_data, Response):
            return self._error_template(
                request,
                grant_data.content.get("error_description", "Invalid grant request"),
            )
        return grant_data

    def _request_return_url(self, request: Request, grant_data: dict[str, Any] | None = None) -> str:
        service_url = ""
        redirect_uri = ""
        if grant_data:
            service_url = str(grant_data.get("service_url", "")).strip()
            redirect_uri = str(grant_data.get("redirect_uri", "")).strip()
        params = request.query_params
        service_url = service_url or str(params.get("service_url", "")).strip()
        redirect_uri = redirect_uri or str(params.get("redirect_uri", "")).strip()

        if service_url and _valid_return_url(service_url):
            return service_url
        return _origin_from_uri(redirect_uri)

    def _request_service_name(self, request: Request, grant_data: dict[str, Any] | None = None) -> str:
        if grant_data:
            service_name = str(grant_data.get("service_name", "")).strip()
            if service_name:
                return service_name
        return str(request.query_params.get("service_name", "")).strip()

    def _request_permissions(
        self,
        request: Request,
        grant_data: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        permissions = []
        if grant_data:
            permissions = _parse_permissions(grant_data.get("permissions", ""))
        if not permissions:
            permissions = _parse_permissions(request.query_params.get("permissions", ""))
        return _permission_context(permissions)

    def _friendly_error(self, message: str) -> dict[str, str | bool]:
        if message in {
            "Known-service permissions require a signed grant request token",
            "Service API key required",
        }:
            return {
                "title": "Unknown Service",
                "kind": "unknown_service",
                "summary": (
                    "The service that sent you here is not known to this site. For security "
                    "reasons, you cannot grant it access to the requested permissions."
                ),
                "next_step": (
                    "Contact the service administrators and ask them to verify their service "
                    "with this site."
                ),
                "developer_detail": (
                    "Known-service permissions must be requested through the signed request-token "
                    "flow using a valid service API key."
                ),
            }
        if message == "One or more requested permissions cannot be granted through the API grant flow":
            return {
                "title": "This access request is not allowed",
                "kind": "blocked_permissions",
                "summary": (
                    "The service asked for permissions this site does not allow through "
                    "this connection flow."
                ),
                "next_step": (
                    "Nothing was connected to your account. Contact the service administrators "
                    "and ask them to request different permissions."
                ),
                "developer_detail": (
                    "At least one requested permission is configured to disallow API grants."
                ),
            }
        if message == "Invalid or expired grant request":
            return {
                "title": "This connection request expired",
                "kind": "expired_request",
                "summary": "The app's connection request is no longer valid.",
                "next_step": "Go back to the app and start the connection again.",
                "developer_detail": "",
            }
        return {
            "title": "We couldn't start this connection",
            "kind": "invalid_request",
            "summary": "The app sent a connection request this site could not use.",
            "next_step": "Nothing was connected to your account. Go back to the app and start again.",
            "developer_detail": message,
        }

    async def _validate_request_data(
        self,
        request: Request,
        db_session: AsyncSession,
        *,
        permissions: list[str],
        redirect_uri: str,
        service_name: str,
        service_url: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
        require_request_token: bool,
    ) -> dict[str, Any] | Response:
        if not permissions:
            return _json_error("invalid_request", "At least one permission is required")
        if not _valid_redirect_uri(redirect_uri):
            return _json_error("invalid_request", "redirect_uri must be an absolute HTTP(S) URL")
        if not code_challenge or code_challenge_method != "S256":
            return _json_error("invalid_request", "PKCE S256 code_challenge is required")

        service_clearance = strictest_clearance(permissions)
        if service_clearance == DISALLOW_API_GRANTS:
            return _json_error(
                "invalid_scope",
                "One or more requested permissions cannot be granted through the API grant flow",
                status_code=403,
            )

        verified = await _verify_service_key(request, db_session)
        parent_key = None
        parent_permissions = None
        if verified is not None:
            parent_key, parent_permissions = verified

        requires_known = service_clearance in {REQUIRE_KNOWN_SERVICE, REQUIRE_ELEVATED_SECURITY}
        if requires_known:
            if require_request_token:
                return _json_error(
                    "invalid_request",
                    "Known-service permissions require a signed grant request token",
                    status_code=403,
                )
            if parent_key is None or parent_permissions is None:
                return _json_error("invalid_client", "Service API key required", status_code=401)
            if parent_key.principal_type != "service":
                return _json_error("invalid_client", "Requester must use a service API key", status_code=403)
            if not set(permissions).issubset(parent_permissions.permissions):
                return _json_error(
                    "invalid_scope",
                    "Requested permissions exceed the requesting service key",
                    status_code=403,
                )
            service_name = service_name or parent_key.service_name or parent_key.display_name
            service_url = service_url or parent_key.service_url or ""
        elif not service_name:
            return _json_error("invalid_request", "service_name is required")

        return {
            "redirect_uri": redirect_uri,
            "permissions": permissions,
            "service_name": service_name,
            "service_url": service_url,
            "state": state,
            "code_challenge": code_challenge,
            "service_clearance": service_clearance,
            "parent_api_key_id": str(parent_key.id) if requires_known and parent_key is not None else "",
        }

    def _error_template(
        self,
        request: Request,
        message: str,
        *,
        grant_data: dict[str, Any] | None = None,
    ) -> TemplateResponse:
        friendly = self._friendly_error(message)
        return_url = self._request_return_url(request, grant_data)
        return TemplateResponse(
            "api_grants/error.html",
            context={
                "request": request,
                "title": friendly["title"],
                "kind": friendly["kind"],
                "summary": friendly["summary"],
                "next_step": friendly["next_step"],
                "developer_detail": friendly["developer_detail"],
                "service_name": self._request_service_name(request, grant_data),
                "blocked_permissions": self._request_permissions(request, grant_data),
                "return_url": return_url,
                "return_label": "Back to the app" if return_url else "Back",
            },
        )

    async def _validate_request_token_parent(
        self,
        db_session: AsyncSession,
        payload: dict[str, Any],
    ) -> str:
        parent_api_key_id = str(payload.get("parent_api_key_id", "")).strip()
        if not parent_api_key_id:
            return ""

        parent_key = await api_key_service.get_api_key(db_session, parent_api_key_id)
        if parent_key is None or not parent_key.is_active or parent_key.is_expired:
            return "The requesting service key is no longer active."
        if not parent_key.user.is_active:
            return "The requesting service key owner is no longer active."
        if parent_key.principal_type != "service":
            return "The requester must use a service API key."

        requested = set(_parse_permissions(payload.get("permissions", "")))
        parent_permissions = await api_key_service.effective_permissions_for_api_key(
            db_session,
            parent_key,
        )
        if not requested.issubset(parent_permissions.permissions):
            return "The requested permissions exceed the requesting service key."
        return ""
