"""OAuth-backed primary authentication method."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

from litestar.exceptions import HTTPException, NotFoundException
from litestar.response import Redirect

from skrift.auth.methods.base import (
    PrimaryAuthCompletion,
    PrimaryAuthMethod,
    PrimaryAuthMethodDescriptor,
)
from skrift.auth.oauth_flow import exchange_and_fetch_oauth_identity
from skrift.auth.providers import get_oauth_provider
from skrift.auth.session_keys import (
    SESSION_AUTH_NEXT,
    SESSION_OAUTH_CODE_VERIFIER,
    SESSION_OAUTH_PROVIDER,
    SESSION_OAUTH_STATE,
)
from skrift.config import get_settings
from skrift.lib.redirects import is_safe_redirect_url
from skrift.setup.providers import get_provider_info


def _resolve_provider_info(method_key: str, provider_type: str):
    """Resolve built-in or custom provider metadata."""
    provider_info = get_provider_info(provider_type)
    if provider_info is not None:
        return provider_info
    return get_oauth_provider(method_key, provider_type=provider_type).provider_info


class OAuthPrimaryAuthMethod(PrimaryAuthMethod):
    """Primary-auth method implementation for OAuth providers."""

    method_type = "oauth"

    def get_descriptor(self) -> PrimaryAuthMethodDescriptor:
        settings = get_settings()
        provider_type = settings.auth.get_provider_type(self.method_key)
        provider_info = _resolve_provider_info(self.method_key, provider_type)

        return PrimaryAuthMethodDescriptor(
            key=self.method_key,
            method_type=self.method_type,
            name=provider_info.name,
            icon=provider_info.icon,
            start_path=f"/auth/{self.method_key}/login",
        )

    async def begin_auth(self, request, *, next_url: str | None = None):
        settings = get_settings()
        provider_type = settings.auth.get_provider_type(self.method_key)
        provider_info = _resolve_provider_info(self.method_key, provider_type)

        if self.method_key not in settings.auth.providers:
            raise NotFoundException(f"Provider {self.method_key} not configured")

        if next_url and is_safe_redirect_url(next_url, settings.auth.allowed_redirect_domains):
            request.session[SESSION_AUTH_NEXT] = next_url

        state = secrets.token_urlsafe(32)
        request.session[SESSION_OAUTH_STATE] = state
        request.session[SESSION_OAUTH_PROVIDER] = self.method_key

        oauth_provider = get_oauth_provider(self.method_key, provider_type=provider_type)
        code_challenge = None
        if oauth_provider.requires_pkce:
            code_verifier = secrets.token_urlsafe(64)[:128]
            request.session[SESSION_OAUTH_CODE_VERIFIER] = code_verifier
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode().rstrip("=")

        provider_config = settings.auth.providers[self.method_key]
        tenant = getattr(provider_config, "tenant_id", None)
        auth_url = oauth_provider.resolve_url(provider_info.auth_url, tenant)
        params = oauth_provider.build_auth_params(
            client_id=provider_config.client_id,
            redirect_uri=settings.auth.get_redirect_uri(self.method_key),
            scopes=provider_config.scopes,
            state=state,
            code_challenge=code_challenge,
        )

        return Redirect(path=f"{auth_url}?{urlencode(params)}")

    async def complete_auth(
        self,
        request,
        *,
        code: str | None = None,
        oauth_state: str | None = None,
        error: str | None = None,
    ) -> PrimaryAuthCompletion:
        settings = get_settings()
        provider_type = settings.auth.get_provider_type(self.method_key)

        _resolve_provider_info(self.method_key, provider_type)

        if error:
            raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

        stored_state = request.session.pop(SESSION_OAUTH_STATE, None)
        if not oauth_state or oauth_state != stored_state:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not code:
            raise HTTPException(status_code=400, detail="Missing authorization code")

        code_verifier = request.session.pop(SESSION_OAUTH_CODE_VERIFIER, None)
        identity, user_info, tokens = await exchange_and_fetch_oauth_identity(
            self.method_key,
            settings,
            code,
            settings.auth.get_redirect_uri(self.method_key),
            code_verifier,
            provider_type=provider_type,
        )
        return PrimaryAuthCompletion(identity=identity, raw_user_info=user_info, tokens=tokens)
