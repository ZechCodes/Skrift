"""Tests for second-factor config, passkey helpers, and transition services."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skrift.auth.second_factors.base import SecondFactorMethodDescriptor
from skrift.auth.second_factors.passkey import PasskeySecondFactorMethod
from skrift.auth.second_factors.passkey_service import (
    begin_primary_passkey_registration,
    begin_passkey_registration,
    complete_passkey_registration,
    get_primary_passkey_registration_state,
)
from skrift.auth.second_factors.services import build_second_factor_transition_decision
from skrift.auth.session_service import (
    PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
    PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
    PendingAuthState,
)
from skrift.config import Settings


class TestSecondFactorConfig:
    def test_settings_parse_second_factor_methods(self):
        settings = Settings(
            secret_key="test-secret",
            auth={
                "second_factors": {
                    "enabled": True,
                    "challenge_on_enrolled": True,
                    "methods": {
                        "passkey": {
                            "type": "passkey",
                            "label": "Security Key",
                        }
                    },
                }
            },
        )

        assert settings.auth.second_factors.enabled is True
        assert settings.auth.second_factors.challenge_on_enrolled is True
        assert settings.auth.second_factors.get_method_keys() == ["passkey"]
        assert settings.auth.second_factors.get_method_type("passkey") == "passkey"
        assert settings.auth.second_factors.get_method_config("passkey")["label"] == "Security Key"


class TestPasskeySecondFactorMethod:
    def test_descriptor_reports_unavailable_without_webauthn_dependency(self):
        settings = Settings(
            secret_key="test-secret",
            auth={
                "second_factors": {
                    "methods": {
                        "passkey": {
                            "type": "passkey",
                            "label": "Security Key",
                        }
                    }
                }
            },
        )

        with patch("skrift.auth.second_factors.passkey.is_webauthn_available", return_value=False):
            descriptor = PasskeySecondFactorMethod("passkey").get_descriptor(settings)

        assert descriptor.name == "Security Key"
        assert descriptor.verify_path == "/auth/verify/passkey"
        assert descriptor.is_available is False
        assert "WebAuthn" in descriptor.availability_note


class TestPasskeyService:
    def _make_request(self):
        request = MagicMock()
        request.session = {}
        request.base_url = "http://localhost:8000/"
        request.url.hostname = "localhost"
        return request

    def test_begin_passkey_registration_stores_session_challenge(self):
        request = self._make_request()
        user = MagicMock(id=uuid4(), email="user@example.com", name="User")
        settings = Settings(secret_key="test-secret")
        options_json = json.dumps(
            {
                "challenge": "challenge-123",
                "user": {"id": "user-id"},
                "excludeCredentials": [],
            }
        )

        with patch(
            "skrift.auth.second_factors.passkey_service._load_webauthn_symbols",
            return_value={
                "PublicKeyCredentialDescriptor": MagicMock(),
                "base64url_to_bytes": lambda value: value.encode("utf-8"),
                "generate_registration_options": MagicMock(return_value=object()),
                "options_to_json": MagicMock(return_value=options_json),
            },
        ):
            options = begin_passkey_registration(request, settings, user, [])

        assert options["challenge"] == "challenge-123"
        assert request.session["passkey_registration_challenge"] == "challenge-123"
        assert request.session["passkey_registration_user_id"] == str(user.id)

    def test_complete_passkey_registration_clears_session_and_normalizes_result(self):
        request = self._make_request()
        user = MagicMock(id=uuid4(), email="user@example.com", name="User")
        settings = Settings(secret_key="test-secret")
        request.session["passkey_registration_challenge"] = "challenge-123"
        request.session["passkey_registration_user_id"] = str(user.id)
        verification = MagicMock(
            credential_id=b"cred-1",
            credential_public_key=b"pub-1",
            sign_count=7,
            credential_device_type="single_device",
            credential_backed_up=False,
        )

        with patch(
            "skrift.auth.second_factors.passkey_service._load_webauthn_symbols",
            return_value={
                "base64url_to_bytes": lambda value: value.encode("utf-8"),
                "verify_registration_response": MagicMock(return_value=verification),
            },
        ):
            result = complete_passkey_registration(
                request,
                settings,
                user,
                {"response": {"transports": ["internal"]}},
            )

        assert result.sign_count == 7
        assert result.transports == ["internal"]
        assert "passkey_registration_challenge" not in request.session
        assert "passkey_registration_user_id" not in request.session

    def test_begin_primary_passkey_registration_tracks_signup_state(self):
        request = self._make_request()
        settings = Settings(secret_key="test-secret")
        options_json = json.dumps(
            {
                "challenge": "challenge-123",
                "user": {"id": "user-id"},
                "excludeCredentials": [],
            }
        )

        with patch(
            "skrift.auth.second_factors.passkey_service._load_webauthn_symbols",
            return_value={
                "generate_registration_options": MagicMock(return_value=object()),
                "options_to_json": MagicMock(return_value=options_json),
            },
        ):
            options = begin_primary_passkey_registration(
                request,
                settings,
                method_key="passkey",
                email="new@example.com",
                name="New User",
            )

        state = get_primary_passkey_registration_state(request)
        assert options["challenge"] == "challenge-123"
        assert state is not None
        assert state.email == "new@example.com"
        assert state.method_key == "passkey"


class TestSecondFactorTransitionDecision:
    def _make_pending_auth(self, user_id: str | None = "user-1") -> PendingAuthState:
        return PendingAuthState(
            pending_auth_id="pending-1",
            method_key="google",
            method_type="oauth",
            stage=PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
            user_id=user_id,
            email="user@example.com",
            expires_at=9999999999,
        )

    @pytest.mark.asyncio
    async def test_promotes_immediately_when_second_factors_disabled(self):
        settings = Settings(secret_key="test-secret")

        decision = await build_second_factor_transition_decision(
            MagicMock(),
            settings,
            MagicMock(),
            self._make_pending_auth(),
        )

        assert decision.promote_immediately is True
        assert decision.next_url is None

    @pytest.mark.asyncio
    async def test_promotes_immediately_for_primary_passkey_logins(self):
        settings = Settings(
            secret_key="test-secret",
            auth={
                "second_factors": {
                    "enabled": True,
                    "challenge_on_enrolled": True,
                    "methods": {"passkey": {"type": "passkey"}},
                }
            },
        )
        login_result = MagicMock(method_type="passkey")

        decision = await build_second_factor_transition_decision(
            MagicMock(),
            settings,
            login_result,
            self._make_pending_auth(),
        )

        assert decision.promote_immediately is True

    @pytest.mark.asyncio
    async def test_promotes_immediately_when_no_available_methods(self):
        settings = Settings(
            secret_key="test-secret",
            auth={
                "second_factors": {
                    "enabled": True,
                    "challenge_on_enrolled": True,
                    "methods": {"passkey": {"type": "passkey"}},
                }
            },
        )

        with patch(
            "skrift.auth.second_factors.services.list_available_second_factor_descriptors",
            new_callable=AsyncMock,
            return_value=[],
        ):
            decision = await build_second_factor_transition_decision(
                MagicMock(),
                settings,
                MagicMock(),
                self._make_pending_auth(),
            )

        assert decision.promote_immediately is True
        assert decision.next_url is None

    @pytest.mark.asyncio
    async def test_holds_pending_auth_when_available_methods_exist(self):
        settings = Settings(
            secret_key="test-secret",
            auth={
                "second_factors": {
                    "enabled": True,
                    "challenge_on_enrolled": True,
                    "methods": {"passkey": {"type": "passkey"}},
                }
            },
        )
        descriptor = SecondFactorMethodDescriptor(
            key="passkey",
            factor_type="passkey",
            name="Passkey",
            verify_path="/auth/verify/passkey",
        )

        with patch(
            "skrift.auth.second_factors.services.list_available_second_factor_descriptors",
            new_callable=AsyncMock,
            return_value=[descriptor],
        ):
            decision = await build_second_factor_transition_decision(
                MagicMock(),
                settings,
                MagicMock(),
                self._make_pending_auth(),
            )

        assert decision.promote_immediately is False
        assert decision.next_url == "/auth/verify"
        assert decision.stage == PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED
