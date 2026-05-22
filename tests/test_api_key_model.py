from datetime import datetime, timedelta, timezone

from skrift.db.models.api_key import APIKey


def test_is_expired_handles_naive_datetime_as_utc():
    api_key = APIKey(
        user_id="user-id",
        display_name="Demo",
        key_prefix="sk_demo",
        key_hash="hash",
        expires_at=datetime.now(tz=timezone.utc).replace(tzinfo=None) + timedelta(days=1),
    )

    assert api_key.is_expired is False


def test_refresh_token_expired_handles_naive_datetime_as_utc():
    api_key = APIKey(
        user_id="user-id",
        display_name="Demo",
        key_prefix="sk_demo",
        key_hash="hash",
        refresh_token_expires_at=datetime.now(tz=timezone.utc).replace(tzinfo=None)
        + timedelta(days=1),
    )

    assert api_key.refresh_token_expired is False
