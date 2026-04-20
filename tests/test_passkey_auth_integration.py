"""End-to-end integration tests for passkey auth flows.

Drives the ``AuthController`` through Litestar's ``TestClient`` against a
SQLite-backed app. The ``webauthn`` runtime is stubbed out via a
fake ``_load_webauthn_symbols`` so these tests run without the optional
passkey dependency installed.

Coverage:

* Primary passkey registration (passwordless signup)
* Primary passkey login (existing credential)
* Second-factor passkey enrollment (logged-in user)
* Second-factor passkey verification during a pending-auth flow
* Error paths: bad CSRF, missing state, unknown credential, spoofed session
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
import yaml
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from litestar import Litestar
from litestar.testing import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

import skrift.db.models  # noqa: F401 — registers all models on Base.metadata
from skrift.app_factory import create_session_config
from skrift.controllers.auth import AuthController
from skrift.db.base import Base
from skrift.db.models.second_factor import SecondFactorEnrollment
from skrift.db.models.user import User
from skrift.forms.core import CSRF_FIELD_NAME, CSRF_SESSION_KEY


# ---------------------------------------------------------------------------
# Fake WebAuthn runtime
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_to_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass
class _FakeOptions:
    """Stand-in for a webauthn options object — ``options_to_json`` stringifies it."""

    challenge: str
    extras: dict

    def __str__(self) -> str:  # pragma: no cover — used by options_to_json
        return json.dumps({"challenge": self.challenge, **self.extras})


@dataclass
class _FakeRegistrationVerification:
    credential_id: bytes
    credential_public_key: bytes
    sign_count: int = 0
    credential_device_type: str = "single_device"
    credential_backed_up: bool = False


@dataclass
class _FakeAuthenticationVerification:
    new_sign_count: int = 1
    credential_device_type: str = "single_device"
    credential_backed_up: bool = False
    user_verified: bool = True


class _FakePublicKeyCredentialDescriptor:
    def __init__(self, id: bytes, **_: Any) -> None:
        self.id = id


class _FakeUserVerificationRequirement:
    REQUIRED = "required"
    PREFERRED = "preferred"
    DISCOURAGED = "discouraged"


_FAKE_CHALLENGE = _b64url(b"fake-challenge-bytes")


def _fake_webauthn_symbols(
    *,
    credential_id_bytes: bytes = b"fake-credential-id",
    public_key_bytes: bytes = b"fake-public-key",
    challenge: str = _FAKE_CHALLENGE,
    sign_count: int = 0,
    new_sign_count: int = 1,
) -> dict:
    """Return a dict that mimics ``_load_webauthn_symbols``'s return value."""

    def generate_registration_options(**kwargs):
        return _FakeOptions(
            challenge=challenge,
            extras={
                "rp": {"id": kwargs.get("rp_id"), "name": kwargs.get("rp_name")},
                "user": {
                    "id": _b64url(kwargs.get("user_id") or b""),
                    "name": kwargs.get("user_name"),
                    "displayName": kwargs.get("user_display_name"),
                },
                "excludeCredentials": [],
            },
        )

    def generate_authentication_options(**kwargs):
        return _FakeOptions(
            challenge=challenge,
            extras={
                "rpId": kwargs.get("rp_id"),
                "userVerification": kwargs.get("user_verification"),
                "allowCredentials": [],
            },
        )

    def options_to_json(options):
        return str(options)

    def verify_registration_response(**_kwargs):
        return _FakeRegistrationVerification(
            credential_id=credential_id_bytes,
            credential_public_key=public_key_bytes,
            sign_count=sign_count,
        )

    def verify_authentication_response(**_kwargs):
        return _FakeAuthenticationVerification(new_sign_count=new_sign_count)

    return {
        "base64url_to_bytes": _b64url_to_bytes,
        "generate_authentication_options": generate_authentication_options,
        "generate_registration_options": generate_registration_options,
        "options_to_json": options_to_json,
        "PublicKeyCredentialDescriptor": _FakePublicKeyCredentialDescriptor,
        "UserVerificationRequirement": _FakeUserVerificationRequirement,
        "verify_authentication_response": verify_authentication_response,
        "verify_registration_response": verify_registration_response,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_yaml(tmp_path, monkeypatch):
    """Write a tmp ``app.yaml`` and point ``get_settings()`` at it.

    Clears the lru_cache so subsequent ``get_settings()`` calls pick up the
    new config, and restores cache state after the test.
    """
    # secret_key is loaded from env by BaseSettings, not from YAML.
    monkeypatch.setenv("SECRET_KEY", "integration-test-secret-key-0000000000000000")
    config: dict = {
        "domain": "localhost",
        "auth": {
            "redirect_base_url": "http://localhost:8000",
            "methods": {
                # Primary passkey method (passwordless login)
                "passkey": {
                    "type": "passkey",
                    "label": "Passkey",
                    "factor_key": "passkey",
                },
                # OAuth provider to exercise the second-factor holding pattern
                "dummy": {"type": "dummy", "label": "Dummy"},
            },
            "second_factors": {
                "enabled": True,
                "challenge_on_enrolled": True,
                "methods": {"passkey": {"type": "passkey", "label": "Security Key"}},
            },
        },
        "db": {"url": "sqlite+aiosqlite:///:memory:"},
        "session": {"max_age": 3600, "cookie_name": "session"},
        "rate_limit": {"enabled": False},
    }
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(config))

    from skrift.config import get_settings, set_config_path

    get_settings.cache_clear()
    set_config_path(path)
    yield path
    get_settings.cache_clear()
    # Reset the override so later tests don't see it.
    import skrift.config

    skrift.config._config_path_override = None


