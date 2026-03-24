"""API key refresh endpoint."""

from __future__ import annotations

from litestar import Controller, Request, post
from litestar.exceptions import NotAuthorizedException
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.config import get_settings
from skrift.db.services import api_key_service


class APIAuthController(Controller):
    """Controller for API key authentication operations."""

    path = "/api/auth"

    @post("/refresh")
    async def refresh_key(
        self, request: Request, db_session: AsyncSession
    ) -> Response:
        """Rotate an API key using a refresh token.

        Accepts JSON body: ``{"refresh_token": "skr_..."}``.
        Returns new key and refresh token. The old key stops working immediately.
        """
        body = await request.json()
        refresh_token = body.get("refresh_token", "")

        if not refresh_token or not refresh_token.startswith("skr_"):
            raise NotAuthorizedException("Invalid refresh token")

        settings = get_settings()
        result = await api_key_service.refresh_api_key(
            db_session,
            refresh_token,
            refresh_token_expiration_days=settings.api_keys.refresh_token_expiration_days,
        )

        if result is None:
            raise NotAuthorizedException("Invalid or expired refresh token")

        api_key, new_raw_key, new_raw_refresh = result

        return Response(
            content={
                "key": new_raw_key,
                "refresh_token": new_raw_refresh,
                "key_prefix": api_key.key_prefix,
                "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
                "refresh_token_expires_at": (
                    api_key.refresh_token_expires_at.isoformat()
                    if api_key.refresh_token_expires_at
                    else None
                ),
            },
            status_code=200,
        )
