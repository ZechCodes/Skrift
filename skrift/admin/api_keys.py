"""API key management admin controller."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.auth.roles import ROLE_DEFINITIONS
from skrift.config import get_settings
from skrift.db.models.api_key import APIKey
from skrift.db.models.user import User
from skrift.db.services import api_key_service
from skrift.lib.flash import flash_error, flash_success, get_flash_messages


def _all_permissions() -> list[str]:
    """Collect all known permissions across role definitions."""
    perms: set[str] = set()
    for role_def in ROLE_DEFINITIONS.values():
        perms.update(role_def.permissions)
    # Remove the special administrator permission — it's implicit
    perms.discard("administrator")
    return sorted(perms)


class APIKeyAdminController(Controller):
    """Controller for managing API keys in admin."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/api-keys",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-api-keys")],
        opt={"label": "API Keys", "icon": "key", "order": 91},
    )
    async def list_keys(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List all API keys."""
        ctx = await get_admin_context(request, db_session)
        keys = await api_key_service.list_api_keys(db_session)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/api_keys/list.html",
            context={"flash_messages": flash_messages, "api_keys": keys, **ctx},
        )

    @get(
        "/api-keys/new",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def new_key_form(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show create API key form."""
        ctx = await get_admin_context(request, db_session)
        flash_messages = get_flash_messages(request)

        # Get all active users for the user dropdown
        result = await db_session.execute(
            select(User).where(User.is_active == True).order_by(User.name)  # noqa: E712
        )
        users = list(result.scalars().all())

        return TemplateResponse(
            "admin/api_keys/edit.html",
            context={
                "flash_messages": flash_messages,
                "api_key": None,
                "users": users,
                "available_permissions": _all_permissions(),
                "available_roles": {
                    name: rd for name, rd in ROLE_DEFINITIONS.items() if name != "admin"
                },
                **ctx,
            },
        )

    @post(
        "/api-keys/new",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def create_key(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        """Create a new API key."""
        form_data = await request.form()
        display_name = form_data.get("display_name", "").strip()
        user_id = form_data.get("user_id", "").strip()

        if not display_name:
            flash_error(request, "Display name is required")
            return Redirect(path="/admin/api-keys/new")
        if not user_id:
            flash_error(request, "User is required")
            return Redirect(path="/admin/api-keys/new")

        description = form_data.get("description", "").strip() or None

        # Collect scoped permissions
        scoped_permissions = []
        for perm in _all_permissions():
            if form_data.get(f"perm_{perm}") == "on":
                scoped_permissions.append(perm)

        # Collect scoped roles
        scoped_roles = []
        for role_name in ROLE_DEFINITIONS:
            if role_name != "admin" and form_data.get(f"role_{role_name}") == "on":
                scoped_roles.append(role_name)

        # Parse expiration
        expires_at = None
        expires_str = form_data.get("expires_at", "").strip()
        if expires_str:
            try:
                expires_at = datetime.fromisoformat(expires_str).replace(tzinfo=timezone.utc)
            except ValueError:
                flash_error(request, "Invalid expiration date format")
                return Redirect(path="/admin/api-keys/new")

        settings = get_settings()
        api_key, raw_key, raw_refresh = await api_key_service.create_api_key(
            db_session,
            user_id,
            display_name,
            description=description,
            scoped_permissions=scoped_permissions or None,
            scoped_roles=scoped_roles or None,
            expires_at=expires_at,
            refresh_token_expiration_days=settings.api_keys.refresh_token_expiration_days,
        )

        # Store raw secrets in session for show-once display
        request.session["new_api_key"] = raw_key
        request.session["new_refresh_token"] = raw_refresh
        flash_success(request, f"API key '{display_name}' created")
        return Redirect(path=f"/admin/api-keys/{api_key.id}/edit")

    @get(
        "/api-keys/{key_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def edit_key_form(
        self, request: Request, db_session: AsyncSession, key_id: UUID
    ) -> TemplateResponse:
        """Show edit API key form."""
        ctx = await get_admin_context(request, db_session)
        api_key = await api_key_service.get_api_key(db_session, key_id)
        if not api_key:
            flash_error(request, "API key not found")
            return Redirect(path="/admin/api-keys")

        # Get all active users for the user dropdown
        result = await db_session.execute(
            select(User).where(User.is_active == True).order_by(User.name)  # noqa: E712
        )
        users = list(result.scalars().all())

        new_api_key = request.session.pop("new_api_key", None)
        new_refresh_token = request.session.pop("new_refresh_token", None)
        flash_messages = get_flash_messages(request)

        return TemplateResponse(
            "admin/api_keys/edit.html",
            context={
                "flash_messages": flash_messages,
                "api_key": api_key,
                "users": users,
                "available_permissions": _all_permissions(),
                "available_roles": {
                    name: rd for name, rd in ROLE_DEFINITIONS.items() if name != "admin"
                },
                "new_api_key": new_api_key,
                "new_refresh_token": new_refresh_token,
                **ctx,
            },
        )

    @post(
        "/api-keys/{key_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def update_key(
        self, request: Request, db_session: AsyncSession, key_id: UUID
    ) -> Redirect:
        """Update an existing API key."""
        api_key = await api_key_service.get_api_key(db_session, key_id)
        if not api_key:
            flash_error(request, "API key not found")
            return Redirect(path="/admin/api-keys")

        form_data = await request.form()
        display_name = form_data.get("display_name", "").strip()
        description = form_data.get("description", "").strip() or None
        is_active = form_data.get("is_active") == "on"

        # Collect scoped permissions
        scoped_permissions = []
        for perm in _all_permissions():
            if form_data.get(f"perm_{perm}") == "on":
                scoped_permissions.append(perm)

        # Collect scoped roles
        scoped_roles = []
        for role_name in ROLE_DEFINITIONS:
            if role_name != "admin" and form_data.get(f"role_{role_name}") == "on":
                scoped_roles.append(role_name)

        # Parse expiration
        expires_at_val: datetime | None | type[...] = ...
        expires_str = form_data.get("expires_at", "").strip()
        if expires_str:
            try:
                expires_at_val = datetime.fromisoformat(expires_str).replace(tzinfo=timezone.utc)
            except ValueError:
                flash_error(request, "Invalid expiration date format")
                return Redirect(path=f"/admin/api-keys/{key_id}/edit")
        else:
            expires_at_val = None

        await api_key_service.update_api_key(
            db_session,
            api_key,
            display_name=display_name or None,
            description=description,
            scoped_permissions=scoped_permissions or None,
            scoped_roles=scoped_roles or None,
            expires_at=expires_at_val,
            is_active=is_active,
        )

        flash_success(request, f"API key '{api_key.display_name}' updated")
        return Redirect(path="/admin/api-keys")

    @post(
        "/api-keys/{key_id:uuid}/revoke",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def revoke_key(
        self, request: Request, db_session: AsyncSession, key_id: UUID
    ) -> Redirect:
        """Revoke an API key."""
        await api_key_service.revoke_api_key(db_session, key_id)
        flash_success(request, "API key revoked")
        return Redirect(path="/admin/api-keys")

    @post(
        "/api-keys/{key_id:uuid}/delete",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def delete_key(
        self, request: Request, db_session: AsyncSession, key_id: UUID
    ) -> Redirect:
        """Delete an API key."""
        await api_key_service.delete_api_key(db_session, key_id)
        flash_success(request, "API key deleted")
        return Redirect(path="/admin/api-keys")

    @post(
        "/api-keys/{key_id:uuid}/rotate",
        guards=[auth_guard, Permission("manage-api-keys")],
    )
    async def rotate_key(
        self, request: Request, db_session: AsyncSession, key_id: UUID
    ) -> Redirect:
        """Rotate an API key — generates a new key and refresh token."""
        api_key = await api_key_service.get_api_key(db_session, key_id)
        if not api_key:
            flash_error(request, "API key not found")
            return Redirect(path="/admin/api-keys")

        from skrift.db.services.api_key_service import (
            _generate_key,
            _generate_refresh_token,
        )
        from datetime import timedelta

        settings = get_settings()
        new_raw_key, new_prefix, new_key_hash = _generate_key()
        new_raw_refresh, new_refresh_hash = _generate_refresh_token()

        api_key.key_prefix = new_prefix
        api_key.key_hash = new_key_hash
        api_key.refresh_token_hash = new_refresh_hash
        api_key.refresh_token_expires_at = datetime.now(tz=timezone.utc) + timedelta(
            days=settings.api_keys.refresh_token_expiration_days
        )
        await db_session.commit()

        request.session["new_api_key"] = new_raw_key
        request.session["new_refresh_token"] = new_raw_refresh
        flash_success(request, f"Key rotated for '{api_key.display_name}'")
        return Redirect(path=f"/admin/api-keys/{key_id}/edit")
