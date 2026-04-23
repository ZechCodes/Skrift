"""H4: the passkey RP ID must never be derived from an untrusted Host header.

These tests pin the configuration invariants:

1. ``_resolve_rp_id`` no longer falls back to ``request.url.hostname``.
2. ``validate_passkey_origin_config`` raises unless ``settings.domain`` or
   ``auth.redirect_base_url`` supplies a hostname.
"""

from unittest.mock import MagicMock

import pytest

from skrift.auth.second_factors.passkey_service import (
    PasskeyStateError,
    _resolve_origin,
    _resolve_rp_id,
    validate_passkey_origin_config,
)


def _settings(*, domain: str = "", redirect_base_url: str = ""):
    settings = MagicMock()
    settings.domain = domain
    settings.auth.redirect_base_url = redirect_base_url
    return settings


def test_resolve_rp_id_uses_configured_domain_first():
    request = MagicMock()
    request.url.hostname = "attacker.example"  # would be used by old fallback
    request.base_url = "https://attacker.example"
    rp_id = _resolve_rp_id(request, _settings(domain="app.example"))
    assert rp_id == "app.example"


def test_resolve_rp_id_uses_redirect_base_url_when_domain_unset():
    request = MagicMock()
    request.url.hostname = "attacker.example"
    request.base_url = "https://attacker.example"
    rp_id = _resolve_rp_id(
        request, _settings(redirect_base_url="https://app.example/")
    )
    assert rp_id == "app.example"


def test_resolve_rp_id_refuses_to_fall_back_to_request_hostname():
    """The core anti-spoof invariant: if neither ``settings.domain`` nor
    ``auth.redirect_base_url`` is configured, we must raise — NOT return the
    request's Host-header-derived hostname."""
    request = MagicMock()
    request.url.hostname = "attacker.example"
    request.base_url = ""
    with pytest.raises(PasskeyStateError):
        _resolve_rp_id(request, _settings())


def test_validate_passkey_origin_config_accepts_domain():
    validate_passkey_origin_config(_settings(domain="app.example"))


def test_validate_passkey_origin_config_accepts_redirect_base_url_with_host():
    validate_passkey_origin_config(_settings(redirect_base_url="https://app.example"))


def test_validate_passkey_origin_config_rejects_when_both_empty():
    with pytest.raises(PasskeyStateError):
        validate_passkey_origin_config(_settings())


def test_validate_passkey_origin_config_rejects_redirect_base_url_without_hostname():
    # Relative URL has no hostname — must not count as configured.
    with pytest.raises(PasskeyStateError):
        validate_passkey_origin_config(_settings(redirect_base_url="/path"))


def test_resolve_origin_refuses_host_header_fallback():
    """``_resolve_origin`` must not fall back to ``request.base_url`` — a
    spoofed Host header would otherwise bind a credential to the attacker's
    origin. It must raise instead."""
    request = MagicMock()
    request.base_url = "https://attacker.example"
    with pytest.raises(PasskeyStateError):
        _resolve_origin(request, _settings())


def test_resolve_origin_uses_configured_redirect_base_url():
    request = MagicMock()
    request.base_url = "https://attacker.example"
    origin = _resolve_origin(
        request, _settings(redirect_base_url="https://app.example/")
    )
    assert origin == "https://app.example"
