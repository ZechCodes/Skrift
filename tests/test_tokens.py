"""Tests for the OAuth2 token signing module."""

import time
from unittest.mock import patch

from skrift.auth.tokens import create_signed_token, verify_signed_token


class TestCreateSignedToken:
    def test_creates_token_string(self):
        token = create_signed_token({"user_id": "123"}, "secret", 300)
        assert isinstance(token, str)
        assert "." in token

    def test_token_has_two_parts(self):
        token = create_signed_token({"foo": "bar"}, "secret", 300)
        parts = token.split(".")
        assert len(parts) == 2


class TestVerifySignedToken:
    def test_valid_token(self):
        token = create_signed_token({"user_id": "123", "type": "access"}, "secret", 300)
        payload = verify_signed_token(token, "secret")
        assert payload is not None
        assert payload["user_id"] == "123"
        assert payload["type"] == "access"
        assert "exp" in payload

    def test_wrong_secret_returns_none(self):
        token = create_signed_token({"user_id": "123"}, "secret", 300)
        result = verify_signed_token(token, "wrong-secret")
        assert result is None

    def test_expired_token_returns_none(self):
        token = create_signed_token({"user_id": "123"}, "secret", 1)
        with patch("skrift.auth.tokens.time") as mock_time:
            # Creation time
            mock_time.time.return_value = time.time() + 10
            result = verify_signed_token(token, "secret")
        assert result is None

    def test_tampered_payload_returns_none(self):
        token = create_signed_token({"user_id": "123"}, "secret", 300)
        # Tamper with the payload part
        parts = token.split(".")
        tampered = "x" + parts[0][1:]
        result = verify_signed_token(f"{tampered}.{parts[1]}", "secret")
        assert result is None

    def test_malformed_token_returns_none(self):
        assert verify_signed_token("not-a-token", "secret") is None
        assert verify_signed_token("", "secret") is None
        assert verify_signed_token("a.b.c", "secret") is None

    def test_type_field_prevents_confusion(self):
        """Auth codes can't be used as access tokens by checking type."""
        code_token = create_signed_token(
            {"user_id": "123", "type": "code"}, "secret", 300
        )
        payload = verify_signed_token(code_token, "secret")
        assert payload is not None
        assert payload["type"] == "code"
        # Caller would check payload["type"] != "access" and reject

    def test_preserves_all_payload_fields(self):
        original = {
            "user_id": "u1",
            "email": "test@example.com",
            "name": "Test User",
            "client_id": "client-abc",
            "type": "code",
        }
        token = create_signed_token(original, "secret", 300)
        payload = verify_signed_token(token, "secret")
        assert payload is not None
        for key, value in original.items():
            assert payload[key] == value
