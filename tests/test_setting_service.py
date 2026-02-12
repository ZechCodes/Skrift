"""Tests for the setting service module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from skrift.db.models import Setting
from skrift.db.services import setting_service
from skrift.db.services.setting_service import (
    SITE_BASE_URL_KEY,
    SITE_COPYRIGHT_HOLDER_KEY,
    SITE_COPYRIGHT_START_YEAR_KEY,
    SITE_DEFAULTS,
    SITE_NAME_KEY,
    SITE_TAGLINE_KEY,
    _site_settings_lock,
    delete_setting,
    get_cached_site_base_url,
    get_cached_site_copyright_holder,
    get_cached_site_copyright_start_year,
    get_cached_site_name,
    get_cached_site_tagline,
    get_setting,
    get_setting_with_default,
    get_settings,
    get_site_settings,
    invalidate_site_settings_cache,
    load_site_settings_cache,
    set_setting,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the site settings cache before and after each test.

    We access via the module attribute to ensure we're always working with the
    same dict the production code sees, even if load_site_settings_cache
    reassigns it.
    """
    setting_service._site_settings_cache.clear()
    yield
    setting_service._site_settings_cache.clear()


def _make_setting(key: str, value: str | None = None) -> MagicMock:
    """Create a mock Setting object with the given key and value."""
    mock = MagicMock(spec=Setting)
    mock.key = key
    mock.value = value
    return mock


def _mock_db_session() -> AsyncMock:
    """Create a mock async database session with a fluent execute interface."""
    session = AsyncMock()
    # Make session.add a regular MagicMock so it doesn't produce coroutine warnings
    session.add = MagicMock()
    return session


# ---------------------------------------------------------------------------
# get_setting
# ---------------------------------------------------------------------------


class TestGetSetting:
    @pytest.mark.asyncio
    async def test_found(self):
        """get_setting returns the value when the key exists."""
        mock_setting = _make_setting("my_key", "my_value")
        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_setting
        db.execute.return_value = result_mock

        result = await get_setting(db, "my_key")

        assert result == "my_value"
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found(self):
        """get_setting returns None when the key does not exist."""
        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        result = await get_setting(db, "missing_key")

        assert result is None


# ---------------------------------------------------------------------------
# get_setting_with_default
# ---------------------------------------------------------------------------


class TestGetSettingWithDefault:
    @pytest.mark.asyncio
    async def test_found_returns_value(self):
        """get_setting_with_default returns the stored value when it exists."""
        db = _mock_db_session()
        result_mock = MagicMock()
        mock_setting = _make_setting("color", "blue")
        result_mock.scalar_one_or_none.return_value = mock_setting
        db.execute.return_value = result_mock

        result = await get_setting_with_default(db, "color", "red")

        assert result == "blue"

    @pytest.mark.asyncio
    async def test_not_found_returns_default(self):
        """get_setting_with_default returns the default when the key is missing."""
        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        result = await get_setting_with_default(db, "missing", "fallback")

        assert result == "fallback"


# ---------------------------------------------------------------------------
# get_settings
# ---------------------------------------------------------------------------


class TestGetSettings:
    @pytest.mark.asyncio
    async def test_with_keys_filter(self):
        """get_settings returns only the requested keys."""
        s1 = _make_setting("a", "1")
        s2 = _make_setting("b", "2")

        db = _mock_db_session()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [s1, s2]
        result_mock.scalars.return_value = scalars_mock
        db.execute.return_value = result_mock

        result = await get_settings(db, keys=["a", "b"])

        assert result == {"a": "1", "b": "2"}
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_filter(self):
        """get_settings returns all settings when no keys are specified."""
        s1 = _make_setting("x", "10")
        s2 = _make_setting("y", "20")
        s3 = _make_setting("z", "30")

        db = _mock_db_session()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [s1, s2, s3]
        result_mock.scalars.return_value = scalars_mock
        db.execute.return_value = result_mock

        result = await get_settings(db)

        assert result == {"x": "10", "y": "20", "z": "30"}

    @pytest.mark.asyncio
    async def test_empty_result(self):
        """get_settings returns an empty dict when no settings exist."""
        db = _mock_db_session()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock.scalars.return_value = scalars_mock
        db.execute.return_value = result_mock

        result = await get_settings(db, keys=["nonexistent"])

        assert result == {}


# ---------------------------------------------------------------------------
# set_setting
# ---------------------------------------------------------------------------


class TestSetSetting:
    @pytest.mark.asyncio
    async def test_create_new(self):
        """set_setting creates a new Setting when the key does not exist."""
        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        returned = await set_setting(db, "new_key", "new_value")

        # A new Setting should have been added to the session
        db.add.assert_called_once()
        added_obj = db.add.call_args[0][0]
        assert isinstance(added_obj, Setting)
        assert added_obj.key == "new_key"
        assert added_obj.value == "new_value"

        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_existing(self):
        """set_setting updates the value when the key already exists."""
        existing = _make_setting("existing_key", "old_value")

        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        db.execute.return_value = result_mock

        returned = await set_setting(db, "existing_key", "updated_value")

        # The existing setting's value should be updated, not a new one added
        assert existing.value == "updated_value"
        db.add.assert_not_called()
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# delete_setting
# ---------------------------------------------------------------------------


