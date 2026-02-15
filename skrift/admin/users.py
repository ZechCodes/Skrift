"""User management admin controller."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse, Redirect
from litestar.params import Body
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotAuthorizedException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.guards import auth_guard, Permission
from skrift.auth.services import (
    assign_role_to_user,
    remove_role_from_user,
    invalidate_user_permissions_cache,
)
from skrift.auth.roles import ROLE_DEFINITIONS
from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.db.models.user import User
from skrift.lib.flash import get_flash_messages


class UserAdminController(Controller):
    """Controller for user management in admin."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/users",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-users")],
        opt={"label": "Users", "icon": "users", "order": 10},
    )
    async def list_users(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List all users with their roles."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(User)
            .options(selectinload(User.roles))
            .order_by(User.created_at.desc())
        )
        users = list(result.scalars().all())

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/users/list.html",
            context={"flash_messages": flash_messages, "users": users, **ctx},
        )

    @get(
        "/users/{user_id:uuid}/roles",
        guards=[auth_guard, Permission("manage-users")],
    )
    async def edit_user_roles(
        self, request: Request, db_session: AsyncSession, user_id: UUID
    ) -> TemplateResponse:
        """Edit user roles form."""
        ctx = await get_admin_context(request, db_session)

        result = await db_session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles))
        )
        target_user = result.scalar_one_or_none()
        if not target_user:
            raise NotAuthorizedException("User not found")

        current_roles = {role.name for role in target_user.roles}

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/users/roles.html",
            context={
                "flash_messages": flash_messages,
                "target_user": target_user,
                "current_roles": current_roles,
                "available_roles": ROLE_DEFINITIONS,
                **ctx,
            },
        )

    @post(
        "/users/{user_id:uuid}/roles",
        guards=[auth_guard, Permission("manage-users")],
    )
    async def save_user_roles(
        self,
        request: Request,
        db_session: AsyncSession,
        user_id: UUID,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Save user roles."""
        selected_roles = set()
        for key in data:
            if key.startswith("role_"):
                role_name = key[5:]
                if data[key] == "on":
                    selected_roles.add(role_name)

        result = await db_session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles))
        )
        target_user = result.scalar_one_or_none()
        if not target_user:
            request.session["flash"] = "User not found"
            return Redirect(path="/admin/users")

        current_roles = {role.name for role in target_user.roles}

        for role_name in selected_roles - current_roles:
            await assign_role_to_user(db_session, user_id, role_name)

        for role_name in current_roles - selected_roles:
            await remove_role_from_user(db_session, user_id, role_name)

        invalidate_user_permissions_cache(user_id)

        request.session["flash"] = f"Roles updated for {target_user.name or target_user.email}"
        return Redirect(path="/admin/users")
