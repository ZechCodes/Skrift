"""Passkey-backed primary authentication method."""

from __future__ import annotations

from litestar.response import Template as TemplateResponse

from skrift.auth.methods.base import PrimaryAuthMethod, PrimaryAuthMethodDescriptor
from skrift.auth.second_factors.passkey_service import is_webauthn_available
from skrift.auth.session_keys import SESSION_AUTH_NEXT
from skrift.config import get_settings
from skrift.lib.flash import get_flash_messages
from skrift.lib.template import resolve_template_name


class PasskeyPrimaryAuthMethod(PrimaryAuthMethod):
    """Primary-auth method implementation for discoverable passkey sign-in."""

    method_type = "passkey"

    def get_descriptor(self) -> PrimaryAuthMethodDescriptor:
        settings = get_settings()
        config = settings.auth.get_method_config(self.method_key)
        name = config.get("label", "") or "Passkey"
        if not is_webauthn_available():
            name = f"{name} (Unavailable)"
        return PrimaryAuthMethodDescriptor(
            key=self.method_key,
            method_type=self.method_type,
            name=name,
            icon="passkey",
            start_path=f"/auth/{self.method_key}/login",
        )

    async def begin_auth(self, request, *, next_url: str | None = None):
        settings = get_settings()
        if next_url:
            from skrift.controllers.auth import _is_safe_redirect_url

            if _is_safe_redirect_url(next_url, settings.auth.allowed_redirect_domains):
                request.session[SESSION_AUTH_NEXT] = next_url

        template_name = resolve_template_name(
            request.app.template_engine, "passkey_login.html", "auth/passkey_login.html"
        )
        return TemplateResponse(
            template_name,
            context={
                "method_key": self.method_key,
                "descriptor": self.get_descriptor(),
                "flash": request.session.pop("flash", None),
                "flash_messages": get_flash_messages(request),
            },
        )

    async def complete_auth(self, request, **kwargs):
        from litestar.exceptions import HTTPException

        raise HTTPException(
            status_code=400,
            detail="Passkey auth uses the primary passkey completion endpoint",
        )
