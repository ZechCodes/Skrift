"""Phase 2 MCP identity provider — RFC 7591 Dynamic Client Registration.

Covers POST /oauth/register (public-client creation, gating, redirect-uri
hardening, per-IP cap, client_name injection safety), last_used_at stamping,
the dynamic-client pruning service + worker handler, and the discovery
registration_endpoint.
"""

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from jinja2 import Environment
from litestar.exceptions import NotFoundException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import skrift
from skrift.auth.tokens import create_signed_token
from skrift.config import SecurityHeadersConfig
from skrift.controllers.oauth2 import (
    AUTH_CODE_TTL,
    CLIENT_NAME_MAX_LENGTH,
    OAuth2Controller,
    _redirect_uri_origin,
    _validate_registration_redirect_uri,
)
from skrift.controllers.sitemap import build_authorization_server_metadata
from skrift.db.base import Base
from skrift.db.models import OAuth2Client, OAuth2SigningKey  # noqa: F401  (registers tables)
from skrift.db.services import oauth2_service


SECRET = "test-secret-key"
ISSUER = "http://localhost:8000"
USER_ID = "00000000-0000-0000-0000-000000000042"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _settings(*, enabled=True, dcr=True, ip_limit=20, window=3600, max_age_days=7):
    settings = MagicMock()
    settings.secret_key = SECRET
    settings.security_headers = SecurityHeadersConfig()
    settings.oauth2_enabled = enabled
    settings.oauth2_dynamic_registration_enabled = dcr
    settings.oauth2_issuer = ISSUER
    settings.oauth2_access_token_ttl = 900
    settings.oauth2_allowed_resources = []
    settings.oauth2_dynamic_registration_ip_limit = ip_limit
    settings.oauth2_dynamic_registration_ip_window_seconds = window
    settings.oauth2_dynamic_client_max_age_days = max_age_days
    return settings


async def _register(db_session, body, *, settings=None, client_ip="1.2.3.4"):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    request.scope = {"state": {"client_ip": client_ip}}
    with patch("skrift.controllers.oauth2.get_settings", return_value=settings or _settings()):
        return await OAuth2Controller.register.fn(controller, request, db_session)


