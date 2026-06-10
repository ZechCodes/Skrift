"""Seed the republish demo databases after migrations."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from skrift.auth.services import sync_roles_to_database
from skrift.config import clear_settings_cache, get_settings, set_config_path
from skrift.db.services import setting_service
from skrift.setup.state import create_setup_engine


SITE_BASE_URLS = {
    "source": "http://localhost:8093",
    "target": "http://localhost:8094",
}


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


async def main() -> None:
    site = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEMO_SITE", "source")
    if site not in {"source", "target"}:
        raise SystemExit("Usage: python -m republishdemo.bootstrap [source|target]")

    _select_config_file(site)

    engine = create_setup_engine(get_settings().db.url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_maker() as session:
            await setting_service.set_setting(
                session,
                setting_service.SETUP_COMPLETED_AT_KEY,
                datetime.now(timezone.utc).isoformat(),
            )
            await _set_default(
                session,
                setting_service.SITE_NAME_KEY,
                "Skrift Republish Source" if site == "source" else "Skrift Republish Target",
            )
            await _set_default(
                session,
                setting_service.SITE_TAGLINE_KEY,
                (
                    "Source site publishing baseline repost payloads"
                    if site == "source"
                    else "Target site accepting republished posts"
                ),
            )
            await _set_default(session, setting_service.SITE_BASE_URL_KEY, SITE_BASE_URLS[site])
            await sync_roles_to_database(session)
    finally:
        await engine.dispose()

    print(f"Seeded {site} republish demo database")


if __name__ == "__main__":
    asyncio.run(main())
