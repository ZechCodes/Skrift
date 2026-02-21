"""Setting service for CRUD operations on site settings."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models import Setting

# In-memory cache for site settings (avoids DB queries on every page render)
_site_settings_cache: dict[str, str] = {}
# Per-subdomain overrides: subdomain → {key → value}
_per_site_cache: dict[str, dict[str, str]] = {}


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

# Keys that can be overridden per subdomain
PER_SITE_KEYS = frozenset({SITE_NAME_KEY, SITE_TAGLINE_KEY})


def site_scoped_key(subdomain: str, key: str) -> str:
    """Build a namespaced DB key for a per-subdomain setting."""
    return f"site:{subdomain}:{key}"


SITE_COPYRIGHT_HOLDER_KEY = "site_copyright_holder"
SITE_COPYRIGHT_START_YEAR_KEY = "site_copyright_start_year"
SITE_BASE_URL_KEY = "site_base_url"
SITE_THEME_KEY = "site_theme"

# Setup wizard key
SETUP_COMPLETED_AT_KEY = "setup_completed_at"

# Robots.txt custom content key
ROBOTS_TXT_KEY = "robots_txt"

# Default values
SITE_DEFAULTS = {
    SITE_NAME_KEY: "My Site",
    SITE_TAGLINE_KEY: "Welcome to my site",
    SITE_COPYRIGHT_HOLDER_KEY: "",
    SITE_COPYRIGHT_START_YEAR_KEY: "",
    SITE_BASE_URL_KEY: "",
    SITE_THEME_KEY: "",
    ROBOTS_TXT_KEY: "",
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

    Also loads per-subdomain overrides (keys matching ``site:*:*``).

    Args:
        db_session: Database session
    """
    global _site_settings_cache, _per_site_cache
    try:
        _site_settings_cache = await get_site_settings(db_session)

        # Load all per-site overrides
        all_settings = await get_settings(db_session)
        per_site: dict[str, dict[str, str]] = {}
        for db_key, value in all_settings.items():
            if db_key.startswith("site:") and value:
                parts = db_key.split(":", 2)
                if len(parts) == 3:
                    _, subdomain, setting_key = parts
                    per_site.setdefault(subdomain, {})[setting_key] = value
        _per_site_cache = per_site
    except Exception:
        # Table might not exist yet (before migration), leave cache empty so
        # the next access retries instead of being stuck on cached defaults.
        _site_settings_cache.clear()
        _per_site_cache.clear()


def invalidate_site_settings_cache() -> None:
    """Clear the site settings cache.

    Call this when settings are modified to ensure fresh values are loaded.
    """
    global _site_settings_cache, _per_site_cache
    _site_settings_cache.clear()
    _per_site_cache.clear()


def site_settings_cache_loaded() -> bool:
    """Return True if the settings cache was successfully loaded from the DB."""
    return bool(_site_settings_cache)


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


def get_cached_site_theme() -> str:
    """Get the cached site theme name for use in template resolution.

    Falls back to the app.yaml ``theme`` setting when the database
    cache has not been populated or the DB has no theme configured.
    """
    theme = _get_cached_setting(SITE_THEME_KEY)
    if theme:
        return theme

    try:
        from skrift.config import get_settings
        return get_settings().theme
    except Exception:
        return ""


# --- Per-subdomain accessors ---


def get_cached_site_setting(key: str, subdomain: str | None = None) -> str:
    """Get a cached setting, checking per-site override first then global."""
    if subdomain:
        override = _per_site_cache.get(subdomain, {}).get(key)
        if override:
            return override
    return _get_cached_setting(key)


def get_cached_site_name_for(subdomain: str | None = None) -> str:
    """Get the cached site name, with per-subdomain override support."""
    return get_cached_site_setting(SITE_NAME_KEY, subdomain)


def get_cached_site_tagline_for(subdomain: str | None = None) -> str:
    """Get the cached site tagline, with per-subdomain override support."""
    return get_cached_site_setting(SITE_TAGLINE_KEY, subdomain)


# --- Per-subdomain DB functions ---


async def get_site_settings_for_subdomain(
    db_session: AsyncSession, subdomain: str
) -> dict[str, str]:
    """Get settings for a subdomain, merging per-site overrides over global defaults.

    Returns a dict with the same shape as ``get_site_settings()`` but with
    ``site_name`` and ``site_tagline`` replaced by per-site values when present.
    """
    global_settings = await get_site_settings(db_session)

    for key in PER_SITE_KEYS:
        scoped = await get_setting(db_session, site_scoped_key(subdomain, key))
        if scoped is not None:
            global_settings[key] = scoped

    return global_settings


async def set_site_setting_for_subdomain(
    db_session: AsyncSession, subdomain: str, key: str, value: str
) -> None:
    """Save a per-subdomain setting override."""
    await set_setting(db_session, site_scoped_key(subdomain, key), value)


def get_cached_robots_txt() -> str:
    """Get the cached robots.txt custom content (empty string = use default)."""
    return _get_cached_setting(ROBOTS_TXT_KEY)
