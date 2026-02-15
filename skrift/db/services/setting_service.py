"""Setting service for CRUD operations on site settings."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models import Setting

# In-memory cache for site settings (avoids DB queries on every page render)
_site_settings_cache: dict[str, str] = {}


async def get_setting(
    db_session: AsyncSession,
    key: str,
) -> str | None:
    """Get a setting value by key.

    Args:
        db_session: Database session
        key: Setting key

    Returns:
        Setting value or None if not found
    """
    result = await db_session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def get_setting_with_default(
    db_session: AsyncSession,
    key: str,
    default: str,
) -> str:
    """Get a setting value by key, returning a default if not found.

    Args:
        db_session: Database session
        key: Setting key
        default: Default value if setting doesn't exist

    Returns:
        Setting value or default
    """
    value = await get_setting(db_session, key)
    return value if value is not None else default


async def get_settings(
    db_session: AsyncSession,
    keys: list[str] | None = None,
) -> dict[str, str | None]:
    """Get multiple settings as a dictionary.

    Args:
        db_session: Database session
        keys: Optional list of keys to retrieve. If None, returns all settings.

    Returns:
        Dictionary of key-value pairs
    """
    query = select(Setting)
    if keys:
        query = query.where(Setting.key.in_(keys))

    result = await db_session.execute(query)
    settings = result.scalars().all()
    return {s.key: s.value for s in settings}


async def set_setting(
    db_session: AsyncSession,
    key: str,
    value: str | None,
) -> Setting:
    """Set a setting value, creating or updating as needed.

    Args:
        db_session: Database session
        key: Setting key
        value: Setting value (can be None)

    Returns:
        The created or updated Setting object
    """
    result = await db_session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = value
    else:
        setting = Setting(key=key, value=value)
        db_session.add(setting)

    await db_session.commit()
    await db_session.refresh(setting)
    return setting


async def delete_setting(
    db_session: AsyncSession,
    key: str,
) -> bool:
    """Delete a setting by key.

    Args:
        db_session: Database session
        key: Setting key to delete

    Returns:
        True if deleted, False if not found
    """
    result = await db_session.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()

    if not setting:
        return False

    await db_session.delete(setting)
    await db_session.commit()
    return True


# Site setting keys
SITE_NAME_KEY = "site_name"
SITE_TAGLINE_KEY = "site_tagline"
SITE_COPYRIGHT_HOLDER_KEY = "site_copyright_holder"
SITE_COPYRIGHT_START_YEAR_KEY = "site_copyright_start_year"
SITE_BASE_URL_KEY = "site_base_url"

# Setup wizard key
SETUP_COMPLETED_AT_KEY = "setup_completed_at"

# Default values
SITE_DEFAULTS = {
    SITE_NAME_KEY: "My Site",
    SITE_TAGLINE_KEY: "Welcome to my site",
    SITE_COPYRIGHT_HOLDER_KEY: "",
    SITE_COPYRIGHT_START_YEAR_KEY: "",
    SITE_BASE_URL_KEY: "",
}


async def get_site_settings(db_session: AsyncSession) -> dict[str, str]:
    """Get all site settings with defaults applied.

    Args:
        db_session: Database session

    Returns:
        Dictionary with site settings, using defaults for missing values
    """
    keys = list(SITE_DEFAULTS.keys())
    settings = await get_settings(db_session, keys)

    return {
        key: settings.get(key) or default
        for key, default in SITE_DEFAULTS.items()
    }


async def load_site_settings_cache(db_session: AsyncSession) -> None:
    """Load site settings into the in-memory cache.

    Call this on application startup to populate the cache.
    If the settings table doesn't exist yet (before migration), uses defaults.

    Args:
        db_session: Database session
    """
    global _site_settings_cache
    try:
        _site_settings_cache = await get_site_settings(db_session)
    except Exception:
        # Table might not exist yet (before migration), use defaults
        _site_settings_cache = SITE_DEFAULTS.copy()


def invalidate_site_settings_cache() -> None:
    """Clear the site settings cache.

    Call this when settings are modified to ensure fresh values are loaded.
    """
    global _site_settings_cache
    _site_settings_cache.clear()


def _get_cached_setting(key: str) -> str:
    """Get a cached setting value, falling back to its default."""
    return _site_settings_cache.get(key, SITE_DEFAULTS.get(key, ""))


def get_cached_site_name() -> str:
    """Get the cached site name for use in templates."""
    return _get_cached_setting(SITE_NAME_KEY)


def get_cached_site_tagline() -> str:
    """Get the cached site tagline for use in templates."""
    return _get_cached_setting(SITE_TAGLINE_KEY)


def get_cached_site_copyright_holder() -> str:
    """Get the cached site copyright holder for use in templates."""
    return _get_cached_setting(SITE_COPYRIGHT_HOLDER_KEY)


def get_cached_site_copyright_start_year() -> str | int | None:
    """Get the cached site copyright start year for use in templates."""
    value = _get_cached_setting(SITE_COPYRIGHT_START_YEAR_KEY)
    if value and value.isdigit():
        return int(value)
    return None


def get_cached_site_base_url() -> str:
    """Get the cached site base URL for use in SEO/sitemap."""
    return _get_cached_setting(SITE_BASE_URL_KEY)
