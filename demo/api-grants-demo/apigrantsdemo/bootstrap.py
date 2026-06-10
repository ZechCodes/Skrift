"""Seed the API grants demo databases after migrations."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apigrantsdemo.seed_values import SEEDED_REFRESH_TOKEN, SEEDED_SERVICE_KEY
from skrift.auth.services import assign_role_to_user, sync_roles_to_database
from skrift.config import clear_settings_cache, get_settings, set_config_path
from skrift.db.models.api_key import APIKey
from skrift.db.models.user import User
from skrift.db.services import setting_service
from skrift.setup.state import create_setup_engine


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _select_config_file(site: str) -> None:
    path = Path.cwd() / f"{site}.app.yaml"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    set_config_path(path)
    clear_settings_cache()


async def _set_default(session, key: str, value: str) -> None:
    existing = await setting_service.get_setting(session, key)
    if existing is None:
        await setting_service.set_setting(session, key, value)


async def _seed_provider(session) -> None:
    import apigrantsdemo.provider  # noqa: F401
    from apigrantsdemo.permissions import DEMO_ROLE, PERM_ELEVATED, PERM_KNOWN

    await sync_roles_to_database(session)

    result = await session.execute(select(User).where(User.email == "service-owner@example.test"))
    owner = result.scalar_one_or_none()
    if owner is None:
        owner = User(
            email="service-owner@example.test",
            name="Seeded Service Owner",
            is_active=True,
        )
        session.add(owner)
        await session.commit()

    await assign_role_to_user(session, owner.id, DEMO_ROLE)

    result = await session.execute(select(APIKey).where(APIKey.key_hash == _hash(SEEDED_SERVICE_KEY)))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        api_key = APIKey(
            user_id=owner.id,
            display_name="Seeded Known Service",
            description="Fixed API key used by the API grants demo client.",
            key_prefix=SEEDED_SERVICE_KEY[:12],
            key_hash=_hash(SEEDED_SERVICE_KEY),
            scoped_permissions=f"{PERM_KNOWN}\n{PERM_ELEVATED}",
            principal_type="service",
            service_name="Seeded Known Service",
            service_url="http://localhost:8092",
            grant_source="demo-seed",
            is_active=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
            refresh_token_hash=_hash(SEEDED_REFRESH_TOKEN),
            refresh_token_expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
        )
        session.add(api_key)
    else:
        api_key.user_id = owner.id
        api_key.scoped_permissions = f"{PERM_KNOWN}\n{PERM_ELEVATED}"
        api_key.principal_type = "service"
        api_key.service_name = "Seeded Known Service"
        api_key.service_url = "http://localhost:8092"
        api_key.grant_source = "demo-seed"
        api_key.is_active = True
    await session.commit()


async def main() -> None:
    site = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEMO_SITE", "provider")
    if site not in {"provider", "client"}:
        raise SystemExit("Usage: python -m apigrantsdemo.bootstrap [provider|client]")

    _select_config_file(site)

    engine = create_setup_engine(get_settings().db.url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_maker() as session:
            timestamp = datetime.now(timezone.utc).isoformat()
            await setting_service.set_setting(
                session,
                setting_service.SETUP_COMPLETED_AT_KEY,
                timestamp,
            )

            if site == "provider":
                await _set_default(session, setting_service.SITE_NAME_KEY, "Skrift Grant Provider")
                await _set_default(
                    session,
                    setting_service.SITE_TAGLINE_KEY,
                    "API permission grant provider",
                )
                await _set_default(session, setting_service.SITE_BASE_URL_KEY, "http://localhost:8091")
                await _seed_provider(session)
            else:
                await _set_default(session, setting_service.SITE_NAME_KEY, "Skrift Grant Client")
                await _set_default(
                    session,
                    setting_service.SITE_TAGLINE_KEY,
                    "Client site requesting provider API access",
                )
                await _set_default(session, setting_service.SITE_BASE_URL_KEY, "http://localhost:8092")
    finally:
        await engine.dispose()

    print(f"Seeded {site} API grants demo database")


if __name__ == "__main__":
    asyncio.run(main())
