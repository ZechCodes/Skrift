"""Dummy primary authentication method for development."""

from __future__ import annotations

from litestar.exceptions import HTTPException, NotFoundException
from litestar.response import Template as TemplateResponse

from skrift.auth.methods.base import PrimaryAuthMethod, PrimaryAuthMethodDescriptor
from skrift.auth.session_keys import SESSION_AUTH_NEXT
from skrift.config import get_settings
from skrift.lib.template import resolve_template_name
from skrift.setup.providers import DUMMY_PROVIDER_KEY


class DummyPrimaryAuthMethod(PrimaryAuthMethod):
    """Development-only dummy login method."""

    method_type = "dummy"

    def get_descriptor(self) -> PrimaryAuthMethodDescriptor:
        return PrimaryAuthMethodDescriptor(
            key=self.method_key,
            method_type=self.method_type,
            name="Dummy (Development Only)",
            icon="dummy",
            start_path=f"/auth/{self.method_key}/login",
        )

    async def begin_auth(self, request, *, next_url: str | None = None):
        settings = get_settings()
        if DUMMY_PROVIDER_KEY not in settings.auth.providers:
            raise NotFoundException("Dummy provider not configured")
        if next_url:
            from skrift.controllers.auth import _is_safe_redirect_url

            if _is_safe_redirect_url(next_url, settings.auth.allowed_redirect_domains):
                request.session[SESSION_AUTH_NEXT] = next_url

        flash = request.session.pop("flash", None)
        template_name = resolve_template_name(
            request.app.template_engine, "dummy_login.html", "auth/dummy_login.html"
        )
        return TemplateResponse(template_name, context={"flash": flash})

    async def complete_auth(self, request, **kwargs):
        raise HTTPException(status_code=400, detail="Dummy auth uses form submission")
