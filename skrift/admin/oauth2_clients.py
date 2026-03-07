"""OAuth2 client management admin controller."""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse, Redirect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard, Permission
from skrift.auth.scopes import SCOPE_DEFINITIONS
from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.db.models.oauth2_client import OAuth2Client
from skrift.db.services import oauth2_service
from skrift.lib.flash import flash_error, flash_success, get_flash_messages


class OAuth2ClientAdminController(Controller):
    """Controller for managing OAuth2 clients in admin."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/oauth-clients",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-oauth-clients")],
        opt={"label": "OAuth Clients", "icon": "key", "order": 90},
    )
    async def list_clients(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List all OAuth2 clients."""
        ctx = await get_admin_context(request, db_session)
        clients = await oauth2_service.list_clients(db_session)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/oauth2/list.html",
            context={"flash_messages": flash_messages, "clients": clients, **ctx},
        )

    @get(
        "/oauth-clients/new",
        guards=[auth_guard, Permission("manage-oauth-clients")],
    )
    async def new_client_form(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show create client form."""
        ctx = await get_admin_context(request, db_session)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/oauth2/edit.html",
            context={
                "flash_messages": flash_messages,
                "client": None,
                "available_scopes": SCOPE_DEFINITIONS,
                **ctx,
            },
        )

    @post(
        "/oauth-clients/new",
        guards=[auth_guard, Permission("manage-oauth-clients")],
    )
    async def create_client(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        """Create a new OAuth2 client."""
        form_data = await request.form()
        display_name = form_data.get("display_name", "").strip()

        if not display_name:
            flash_error(request, "Display name is required")
            return Redirect(path="/admin/oauth-clients/new")

        redirect_uris_raw = form_data.get("redirect_uris", "")
        redirect_uris = [u.strip() for u in redirect_uris_raw.split("\n") if u.strip()]

        allowed_scopes = []
        for scope_name in SCOPE_DEFINITIONS:
            if form_data.get(f"scope_{scope_name}") == "on":
                allowed_scopes.append(scope_name)

        client = await oauth2_service.create_client(
            db_session, display_name, redirect_uris, allowed_scopes
        )

        request.session["new_secret"] = client.client_secret
        flash_success(request, f"Client '{display_name}' created")
        return Redirect(path=f"/admin/oauth-clients/{client.id}/edit")

    @get(
        "/oauth-clients/{client_db_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-oauth-clients")],
    )
    async def edit_client_form(
        self, request: Request, db_session: AsyncSession, client_db_id: UUID
    ) -> TemplateResponse:
        """Show edit client form."""
        ctx = await get_admin_context(request, db_session)
        result = await db_session.execute(
            select(OAuth2Client).where(OAuth2Client.id == client_db_id)
        )
        client = result.scalar_one_or_none()
        if not client:
            flash_error(request, "Client not found")
            return Redirect(path="/admin/oauth-clients")

        new_secret = request.session.pop("new_secret", None)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/oauth2/edit.html",
            context={
                "flash_messages": flash_messages,
                "client": client,
                "available_scopes": SCOPE_DEFINITIONS,
                "new_secret": new_secret,
                **ctx,
            },
        )

    @post(
        "/oauth-clients/{client_db_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-oauth-clients")],
    )
    async def update_client(
        self, request: Request, db_session: AsyncSession, client_db_id: UUID
    ) -> Redirect:
        """Update an existing OAuth2 client."""
        result = await db_session.execute(
            select(OAuth2Client).where(OAuth2Client.id == client_db_id)
        )
        client = result.scalar_one_or_none()
        if not client:
            flash_error(request, "Client not found")
            return Redirect(path="/admin/oauth-clients")

        form_data = await request.form()
        display_name = form_data.get("display_name", "").strip()
        redirect_uris_raw = form_data.get("redirect_uris", "")
        redirect_uris = [u.strip() for u in redirect_uris_raw.split("\n") if u.strip()]
        is_active = form_data.get("is_active") == "on"

        allowed_scopes = []
        for scope_name in SCOPE_DEFINITIONS:
            if form_data.get(f"scope_{scope_name}") == "on":
                allowed_scopes.append(scope_name)

        await oauth2_service.update_client(
            db_session, client,
            display_name=display_name,
            redirect_uris=redirect_uris,
            allowed_scopes=allowed_scopes,
            is_active=is_active,
        )

        flash_success(request, f"Client '{display_name}' updated")
        return Redirect(path="/admin/oauth-clients")

    @post(
        "/oauth-clients/{client_db_id:uuid}/delete",
        guards=[auth_guard, Permission("manage-oauth-clients")],
    )
    async def delete_client(
        self, request: Request, db_session: AsyncSession, client_db_id: UUID
    ) -> Redirect:
        """Delete an OAuth2 client."""
        await oauth2_service.delete_client(db_session, client_db_id)
        flash_success(request, "Client deleted")
        return Redirect(path="/admin/oauth-clients")

    @post(
        "/oauth-clients/{client_db_id:uuid}/regenerate-secret",
        guards=[auth_guard, Permission("manage-oauth-clients")],
    )
    async def regenerate_secret(
        self, request: Request, db_session: AsyncSession, client_db_id: UUID
    ) -> Redirect:
        """Regenerate a client's secret."""
        result = await db_session.execute(
            select(OAuth2Client).where(OAuth2Client.id == client_db_id)
        )
        client = result.scalar_one_or_none()
        if not client:
            flash_error(request, "Client not found")
            return Redirect(path="/admin/oauth-clients")

        new_secret = await oauth2_service.regenerate_client_secret(db_session, client)
        request.session["new_secret"] = new_secret
        flash_success(request, f"Secret regenerated for '{client.display_name}'")
        return Redirect(path=f"/admin/oauth-clients/{client_db_id}/edit")
