"""Tests for OAuth2 client_secret hashing (H2)."""

import hmac
from unittest.mock import patch

import pytest

from skrift.auth.client_secret import (
    hash_client_secret,
    is_hashed,
    verify_client_secret,
)


def test_hash_client_secret_produces_prefixed_format():
    stored = hash_client_secret("some-secret-value")
    assert stored.startswith("sha256$")
    assert stored.count("$") == 2


def test_hash_client_secret_salt_is_unique_per_call():
    a = hash_client_secret("same-secret")
    b = hash_client_secret("same-secret")
    assert a != b, "each hash should use a fresh random salt"


def test_hash_client_secret_rejects_empty():
    with pytest.raises(ValueError):
        hash_client_secret("")


def test_verify_client_secret_roundtrip():
    plaintext = "abc123_XYZ-789"
    stored = hash_client_secret(plaintext)
    assert verify_client_secret(plaintext, stored) is True


def test_verify_client_secret_rejects_mismatch():
    stored = hash_client_secret("right")
    assert verify_client_secret("wrong", stored) is False


def test_verify_client_secret_rejects_empty_inputs():
    stored = hash_client_secret("ok")
    assert verify_client_secret("", stored) is False
    assert verify_client_secret("ok", "") is False


def test_verify_client_secret_rejects_legacy_plaintext():
    """A stored value without the ``sha256$`` prefix is treated as unrecoverable
    legacy data and never matches — the alembic migration is the single source
    of re-hashing, so legacy values should never slip through verification."""
    assert verify_client_secret("plain", "plain") is False


def test_verify_client_secret_rejects_malformed_hash():
    assert verify_client_secret("ok", "sha256$no-hash-part") is False
    assert verify_client_secret("ok", "sha256$@@@@$@@@@") is False
    assert verify_client_secret("ok", "md5$abc$def") is False


def test_verify_client_secret_uses_constant_time_compare():
    """Pin the implementation to ``hmac.compare_digest`` so a future refactor
    cannot silently reintroduce a short-circuiting comparison."""
    stored = hash_client_secret("secret")
    with patch(
        "skrift.auth.client_secret.hmac.compare_digest", wraps=hmac.compare_digest
    ) as spy:
        verify_client_secret("secret", stored)
        spy.assert_called_once()


def test_is_hashed_detects_prefix():
    assert is_hashed(hash_client_secret("x")) is True
    assert is_hashed("plain") is False
    assert is_hashed("") is False
    assert is_hashed("sha256$only-one-part") is True  # prefix-based, migration still needs to parse
