"""Authentication and authorization services."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from skrift.db.models.role import Role, RolePermission
from skrift.hooks import hooks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Cache for user permissions: TTL for freshness, LRU cap for memory. The TTL
# alone can't bound it — a user who never comes back is never read again, so
# their entry would sit there forever. Least-recently-used entries sit first.
_permission_cache: OrderedDict[str, tuple[datetime, "UserPermissions"]] = OrderedDict()
CACHE_TTL = timedelta(minutes=5)
MAX_PERMISSION_CACHE_ENTRIES = 10_000


@dataclass
class UserPermissions:
    """Container for a user's permissions and roles."""

    user_id: str
    roles: set[str] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)


def _purge_expired_permissions(now: datetime) -> None:
    """Drop lapsed entries from the front, stopping at the first live one.

    Opportunistic: entries that were pushed to the back by a recent read may
    outlive their TTL until the LRU cap reaches them. That's fine — the TTL is
    still checked on every read, so a stale entry is never served.
    """
    while _permission_cache:
        oldest_user_id = next(iter(_permission_cache))
        cached_time, _ = _permission_cache[oldest_user_id]
        if now - cached_time < CACHE_TTL:
            return
        del _permission_cache[oldest_user_id]


def _evict_permissions_over_capacity() -> None:
    """Drop least-recently-used entries until the cache fits its cap."""
    while len(_permission_cache) > MAX_PERMISSION_CACHE_ENTRIES:
        _permission_cache.popitem(last=False)


async def get_user_permissions(
    session: "AsyncSession", user_id: str | UUID
) -> UserPermissions:
    """Get all permissions and roles for a user.

    Results are cached with a TTL for performance; the cache is capped at
    ``MAX_PERMISSION_CACHE_ENTRIES`` and evicts least-recently-used entries.
    """
    user_id_str = str(user_id)
    user_id_uuid = user_id if isinstance(user_id, UUID) else UUID(user_id)

    # Check cache
    if user_id_str in _permission_cache:
        cached_time, cached_perms = _permission_cache[user_id_str]
        if datetime.now() - cached_time < CACHE_TTL:
            _permission_cache.move_to_end(user_id_str)
            return cached_perms

    # Query user with roles and permissions
    from skrift.db.models.user import User

    result = await session.execute(
        select(User)
        .where(User.id == user_id_uuid)
        .options(selectinload(User.roles).selectinload(Role.permissions))
    )
    user = result.scalar_one_or_none()

    permissions = UserPermissions(user_id=user_id_str)

    if user:
        for role in user.roles:
            permissions.roles.add(role.name)
            for role_perm in role.permissions:
                permissions.permissions.add(role_perm.permission)

    # Update cache
    cached_at = datetime.now()
    _purge_expired_permissions(cached_at)
    _permission_cache[user_id_str] = (cached_at, permissions)
    _permission_cache.move_to_end(user_id_str)
    _evict_permissions_over_capacity()

    return permissions


def invalidate_user_permissions_cache(user_id: str | UUID | None = None) -> None:
    """Invalidate cached permissions for a user or all users.

    Args:
        user_id: Specific user to invalidate, or None to clear all cache
    """
    if user_id is None:
        _permission_cache.clear()
    else:
        _permission_cache.pop(str(user_id), None)


async def assign_role_to_user(
    session: "AsyncSession", user_id: str | UUID, role_name: str
) -> bool:
    """Assign a role to a user.

    Returns True if the role was assigned, False if role not found.
    """
    from skrift.db.models.user import User

    user_id_uuid = UUID(user_id) if isinstance(user_id, str) else user_id

    # Get user and role
    user_result = await session.execute(
        select(User).where(User.id == user_id_uuid).options(selectinload(User.roles))
    )
    user = user_result.scalar_one_or_none()

    role_result = await session.execute(select(Role).where(Role.name == role_name))
    role = role_result.scalar_one_or_none()

    if not user or not role:
        return False

    # Check if already assigned
    if role not in user.roles:
        user.roles.append(role)
        await session.commit()
        invalidate_user_permissions_cache(user_id)
        await hooks.do_action("after_role_assigned", user, role)

    return True


async def remove_role_from_user(
    session: "AsyncSession", user_id: str | UUID, role_name: str
) -> bool:
    """Remove a role from a user.

    Returns True if the role was removed, False if not found.
    """
    from skrift.db.models.user import User

    user_id_uuid = UUID(user_id) if isinstance(user_id, str) else user_id

    # Get user with roles
    user_result = await session.execute(
        select(User).where(User.id == user_id_uuid).options(selectinload(User.roles))
    )
    user = user_result.scalar_one_or_none()

    if not user:
        return False

    # Find and remove the role
    for role in user.roles:
        if role.name == role_name:
            user.roles.remove(role)
            await session.commit()
            invalidate_user_permissions_cache(user_id)
            await hooks.do_action("after_role_removed", user, role)
            return True

    return False


async def sync_roles_to_database(session: "AsyncSession") -> None:
    """Sync role definitions from code to the database.

    This creates or updates roles based on the definitions in roles.py.
    """
    from skrift.auth.roles import ROLE_DEFINITIONS

    for role_def in ROLE_DEFINITIONS.values():
        # Check if role exists
        result = await session.execute(select(Role).where(Role.name == role_def.name))
        role = result.scalar_one_or_none()

        if role:
            # Update existing role
            role.display_name = role_def.display_name
            role.description = role_def.description

            # Remove old permissions
            await session.execute(
                delete(RolePermission).where(RolePermission.role_id == role.id)
            )
        else:
            # Create new role
            role = Role(
                name=role_def.name,
                display_name=role_def.display_name,
                description=role_def.description,
            )
            session.add(role)
            await session.flush()  # Get the role ID

        # Add permissions
        for permission in role_def.permissions:
            role_permission = RolePermission(role_id=role.id, permission=permission)
            session.add(role_permission)

    await session.commit()
    invalidate_user_permissions_cache()