class TestDeleteSetting:
    @pytest.mark.asyncio
    async def test_found_and_deleted(self):
        """delete_setting returns True and deletes the setting when found."""
        existing = _make_setting("to_delete", "value")

        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = existing
        db.execute.return_value = result_mock

        result = await delete_setting(db, "to_delete")

        assert result is True
        db.delete.assert_awaited_once_with(existing)
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found(self):
        """delete_setting returns False when the key does not exist."""
        db = _mock_db_session()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        result = await delete_setting(db, "nonexistent")

        assert result is False
        db.delete.assert_not_awaited()
        db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_site_settings
# ---------------------------------------------------------------------------


class TestGetSiteSettings:
    @pytest.mark.asyncio
    async def test_defaults_applied_for_missing_keys(self):
        """get_site_settings fills in defaults for keys not found in the DB."""
        # Simulate only site_name being stored in the DB
        s1 = _make_setting(SITE_NAME_KEY, "Custom Name")

        db = _mock_db_session()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [s1]
        result_mock.scalars.return_value = scalars_mock
        db.execute.return_value = result_mock

        result = await get_site_settings(db)

        assert result[SITE_NAME_KEY] == "Custom Name"
        assert result[SITE_TAGLINE_KEY] == SITE_DEFAULTS[SITE_TAGLINE_KEY]
        assert result[SITE_COPYRIGHT_HOLDER_KEY] == SITE_DEFAULTS[SITE_COPYRIGHT_HOLDER_KEY]
        assert result[SITE_COPYRIGHT_START_YEAR_KEY] == SITE_DEFAULTS[SITE_COPYRIGHT_START_YEAR_KEY]
        assert result[SITE_BASE_URL_KEY] == SITE_DEFAULTS[SITE_BASE_URL_KEY]

    @pytest.mark.asyncio
    async def test_all_keys_present(self):
        """get_site_settings returns stored values when all keys are present."""
        stored = {
            SITE_NAME_KEY: "My Blog",
            SITE_TAGLINE_KEY: "A great blog",
            SITE_COPYRIGHT_HOLDER_KEY: "Me",
            SITE_COPYRIGHT_START_YEAR_KEY: "2024",
            SITE_BASE_URL_KEY: "https://example.com",
        }
        settings_list = [_make_setting(k, v) for k, v in stored.items()]

        db = _mock_db_session()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = settings_list
        result_mock.scalars.return_value = scalars_mock
        db.execute.return_value = result_mock

        result = await get_site_settings(db)

        for key, value in stored.items():
            assert result[key] == value

    @pytest.mark.asyncio
    async def test_empty_string_value_falls_back_to_default(self):
        """get_site_settings uses the default when a stored value is an empty string.

        The implementation uses `or`, so falsy values (empty string) fall back to defaults.
        """
        s1 = _make_setting(SITE_NAME_KEY, "")

        db = _mock_db_session()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [s1]
        result_mock.scalars.return_value = scalars_mock
        db.execute.return_value = result_mock

        result = await get_site_settings(db)

        # Empty string is falsy, so the default should be used
        assert result[SITE_NAME_KEY] == SITE_DEFAULTS[SITE_NAME_KEY]


# ---------------------------------------------------------------------------
# load_site_settings_cache
# ---------------------------------------------------------------------------