@pytest.fixture
def webauthn_stub(monkeypatch):
    """Replace the optional webauthn runtime with an in-memory fake."""
    monkeypatch.setattr(
        "skrift.auth.second_factors.passkey_service._load_webauthn_symbols",
        _fake_webauthn_symbols,
    )
    monkeypatch.setattr(
        "skrift.auth.second_factors.passkey_service.is_webauthn_available",
        lambda: True,
    )
    # The descriptor also imports is_webauthn_available directly.
    monkeypatch.setattr(
        "skrift.auth.second_factors.passkey.is_webauthn_available", lambda: True
    )
    monkeypatch.setattr(
        "skrift.auth.methods.passkey.is_webauthn_available", lambda: True
    )


@pytest.fixture
def engine():
    """Shared in-memory SQLite engine (StaticPool keeps the same connection)."""
    from sqlalchemy.pool import StaticPool

    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    return eng


@pytest.fixture
def app(app_yaml, webauthn_stub, engine):
    """Build a minimal Litestar app wired for the AuthController."""
    db_config = SQLAlchemyAsyncConfig(
        engine_instance=engine,
        metadata=Base.metadata,
        create_all=True,
        session_config=AsyncSessionConfig(expire_on_commit=False),
    )

    session_config = create_session_config(
        secret_key="integration-test-secret-key-0000000000000000",
        max_age=3600,
        secure=False,
    )

    app = Litestar(
        route_handlers=[AuthController],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[session_config.middleware],
        csrf_config=None,
        debug=True,
    )
    app.state.session_config = session_config
    app.state.engine = engine
    return app


@pytest.fixture
def client(app):
    """TestClient with cookie-based session support."""
    with TestClient(app=app, session_config=app.state.session_config) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_auth_hooks():
    """The controller fires async hooks on login — keep them isolated."""
    from skrift.lib.hooks import hooks

    original_filters = hooks._filters.copy()
    original_actions = hooks._actions.copy()
    hooks.clear()
    yield
    hooks._filters = original_filters
    hooks._actions = original_actions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_csrf(client: TestClient) -> str:
    """Install a known CSRF token in the session and return it."""
    token = "test-csrf-token-abc"
    client.set_session_data({CSRF_SESSION_KEY: token})
    return token


