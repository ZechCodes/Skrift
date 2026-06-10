from datetime import datetime, timedelta, timezone

from skrift.db.models.api_key import APIKey


def test_constraint_data_parses_json():
    api_key = APIKey(
        user_id="user-id",
        display_name="Demo",
        key_prefix="sk_demo",
        key_hash="hash",
        constraints='{"republish": {"source_origin": "https://source.example"}}',
    )

    assert api_key.constraint_data == {
        "republish": {"source_origin": "https://source.example"}
    }


def test_constraint_data_returns_empty_dict_for_invalid_json():
    api_key = APIKey(
        user_id="user-id",
        display_name="Demo",
        key_prefix="sk_demo",
        key_hash="hash",
        constraints="not-json",
    )

    assert api_key.constraint_data == {}


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
