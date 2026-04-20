"""C2: OAuth2 client_secret comparison must be constant-time.

We can't measure timing directly in unit tests, but we can pin the
implementation to :func:`hmac.compare_digest` so a future refactor cannot
silently reintroduce byte-wise ``!=``.
"""

import hmac
from unittest.mock import patch

import pytest

from skrift.controllers.oauth2 import _verify_client_secret


def test_verify_client_secret_matches_equal_strings():
    assert _verify_client_secret("abc", "abc") is True


def test_verify_client_secret_rejects_mismatch():
    assert _verify_client_secret("abc", "abd") is False


def test_verify_client_secret_rejects_empty_submitted():
    assert _verify_client_secret("", "abc") is False


def test_verify_client_secret_calls_hmac_compare_digest():
    with patch(
        "skrift.controllers.oauth2.hmac.compare_digest", wraps=hmac.compare_digest
    ) as spy:
        _verify_client_secret("abc123", "abc124")
        spy.assert_called_once()