def _run(coro):
    """Run ``coro`` on a dedicated event loop.

    We can't use ``asyncio.run`` because aiosqlite connections pinned to the
    StaticPool must outlive the call (the TestClient below reuses them). So
    we manage the loop explicitly and leave it open for the duration of the
    test — pytest tears it down at fixture teardown.
    """
    policy = asyncio.get_event_loop_policy()
    loop = getattr(policy, "_test_loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        policy._test_loop = loop  # type: ignore[attr-defined]
    return loop.run_until_complete(coro)


async def _ensure_schema(engine) -> None:
    """Make sure tables exist. ``create_all=True`` on the plugin runs on startup,
    but when we seed *before* the first request we need to create them ourselves.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _insert_user_and_enrollment(
    engine,
    *,
    email: str,
    credential_id: str,
    public_key: str,
    factor_key: str = "passkey",
) -> str:
    """Insert a user + passkey enrollment directly. Returns the user ID."""
    from sqlalchemy.ext.asyncio import AsyncSession

    await _ensure_schema(engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(email=email, name="Test User")
        session.add(user)
        await session.flush()
        enrollment = SecondFactorEnrollment(
            user_id=user.id,
            factor_key=factor_key,
            factor_type="passkey",
            display_name="Primary key",
            credential_id=credential_id,
            public_key=public_key,
            sign_count=0,
            transports="internal",
            is_active=True,
        )
        session.add(enrollment)
        await session.commit()
        return str(user.id)


# ---------------------------------------------------------------------------
# Primary passkey — registration
# ---------------------------------------------------------------------------


class TestPrimaryPasskeyRegistration:
    def test_full_registration_flow_creates_user_and_logs_in(self, client, engine):
        """Happy path: start → complete primary passkey signup → user + enrollment + logged-in session."""
        csrf = _seed_csrf(client)

        # 1. Start registration — returns options and rotates CSRF
        resp = client.post(
            "/auth/passkey/register/options",
            data={"email": "new@example.com", "name": "New User", CSRF_FIELD_NAME: csrf},
        )
        assert resp.status_code == 201 or resp.status_code == 200
        body = resp.json()
        assert body["options"]["challenge"] == _FAKE_CHALLENGE
        next_csrf = body["csrf_token"]
        assert next_csrf and next_csrf != csrf

        # 2. Complete registration with the (fake) attested credential
        credential = {
            "id": "submitted-cred-id",
            "rawId": "submitted-cred-id",
            "type": "public-key",
            "response": {"transports": ["internal"]},
        }
        resp = client.post(
            "/auth/passkey/register/complete",
            data={
                "credential": json.dumps(credential),
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["redirect"] == "/"

        # Session now carries the authenticated user
        session = client.get_session_data()
        assert session.get("user_email") == "new@example.com"
        assert "user_id" in session

        # DB side: user + enrollment persisted
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy import select

        async def _check():
            async with AsyncSession(engine, expire_on_commit=False) as session:
                users = (
                    await session.execute(select(User).where(User.email == "new@example.com"))
                ).scalars().all()
                assert len(users) == 1
                enrollments = (
                    await session.execute(
                        select(SecondFactorEnrollment).where(
                            SecondFactorEnrollment.user_id == users[0].id
                        )
                    )
                ).scalars().all()
                assert len(enrollments) == 1
                assert enrollments[0].factor_key == "passkey"
                assert enrollments[0].factor_type == "passkey"

        import asyncio

        _run(_check())

    def test_registration_without_email_is_rejected(self, client):
        csrf = _seed_csrf(client)
        resp = client.post(
            "/auth/passkey/register/options",
            data={"email": "", CSRF_FIELD_NAME: csrf},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "email_required"

    def test_registration_without_csrf_is_rejected(self, client):
        resp = client.post(
            "/auth/passkey/register/options",
            data={"email": "someone@example.com"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_csrf"

    def test_complete_without_prior_start_returns_missing_state(self, client):
        csrf = _seed_csrf(client)
        credential = {"id": "x", "type": "public-key", "response": {}}
        resp = client.post(
            "/auth/passkey/register/complete",
            data={"credential": json.dumps(credential), CSRF_FIELD_NAME: csrf},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "registration_state_missing"

    def test_unknown_provider_returns_404(self, client):
        csrf = _seed_csrf(client)
        resp = client.post(
            "/auth/not-configured/register/options",
            data={"email": "x@example.com", CSRF_FIELD_NAME: csrf},
        )
        assert resp.status_code == 404

    def test_duplicate_email_rejects_second_signup(self, client, engine):
        """Duplicate email must be rejected up front at /register/options so
        the browser never prompts for a passkey that can't be used (orphan
        credential in the user's password manager). The response must be a
        generic ``invalid_request`` — a distinct "already exists" error
        would leak whether an email is registered (H3)."""
        import asyncio
        from sqlalchemy.ext.asyncio import AsyncSession

        async def _seed():
            await _ensure_schema(engine)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                session.add(User(email="taken@example.com", name="Existing"))
                await session.commit()

        _run(_seed())

        csrf = _seed_csrf(client)
        resp = client.post(
            "/auth/passkey/register/options",
            data={"email": "taken@example.com", CSRF_FIELD_NAME: csrf},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_request"
        # Do not leak that the email is already registered.
        assert "already" not in body["error"].lower()
        assert "exists" not in body["error"].lower()


# ---------------------------------------------------------------------------
# Primary passkey — authentication (existing account)
# ---------------------------------------------------------------------------


class TestPrimaryPasskeyAuthentication:
    def test_login_with_enrolled_credential_succeeds(self, client, engine):
        """A user with an enrolled credential can sign in via primary passkey auth."""
        credential_id = _b64url(b"fake-credential-id")
        public_key = _b64url(b"fake-public-key")

        import asyncio

        _run(
            _insert_user_and_enrollment(
                engine,
                email="existing@example.com",
                credential_id=credential_id,
                public_key=public_key,
            )
        )

        csrf = _seed_csrf(client)
        resp = client.post(
            "/auth/passkey/options",
            data={CSRF_FIELD_NAME: csrf},
        )
        assert resp.status_code in (200, 201), resp.text
        next_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/passkey/complete",
            data={
                "credential": json.dumps(
                    {
                        "id": credential_id,
                        "rawId": credential_id,
                        "type": "public-key",
                        "response": {},
                    }
                ),
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        assert resp.status_code in (200, 201), resp.text
        body = resp.json()
        assert body["ok"] is True

        session = client.get_session_data()
        assert session.get("user_email") == "existing@example.com"

    def test_login_with_unknown_credential_returns_generic_invalid_credential(self, client):
        """Unknown credentials must not return a distinct 404/error code —
        that would let an attacker enumerate enrolled credential IDs (H3)."""
        csrf = _seed_csrf(client)
        resp = client.post("/auth/passkey/options", data={CSRF_FIELD_NAME: csrf})
        next_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/passkey/complete",
            data={
                "credential": json.dumps(
                    {"id": "never-enrolled", "type": "public-key", "response": {}}
                ),
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_credential"

    def test_login_complete_rejects_credential_without_id(self, client):
        csrf = _seed_csrf(client)
        resp = client.post("/auth/passkey/options", data={CSRF_FIELD_NAME: csrf})
        next_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/passkey/complete",
            data={
                "credential": json.dumps({"type": "public-key", "response": {}}),
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "credential_id_required"

    def test_login_complete_with_invalid_json_is_rejected(self, client):
        csrf = _seed_csrf(client)
        resp = client.post("/auth/passkey/options", data={CSRF_FIELD_NAME: csrf})
        next_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/passkey/complete",
            data={"credential": "{not json", CSRF_FIELD_NAME: next_csrf},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_credential"


# ---------------------------------------------------------------------------
# Second-factor passkey — enrollment (logged-in user)
# ---------------------------------------------------------------------------


class TestSecondFactorPasskeyEnrollment:
    def test_logged_in_user_can_enroll_a_passkey(self, client, engine):
        """POST /auth/passkeys/options then /complete enrolls a new credential."""
        import asyncio
        from sqlalchemy.ext.asyncio import AsyncSession

        async def _seed_user():
            await _ensure_schema(engine)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                user = User(email="mfa@example.com", name="MFA User")
                session.add(user)
                await session.commit()
                return str(user.id)

        user_id = _run(_seed_user())

        # Log the user in by seeding session data
        client.set_session_data({"user_id": user_id, CSRF_SESSION_KEY: "enroll-csrf"})

        resp = client.post(
            "/auth/passkeys/options", data={CSRF_FIELD_NAME: "enroll-csrf"}
        )
        assert resp.status_code in (200, 201), resp.text
        next_csrf = resp.json()["csrf_token"]

        credential = {
            "id": "enrolled-cred-id",
            "type": "public-key",
            "response": {"transports": ["usb"]},
        }
        resp = client.post(
            "/auth/passkeys/complete",
            data={
                "credential": json.dumps(credential),
                "display_name": "Yubikey",
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["enrollment"]["display_name"] == "Yubikey"

        async def _check_db():
            async with AsyncSession(engine, expire_on_commit=False) as session:
                from sqlalchemy import select

                rows = (
                    await session.execute(
                        select(SecondFactorEnrollment).where(
                            SecondFactorEnrollment.user_id == UUID(user_id)
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].display_name == "Yubikey"

        _run(_check_db())

    def test_anonymous_user_cannot_enroll(self, client):
        """No session user → 401."""
        csrf = _seed_csrf(client)
        resp = client.post("/auth/passkeys/options", data={CSRF_FIELD_NAME: csrf})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Second-factor passkey — verification (pending-auth flow)
# ---------------------------------------------------------------------------


class TestSecondFactorPasskeyVerification:
    def test_verify_completes_pending_auth_and_promotes_session(self, client, engine):
        """A user with a pending-auth session completes 2FA via passkey."""
        import asyncio

        credential_id = _b64url(b"fake-credential-id")
        public_key = _b64url(b"fake-public-key")
        user_id = _run(
            _insert_user_and_enrollment(
                engine,
                email="mfa-user@example.com",
                credential_id=credential_id,
                public_key=public_key,
            )
        )

        # Simulate a primary-auth-verified session that's waiting on 2FA.
        from time import time as _time
        from uuid import uuid4 as _uuid4

        pending_id = _uuid4().hex
        client.set_session_data(
            {
                "pending_auth_id": pending_id,
                "pending_auth_method": "dummy",
                "pending_auth_method_type": "oauth",
                "pending_auth_stage": "second_factor_required",
                "pending_auth_user_id": user_id,
                "pending_auth_expires_at": int(_time()) + 900,
                CSRF_SESSION_KEY: "verify-csrf",
            }
        )

        resp = client.post(
            "/auth/verify/passkey/options",
            data={CSRF_FIELD_NAME: "verify-csrf"},
        )
        assert resp.status_code in (200, 201), resp.text
        next_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/verify/passkey/complete",
            data={
                "credential": json.dumps(
                    {
                        "id": credential_id,
                        "rawId": credential_id,
                        "type": "public-key",
                        "response": {},
                    }
                ),
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["ok"] is True

        session = client.get_session_data()
        assert session.get("user_id") == user_id
        assert "pending_auth_id" not in session

    def test_verify_without_pending_auth_returns_401(self, client):
        csrf = _seed_csrf(client)
        resp = client.post(
            "/auth/verify/passkey/options", data={CSRF_FIELD_NAME: csrf}
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "pending_auth_missing"

    def test_verify_with_cross_user_credential_rejected(self, client, engine):
        """Session user A cannot complete with user B's enrolled credential."""
        import asyncio
        from time import time as _time
        from uuid import uuid4 as _uuid4

        credential_id_a = _b64url(b"user-a-cred")
        credential_id_b = _b64url(b"user-b-cred")
        public_key = _b64url(b"fake-public-key")

        _run(
            _insert_user_and_enrollment(
                engine,
                email="user-a@example.com",
                credential_id=credential_id_a,
                public_key=public_key,
            )
        )
        user_b_id = _run(
            _insert_user_and_enrollment(
                engine,
                email="user-b@example.com",
                credential_id=credential_id_b,
                public_key=public_key,
            )
        )

        # Session "belongs" to user B, but attacker tries to submit user A's credential.
        client.set_session_data(
            {
                "pending_auth_id": _uuid4().hex,
                "pending_auth_method": "dummy",
                "pending_auth_method_type": "oauth",
                "pending_auth_stage": "second_factor_required",
                "pending_auth_user_id": user_b_id,
                "pending_auth_expires_at": int(_time()) + 900,
                CSRF_SESSION_KEY: "verify-csrf",
            }
        )

        resp = client.post(
            "/auth/verify/passkey/options", data={CSRF_FIELD_NAME: "verify-csrf"}
        )
        next_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/verify/passkey/complete",
            data={
                "credential": json.dumps(
                    {
                        "id": credential_id_a,
                        "rawId": credential_id_a,
                        "type": "public-key",
                        "response": {},
                    }
                ),
                CSRF_FIELD_NAME: next_csrf,
            },
        )
        # Generic `invalid_credential` (400) — NOT the distinct 404/
        # `credential_not_found` that would let an attacker enumerate whose
        # credentials live on the server (H3).
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_credential"

        # Session must still be pending — NOT promoted
        session = client.get_session_data()
        assert "user_id" not in session
        assert session.get("pending_auth_user_id") == user_b_id


