"""Email-link token + challenge helpers."""

import time
from unittest.mock import patch

import pytest

from skrift.auth.email_link import (
    EMAIL_LINK_PURPOSE,
    build_link_token,
    verify_link_token,
    _mask_email,
)
from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.tokens import create_signed_token

SECRET = "test-secret-key"


def _identity():
    return ResolvedPrimaryIdentity(
        method_key="github",
        method_type="oauth",
        subject_id="sub-1",
        email="victim@example.com",
        name="V",
        picture_url=None,
        raw_metadata={},
        provided_fields={"email"},
        email_verified=True,
    )


def test_link_token_roundtrip_carries_expected_fields():
    token = build_link_token(
        secret_key=SECRET,
        pending_auth_id="pending-abc",
        identity=_identity(),
        existing_user_id="user-42",
    )
    payload = verify_link_token(token, SECRET)

    assert payload is not None
    assert payload["purpose"] == EMAIL_LINK_PURPOSE
    assert payload["pending_auth_id"] == "pending-abc"
    assert payload["provider_key"] == "github"
    assert payload["subject_id"] == "sub-1"
    assert payload["email"] == "victim@example.com"
    assert payload["user_id_to_link"] == "user-42"
    assert "jti" in payload
    assert "exp" in payload


def test_verify_link_token_rejects_tampered_signature():
    token = build_link_token(
        secret_key=SECRET,
        pending_auth_id="p",
        identity=_identity(),
        existing_user_id="u",
    )
    # Flip one byte of the signature section
    head, sig = token.rsplit(".", 1)
    tampered = head + "." + ("A" if not sig.startswith("A") else "B") + sig[1:]
    assert verify_link_token(tampered, SECRET) is None


def test_verify_link_token_rejects_wrong_secret():
    token = build_link_token(
        secret_key=SECRET,
        pending_auth_id="p",
        identity=_identity(),
        existing_user_id="u",
    )
    assert verify_link_token(token, "other-secret") is None


def test_verify_link_token_rejects_purpose_confusion():
    """A signed token produced for a different feature (e.g. an OAuth code)
    must NOT be accepted as a link token even though the signature is valid.
    This is the defense against cross-feature token replay."""
    # Craft a syntactically-valid signed token with a different purpose.
    malicious = create_signed_token(
        {"purpose": "some_other_purpose", "pending_auth_id": "p"},
        SECRET,
        60,
    )
    assert verify_link_token(malicious, SECRET) is None


def test_mask_email_redacts_local_part():
    assert _mask_email("victim@example.com").endswith("@example.com")
    assert "v" in _mask_email("victim@example.com")
    assert "*" in _mask_email("victim@example.com")
    # Short local part still masks meaningfully
    assert _mask_email("ab@c.com") == "a*@c.com"
    # Non-email input is returned unchanged
    assert _mask_email("not-an-email") == "not-an-email"
