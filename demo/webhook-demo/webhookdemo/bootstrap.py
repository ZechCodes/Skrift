"""Bootstrap the webhook demo database after migrations."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from skrift.config import clear_settings_cache, get_settings, set_config_path
from skrift.db.services import setting_service
from skrift.setup.state import create_setup_engine


def _select_config_file() -> None:
    """Use the demo compose config when running under compose."""

    cwd = Path.cwd()
    candidates: list[Path] = []
    if os.environ.get("SKRIFT_ENV") == "compose":
        candidates.append(cwd / "compose.app.yaml")
    candidates.append(cwd / "app.yaml")

    for path in candidates:
        if path.exists():
            set_config_path(path)
            clear_settings_cache()
            return


async def _set_default(session, key: str, value: str) -> None:
    existing = await setting_service.get_setting(session, key)
    if existing is None:
        await setting_service.set_setting(session, key, value)


async def main() -> None:
    _select_config_file()

    db_url = os.environ.get("DATABASE_URL") or get_settings().db.url
    engine = create_setup_engine(db_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_maker() as session:
            timestamp = datetime.now(timezone.utc).isoformat()
            await setting_service.set_setting(
                session,
                setting_service.SETUP_COMPLETED_AT_KEY,
                timestamp,
            )
            await _set_default(session, setting_service.SITE_NAME_KEY, "Skrift Webhook Demo")
            await _set_default(
                session,
                setting_service.SITE_TAGLINE_KEY,
                "Outbound webhook delivery demo",
            )
            await _set_default(session, setting_service.SITE_BASE_URL_KEY, "http://localhost:8084")
    finally:
        await engine.dispose()

    print("Seeded Skrift webhook demo settings")


if __name__ == "__main__":
    asyncio.run(main())