# ---------------------------------------------------------------------------
# H3 enumeration hygiene — the remaining oracle sites
# ---------------------------------------------------------------------------


class TestPasskeyEnumerationHygiene:
    """All public passkey endpoints must return a single generic error shape
    for the branches that check the DB, so an attacker cannot probe whether
    an email/user/credential exists. The real reason is logged server-side
    (asserted below via caplog) so operators can still diagnose failures."""

    def _seed_pending_auth(self, client, *, user_id: str):
        from time import time as _time
        from uuid import uuid4 as _uuid4

        client.set_session_data(
            {
                "pending_auth_id": _uuid4().hex,
                "pending_auth_method": "dummy",
                "pending_auth_method_type": "oauth",
                "pending_auth_stage": "second_factor_required",
                "pending_auth_user_id": user_id,
                "pending_auth_expires_at": int(_time()) + 900,
                CSRF_SESSION_KEY: "enum-csrf",
            }
        )

    def test_verify_options_with_deleted_pending_user_returns_generic(self, client, caplog):
        """pending_auth carries a user_id that no longer exists in the DB
        (race condition or admin-delete). Response must be a flat
        ``invalid_request`` at 400 — not ``user_not_found`` at 404."""
        import logging
        from uuid import uuid4 as _uuid4

        missing_id = str(_uuid4())
        self._seed_pending_auth(client, user_id=missing_id)

        with caplog.at_level(logging.INFO, logger="skrift.controllers.auth"):
            resp = client.post(
                "/auth/verify/passkey/options",
                data={CSRF_FIELD_NAME: "enum-csrf"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"
        # Session's pending_auth must be cleared so the attacker cannot retry.
        assert "pending_auth_user_id" not in client.get_session_data()
        # Ops visibility: the real reason should show up in the server log.
        assert any(
            "pending-auth user not found" in r.getMessage() for r in caplog.records
        )

    def test_verify_options_with_zero_enrollments_returns_generic(self, client, engine, caplog):
        """A real user with no passkey enrollments looks identical to any
        other verification failure — no ``no_enrollments`` leak."""
        import logging
        from sqlalchemy.ext.asyncio import AsyncSession

        async def _seed_bare_user():
            await _ensure_schema(engine)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                user = User(email="bare@example.com", name="Bare")
                session.add(user)
                await session.commit()
                return str(user.id)

        user_id = _run(_seed_bare_user())
        self._seed_pending_auth(client, user_id=user_id)

        with caplog.at_level(logging.INFO, logger="skrift.controllers.auth"):
            resp = client.post(
                "/auth/verify/passkey/options",
                data={CSRF_FIELD_NAME: "enum-csrf"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"
        assert any(
            "no enrollments" in r.getMessage().lower() for r in caplog.records
        )

    def test_verify_complete_with_deleted_pending_user_returns_generic(self, client, caplog):
        """Same invariant for /verify/{factor}/complete — deleted user in
        pending_auth must surface as ``invalid_request`` 400."""
        import logging
        from uuid import uuid4 as _uuid4

        self._seed_pending_auth(client, user_id=str(_uuid4()))

        with caplog.at_level(logging.INFO, logger="skrift.controllers.auth"):
            resp = client.post(
                "/auth/verify/passkey/complete",
                data={
                    "credential": json.dumps(
                        {"id": "x", "rawId": "x", "type": "public-key", "response": {}}
                    ),
                    CSRF_FIELD_NAME: "enum-csrf",
                },
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"
        assert any(
            "pending-auth user not found" in r.getMessage() for r in caplog.records
        )

    def test_signup_duplicate_email_log_does_not_include_plaintext_email(self, client, engine, caplog):
        """The server-side log for duplicate signup must not itself enumerate
        the email — we log an SHA-256 prefix, never the raw address."""
        import logging
        from sqlalchemy.ext.asyncio import AsyncSession

        async def _seed():
            await _ensure_schema(engine)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                session.add(User(email="sensitive@example.com", name="U"))
                await session.commit()

        _run(_seed())

        csrf = _seed_csrf(client)
        with caplog.at_level(logging.INFO, logger="skrift.controllers.auth"):
            resp = client.post(
                "/auth/passkey/register/options",
                data={"email": "sensitive@example.com", CSRF_FIELD_NAME: csrf},
            )

        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"
        # Log must fire for ops visibility, but must not contain the
        # plaintext email.
        matched = [r for r in caplog.records if "email already registered" in r.getMessage()]
        assert matched, "expected a diagnostic log for duplicate-email signup"
        for record in matched:
            assert "sensitive@example.com" not in record.getMessage()
