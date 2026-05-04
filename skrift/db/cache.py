"""Request-scoped primary-key cache helpers for database sessions."""

from __future__ import annotations

from collections.abc import Hashable
from typing import TypeVar, cast

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.base import Base


ModelT = TypeVar("ModelT", bound=Base)

CACHE_KEY = "skrift_pk_cache"


async def get_by_pk(
    db_session: AsyncSession,
    model: type[ModelT],
    pk: Hashable,
) -> ModelT | None:
    """Return a model instance by primary key, cached for this session/request."""
    cache = db_session.info.setdefault(CACHE_KEY, {})
    key = (model, pk)
    if key in cache:
        return cast(ModelT | None, cache[key])

    obj = await db_session.get(model, pk)
    cache[key] = obj
    return obj


def seed_instance(db_session: AsyncSession, obj: Base) -> None:
    """Seed the request cache with an already-loaded model instance."""
    identity = inspect(obj).identity
    if identity is not None and len(identity) == 1:
        db_session.info.setdefault(CACHE_KEY, {})[(type(obj), identity[0])] = obj


def evict_pk(db_session: AsyncSession, model: type[Base], pk: Hashable) -> None:
    """Remove a cached primary-key entry from this session/request."""
    cache = db_session.info.get(CACHE_KEY)
    if cache is not None:
        cache.pop((model, pk), None)