def _pkce_pair():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class TestRegisterEndpoint:
    @pytest.mark.asyncio
    async def test_returns_public_client_without_secret(self, db_session):
        result = await _register(
            db_session,
            {
                "redirect_uris": ["https://app.example.com/cb"],
                "client_name": "My Connector",
                "scope": "openid email",
            },
        )

        assert result.status_code == 201
        body = result.content
        assert "client_secret" not in body
        assert body["client_id"]
        assert body["token_endpoint_auth_method"] == "none"
        assert isinstance(body["client_id_issued_at"], int)
        assert body["redirect_uris"] == ["https://app.example.com/cb"]
        assert body["grant_types"] == ["authorization_code", "refresh_token"]
        assert body["response_types"] == ["code"]

        client = await oauth2_service.get_client_by_client_id(db_session, body["client_id"])
        assert client is not None
        assert client.client_secret == ""
        assert client.is_dynamically_registered is True
        assert client.token_endpoint_auth_method == "none"
        assert client.registered_by_ip == "1.2.3.4"
        assert client.client_id_issued_at is not None
        assert client.allowed_scope_list == ["openid", "email"]

    @pytest.mark.asyncio
    async def test_registered_public_client_completes_pkce_token_flow(self, db_session):
        register_result = await _register(
            db_session,
            {"redirect_uris": ["https://app.example.com/cb"], "scope": "openid"},
        )
        client_id = register_result.content["client_id"]

        verifier, challenge = _pkce_pair()
        code = create_signed_token(
            {
                "type": "code",
                "user_id": USER_ID,
                "email": "u@example.com",
                "name": "U User",
                "picture_url": "",
                "client_id": client_id,
                "redirect_uri": "https://app.example.com/cb",
                "scope": "openid",
                "code_challenge": challenge,
            },
            SECRET,
            AUTH_CODE_TTL,
        )

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.base_url = "http://localhost:8000/"
        # A public client presents NO client_secret — PKCE alone authenticates it.
        request.form = AsyncMock(return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://app.example.com/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        })

        with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
            token_result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

        assert token_result.status_code == 200
        assert token_result.content["access_token"]
        assert token_result.content["refresh_token"]

        # Token issuance stamps last_used_at so the pruner leaves it alone.
        client = await oauth2_service.get_client_by_client_id(db_session, client_id)
        assert client.last_used_at is not None

    @pytest.mark.asyncio
    async def test_disabled_returns_404(self, db_session):
        with pytest.raises(NotFoundException):
            await _register(
                db_session,
                {"redirect_uris": ["https://app.example.com/cb"]},
                settings=_settings(dcr=False),
            )

    @pytest.mark.asyncio
    async def test_disabled_when_oauth2_off(self, db_session):
        with pytest.raises(NotFoundException):
            await _register(
                db_session,
                {"redirect_uris": ["https://app.example.com/cb"]},
                settings=_settings(enabled=False),
            )

    @pytest.mark.asyncio
    async def test_missing_redirect_uris_invalid_client_metadata(self, db_session):
        result = await _register(db_session, {"client_name": "No URIs"})
        assert result.status_code == 400
        assert result.content["error"] == "invalid_client_metadata"

    @pytest.mark.asyncio
    async def test_empty_redirect_uris_invalid_client_metadata(self, db_session):
        result = await _register(db_session, {"redirect_uris": []})
        assert result.status_code == 400
        assert result.content["error"] == "invalid_client_metadata"

    @pytest.mark.asyncio
    async def test_non_https_non_loopback_rejected(self, db_session):
        result = await _register(db_session, {"redirect_uris": ["http://evil.example.com/cb"]})
        assert result.status_code == 400
        assert result.content["error"] == "invalid_redirect_uri"

    @pytest.mark.asyncio
    async def test_loopback_http_allowed(self, db_session):
        for uri in ["http://127.0.0.1:8765/cb", "http://localhost/cb"]:
            result = await _register(db_session, {"redirect_uris": [uri]})
            assert result.status_code == 201, uri

    @pytest.mark.asyncio
    async def test_fragment_rejected(self, db_session):
        result = await _register(db_session, {"redirect_uris": ["https://app.example.com/cb#frag"]})
        assert result.status_code == 400
        assert result.content["error"] == "invalid_redirect_uri"

    @pytest.mark.asyncio
    async def test_auth_method_must_be_none(self, db_session):
        result = await _register(
            db_session,
            {
                "redirect_uris": ["https://app.example.com/cb"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_client_metadata"

    @pytest.mark.asyncio
    async def test_unknown_scope_rejected(self, db_session):
        result = await _register(
            db_session,
            {"redirect_uris": ["https://app.example.com/cb"], "scope": "openid not-a-scope"},
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_client_metadata"

    @pytest.mark.asyncio
    async def test_client_name_length_capped(self, db_session):
        result = await _register(
            db_session,
            {"redirect_uris": ["https://app.example.com/cb"], "client_name": "A" * 5000},
        )
        assert result.status_code == 201
        client = await oauth2_service.get_client_by_client_id(db_session, result.content["client_id"])
        assert len(client.display_name) <= CLIENT_NAME_MAX_LENGTH


class TestPerIpRegistrationCap:
    @pytest.mark.asyncio
    async def test_cap_enforced_per_ip(self, db_session):
        settings = _settings(ip_limit=2)
        body = {"redirect_uris": ["https://app.example.com/cb"]}

        first = await _register(db_session, body, settings=settings, client_ip="9.9.9.9")
        second = await _register(db_session, body, settings=settings, client_ip="9.9.9.9")
        third = await _register(db_session, body, settings=settings, client_ip="9.9.9.9")

        assert first.status_code == 201
        assert second.status_code == 201
        assert third.status_code == 429

        # A different IP is unaffected by another IP's usage.
        other = await _register(db_session, body, settings=settings, client_ip="8.8.8.8")
        assert other.status_code == 201

    @pytest.mark.asyncio
    async def test_registrations_outside_window_do_not_count(self, db_session):
        settings = _settings(ip_limit=1, window=3600)
        # An old registration for this IP sits before the window and must not
        # count against the cap.
        await oauth2_service.create_dynamic_client(
            db_session,
            display_name="Old",
            redirect_uris=["https://old.example.com/cb"],
            allowed_scopes=[],
            registered_by_ip="7.7.7.7",
            issued_at=datetime.now(tz=timezone.utc) - timedelta(hours=5),
        )
        result = await _register(
            db_session,
            {"redirect_uris": ["https://app.example.com/cb"]},
            settings=settings,
            client_ip="7.7.7.7",
        )
        assert result.status_code == 201


class TestRedirectUriValidation:
    def test_https_allowed(self):
        assert _validate_registration_redirect_uri("https://app.example.com/cb") is True

    def test_loopback_ip_http_allowed(self):
        assert _validate_registration_redirect_uri("http://127.0.0.1:8765/cb") is True

    def test_localhost_http_allowed(self):
        assert _validate_registration_redirect_uri("http://localhost/cb") is True

    def test_http_non_loopback_rejected(self):
        assert _validate_registration_redirect_uri("http://example.com/cb") is False

    def test_fragment_rejected(self):
        assert _validate_registration_redirect_uri("https://app.example.com/cb#x") is False

    def test_relative_rejected(self):
        assert _validate_registration_redirect_uri("/cb") is False

    def test_empty_rejected(self):
        assert _validate_registration_redirect_uri("") is False

    def test_non_http_scheme_rejected(self):
        assert _validate_registration_redirect_uri("ftp://app.example.com/cb") is False


class TestRedirectUriAdversarial:
    """Bypass attempts an attacker would use against the redirect_uri filter."""

    def test_embedded_userinfo_rejected(self):
        # user:pass@host and host@host phishing/parser-confusion forms.
        assert _validate_registration_redirect_uri("https://user:pass@evil.com/cb") is False
        assert _validate_registration_redirect_uri("https://evil.com@good.com/cb") is False

    def test_loopback_userinfo_bypass_rejected(self):
        # Trying to satisfy the loopback exception with 127.0.0.1 in userinfo
        # while the real host is attacker-controlled.
        assert _validate_registration_redirect_uri("http://127.0.0.1@evil.com/cb") is False
        assert _validate_registration_redirect_uri("http://localhost@evil.com/cb") is False

    def test_newline_smuggling_rejected(self):
        # A single string carrying a newline: the URL parser strips it (so the
        # host looks benign) but the newline-delimited storage would later split
        # it into a second, never-validated redirect_uri.
        for smuggled in (
            "https://good.com/cb\nhttp://evil.com/steal",
            "https://good.com/cb\r\nhttp://evil.com/steal",
            "https://good.com/cb\thttp://evil.com/steal",
        ):
            assert _validate_registration_redirect_uri(smuggled) is False

    def test_csp_token_injection_hosts_rejected(self):
        # Hosts whose characters would inject extra CSP tokens/directives into
        # the consent-page form-action allowlist.
        for hostile in (
            "https://a b/cb",
            "https://good.com;form-action */cb",
            "https://exam ple.com;form-action */cb",
            "https://*.evil.com/cb",
        ):
            assert _validate_registration_redirect_uri(hostile) is False

    def test_origin_safe_for_valid_uris(self):
        assert _redirect_uri_origin("https://app.example.com/cb") == "https://app.example.com"
        assert _redirect_uri_origin("http://127.0.0.1:8765/cb") == "http://127.0.0.1:8765"

    def test_origin_none_for_hostile_uris(self):
        for hostile in (
            "https://user@evil.com/cb",
            "https://a b/cb",
            "https://good.com/cb\nhttp://evil.com",
            "https://good.com;x */cb",
        ):
            assert _redirect_uri_origin(hostile) is None

    @pytest.mark.asyncio
    async def test_newline_smuggling_registration_rejected_end_to_end(self, db_session):
        result = await _register(
            db_session,
            {"redirect_uris": ["https://good.com/cb\nhttp://evil.com/steal"]},
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_redirect_uri"
        # Nothing must have been persisted for the hostile registration.
        clients = (await db_session.execute(select(OAuth2Client))).scalars().all()
        assert clients == []

    @pytest.mark.asyncio
    async def test_csp_injection_registration_rejected_end_to_end(self, db_session):
        result = await _register(
            db_session,
            {"redirect_uris": ["https://evil.com;form-action */cb"]},
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_redirect_uri"


class TestConsentCspSink:
    """The consent-page CSP builder must never emit an attacker-shaped origin."""

    def test_clean_origin_widens_form_action(self):
        with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
            headers = OAuth2Controller._consent_csp_headers("https://app.example.com/cb")
        csp = headers["content-security-policy"]
        assert "form-action 'self' https://app.example.com" in csp
        assert "https://app.example.com/cb" not in csp

    def test_hostile_origin_fails_closed(self):
        # A malformed/hostile redirect_uri (which a hardened registrar rejects,
        # but defense-in-depth still guards) must not inject CSP tokens; the
        # builder returns no override so the default 'self' policy stands.
        for hostile in (
            "https://evil.com;form-action */cb",
            "https://a b/cb",
            "https://user@evil.com/cb",
        ):
            with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
                headers = OAuth2Controller._consent_csp_headers(hostile)
            assert headers == {}


class TestClientNameInjection:
    @pytest.mark.asyncio
    async def test_client_name_html_is_not_executable_on_consent(self, db_session):
        payload = "<script>alert('xss')</script>Nice App"
        result = await _register(
            db_session,
            {"redirect_uris": ["https://app.example.com/cb"], "client_name": payload},
        )
        client = await oauth2_service.get_client_by_client_id(db_session, result.content["client_id"])

        # The raw markup is stored verbatim (no lossy sanitization) — safety
        # comes from the consent template's autoescape, mirrored here.
        assert client.display_name == payload

        env = Environment(autoescape=True)
        rendered = env.from_string("{{ display_name or client_id }}").render(
            display_name=client.display_name, client_id=client.client_id
        )
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered

        # And the real consent template must never mark that value safe.
        template_source = (
            Path(skrift.__file__).parent / "templates" / "oauth" / "authorize.html"
        ).read_text()
        assert "display_name or client_id" in template_source
        assert "display_name | safe" not in template_source
        assert "display_name|safe" not in template_source

    @pytest.mark.asyncio
    async def test_client_name_with_json_metacharacters_round_trips(self, db_session):
        # Quotes/backslashes/braces in client_name must not break the JSON
        # registration response — Litestar's serializer escapes them and the
        # echoed value is byte-for-byte what was stored (after the length cap).
        import json

        payload = 'App", "client_secret": "pwned\\ {}'
        result = await _register(
            db_session,
            {"redirect_uris": ["https://app.example.com/cb"], "client_name": payload},
        )
        assert result.status_code == 201
        assert "client_secret" not in result.content
        assert result.content["client_name"] == payload
        # The dict serializes to valid JSON with no smuggled keys.
        encoded = json.dumps(result.content)
        assert json.loads(encoded)["client_name"] == payload


class TestDiscoveryRegistrationEndpoint:
    def test_present_when_enabled(self):
        metadata = build_authorization_server_metadata(ISSUER, registration_enabled=True)
        assert metadata["registration_endpoint"] == f"{ISSUER}/oauth/register"

    def test_absent_when_disabled(self):
        metadata = build_authorization_server_metadata(ISSUER, registration_enabled=False)
        assert "registration_endpoint" not in metadata

    @pytest.mark.asyncio
    async def test_discovery_route_advertises_registration_when_enabled(self):
        from skrift.controllers.sitemap import SitemapController

        settings = MagicMock()
        settings.oauth2_enabled = True
        settings.oauth2_issuer = ISSUER
        settings.oauth2_dynamic_registration_enabled = True

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()
        request.base_url = "http://localhost:8000/"

        with patch("skrift.config.get_settings", return_value=settings):
            result = await SitemapController.oauth_authorization_server.fn(controller, request)

        assert result.content["registration_endpoint"] == f"{ISSUER}/oauth/register"


class TestPruneStaleDynamicClients:
    async def _make_dynamic(self, db_session, *, issued_ago_days, last_used=None, ip="1.1.1.1"):
        client = await oauth2_service.create_dynamic_client(
            db_session,
            display_name="Dyn",
            redirect_uris=["https://app.example.com/cb"],
            allowed_scopes=[],
            registered_by_ip=ip,
            issued_at=datetime.now(tz=timezone.utc) - timedelta(days=issued_ago_days),
        )
        if last_used is not None:
            await oauth2_service.mark_client_used(db_session, client, last_used)
        return client

    @pytest.mark.asyncio
    async def test_prunes_unused_old_dynamic_client(self, db_session):
        await self._make_dynamic(db_session, issued_ago_days=10)
        deleted = await oauth2_service.prune_stale_dynamic_clients(
            db_session, now=datetime.now(tz=timezone.utc), max_age_days=7
        )
        assert deleted == 1
        remaining = (await db_session.execute(select(OAuth2Client))).scalars().all()
        assert remaining == []

    @pytest.mark.asyncio
    async def test_keeps_recent_dynamic_client(self, db_session):
        await self._make_dynamic(db_session, issued_ago_days=1)
        deleted = await oauth2_service.prune_stale_dynamic_clients(
            db_session, now=datetime.now(tz=timezone.utc), max_age_days=7
        )
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_keeps_used_old_dynamic_client(self, db_session):
        await self._make_dynamic(
            db_session, issued_ago_days=30, last_used=datetime.now(tz=timezone.utc)
        )
        deleted = await oauth2_service.prune_stale_dynamic_clients(
            db_session, now=datetime.now(tz=timezone.utc), max_age_days=7
        )
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_keeps_admin_client(self, db_session):
        await oauth2_service.create_client(
            db_session,
            display_name="Confidential",
            redirect_uris=["https://app.example.com/cb"],
            allowed_scopes=["openid"],
        )
        deleted = await oauth2_service.prune_stale_dynamic_clients(
            db_session, now=datetime.now(tz=timezone.utc), max_age_days=0
        )
        assert deleted == 0


class TestPruneWorkerHandler:
    @pytest.mark.asyncio
    async def test_handler_prunes_via_configured_session_maker(self):
        from skrift import oauth2_maintenance

        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async with maker() as session:
            await oauth2_service.create_dynamic_client(
                session,
                display_name="Stale",
                redirect_uris=["https://app.example.com/cb"],
                allowed_scopes=[],
                registered_by_ip="1.1.1.1",
                issued_at=datetime.now(tz=timezone.utc) - timedelta(days=30),
            )

        oauth2_maintenance.configure_oauth2_maintenance(session_maker=maker)
        try:
            with patch(
                "skrift.oauth2_maintenance.get_settings",
                return_value=_settings(max_age_days=7),
            ):
                result = await oauth2_maintenance.prune_dynamic_clients(
                    oauth2_maintenance.PruneDynamicClients(), MagicMock()
                )
        finally:
            oauth2_maintenance.configure_oauth2_maintenance(session_maker=None)

        assert result == {"deleted": 1}
        await engine.dispose()