class TestLoadSiteSettingsCache:
    @pytest.mark.asyncio
    async def test_success(self):
        """load_site_settings_cache populates the cache from the database."""
        expected = {
            SITE_NAME_KEY: "Loaded Name",
            SITE_TAGLINE_KEY: "Loaded Tagline",
            SITE_COPYRIGHT_HOLDER_KEY: "Owner",
            SITE_COPYRIGHT_START_YEAR_KEY: "2020",
            SITE_BASE_URL_KEY: "https://loaded.example.com",
        }
        db = _mock_db_session()

        with patch.object(
            setting_service, "get_site_settings", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = expected
            await load_site_settings_cache(db)

        assert setting_service._site_settings_cache == expected

    @pytest.mark.asyncio
    async def test_table_not_found_operational_error(self):
        """load_site_settings_cache falls back to defaults on OperationalError."""
        db = _mock_db_session()

        with patch.object(
            setting_service,
            "get_site_settings",
            new_callable=AsyncMock,
            side_effect=OperationalError("no such table", {}, None),
        ):
            await load_site_settings_cache(db)

        assert setting_service._site_settings_cache == SITE_DEFAULTS

    @pytest.mark.asyncio
    async def test_table_not_found_programming_error(self):
        """load_site_settings_cache falls back to defaults on ProgrammingError."""
        db = _mock_db_session()

        with patch.object(
            setting_service,
            "get_site_settings",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("relation does not exist", {}, None),
        ):
            await load_site_settings_cache(db)

        assert setting_service._site_settings_cache == SITE_DEFAULTS


# ---------------------------------------------------------------------------
# invalidate_site_settings_cache
# ---------------------------------------------------------------------------


class TestInvalidateSiteSettingsCache:
    def test_clears_cache(self):
        """invalidate_site_settings_cache empties the cache dict."""
        setting_service._site_settings_cache["some_key"] = "some_value"
        setting_service._site_settings_cache["another_key"] = "another_value"

        invalidate_site_settings_cache()

        assert setting_service._site_settings_cache == {}

    def test_clears_already_empty_cache(self):
        """invalidate_site_settings_cache does not error on an empty cache."""
        assert setting_service._site_settings_cache == {}
        invalidate_site_settings_cache()
        assert setting_service._site_settings_cache == {}


# ---------------------------------------------------------------------------
# Cached getters
# ---------------------------------------------------------------------------


class TestGetCachedSiteName:
    def test_returns_cached_value(self):
        """get_cached_site_name returns the cached value when present."""
        setting_service._site_settings_cache[SITE_NAME_KEY] = "Cached Name"
        assert get_cached_site_name() == "Cached Name"

    def test_returns_default_when_cache_empty(self):
        """get_cached_site_name returns the default when the cache is empty."""
        assert get_cached_site_name() == SITE_DEFAULTS[SITE_NAME_KEY]


class TestGetCachedSiteTagline:
    def test_returns_cached_value(self):
        """get_cached_site_tagline returns the cached value when present."""
        setting_service._site_settings_cache[SITE_TAGLINE_KEY] = "Cached Tagline"
        assert get_cached_site_tagline() == "Cached Tagline"

    def test_returns_default_when_cache_empty(self):
        """get_cached_site_tagline returns the default when the cache is empty."""
        assert get_cached_site_tagline() == SITE_DEFAULTS[SITE_TAGLINE_KEY]


class TestGetCachedSiteCopyrightHolder:
    def test_returns_cached_value(self):
        """get_cached_site_copyright_holder returns the cached value when present."""
        setting_service._site_settings_cache[SITE_COPYRIGHT_HOLDER_KEY] = "ACME Corp"
        assert get_cached_site_copyright_holder() == "ACME Corp"

    def test_returns_default_when_cache_empty(self):
        """get_cached_site_copyright_holder returns the default when the cache is empty."""
        assert get_cached_site_copyright_holder() == SITE_DEFAULTS[SITE_COPYRIGHT_HOLDER_KEY]


class TestGetCachedSiteCopyrightStartYear:
    def test_returns_int_when_digit_string(self):
        """get_cached_site_copyright_start_year converts a digit string to int."""
        setting_service._site_settings_cache[SITE_COPYRIGHT_START_YEAR_KEY] = "2024"
        result = get_cached_site_copyright_start_year()
        assert result == 2024
        assert isinstance(result, int)

    def test_returns_none_when_empty_string(self):
        """get_cached_site_copyright_start_year returns None for an empty string."""
        setting_service._site_settings_cache[SITE_COPYRIGHT_START_YEAR_KEY] = ""
        assert get_cached_site_copyright_start_year() is None

    def test_returns_none_when_non_digit(self):
        """get_cached_site_copyright_start_year returns None for a non-digit string."""
        setting_service._site_settings_cache[SITE_COPYRIGHT_START_YEAR_KEY] = "not-a-year"
        assert get_cached_site_copyright_start_year() is None

    def test_returns_none_when_cache_empty(self):
        """get_cached_site_copyright_start_year returns None when cache has no entry.

        The default for SITE_COPYRIGHT_START_YEAR_KEY is an empty string, which is
        falsy, so the function returns None.
        """
        assert get_cached_site_copyright_start_year() is None


class TestGetCachedSiteBaseUrl:
    def test_returns_cached_value(self):
        """get_cached_site_base_url returns the cached value when present."""
        setting_service._site_settings_cache[SITE_BASE_URL_KEY] = "https://example.com"
        assert get_cached_site_base_url() == "https://example.com"

    def test_returns_default_when_cache_empty(self):
        """get_cached_site_base_url returns the default when the cache is empty."""
        assert get_cached_site_base_url() == SITE_DEFAULTS[SITE_BASE_URL_KEY]


# ---------------------------------------------------------------------------
# _site_settings_lock
# ---------------------------------------------------------------------------


class TestSiteSettingsLock:
    def test_lock_is_asyncio_lock(self):
        """_site_settings_lock should be an asyncio.Lock instance."""
        assert isinstance(_site_settings_lock, asyncio.Lock)
