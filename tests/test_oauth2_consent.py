"""Phase 3 MCP identity provider — remembered OAuth2 consent.

Covers the ConsentGrant service (find/upsert/union/revoke/cascade), the
``/oauth/authorize`` consent-skip and re-prompt behaviour, that consent
approval persists a grant, and that deleting or pruning a client removes its
grants.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.config import SecurityHeadersConfig
from skrift.controllers.oauth2 import OAuth2Controller
from skrift.db.base import Base
from skrift.db.models import ConsentGrant, OAuth2Client  # noqa: F401  (registers tables)
from skrift.db.services import oauth2_consent_service, oauth2_service


SECRET = "test-secret-key"
USER_ID = "00000000-0000-0000-0000-000000000042"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000099"
REDIRECT_URI = "http://localhost/cb"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _settings():
    settings = MagicMock()
    settings.secret_key = SECRET
    settings.security_headers = SecurityHeadersConfig()
    settings.oauth2_allowed_resources = []
    return settings


async def _make_client(db_session, scopes=None):
    return await oauth2_service.create_dynamic_client(
        db_session,
        display_name="Consent App",
        redirect_uris=[REDIRECT_URI],
        allowed_scopes=scopes or [],
        registered_by_ip="1.1.1.1",
        issued_at=datetime.now(tz=timezone.utc),
    )


def _authorize_get_request(client_id, scope, user_id=USER_ID):
    request = MagicMock()
    request.query_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": "xyz",
        "scope": scope,
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
    }
    request.session = {
        "user_id": user_id,
        "user_email": "u@example.com",
        "user_name": "User",
        "user_picture_url": "",
    }
    return request


async def _authorize_get(db_session, request):
    controller = OAuth2Controller(owner=MagicMock())
    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
        return await OAuth2Controller.authorize_get.fn(controller, request, db_session)


class TestConsentService:
    @pytest.mark.asyncio
    async def test_find_grant_missing_returns_none(self, db_session):
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, "client-a") is None

    @pytest.mark.asyncio
    async def test_upsert_creates_grant(self, db_session):
        grant = await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id="client-a",
            scopes=["openid", "profile"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        assert set(grant.scope_list) == {"openid", "profile"}
        found = await oauth2_consent_service.find_grant(db_session, USER_ID, "client-a")
        assert found is not None
        assert set(found.scope_list) == {"openid", "profile"}

    @pytest.mark.asyncio
    async def test_upsert_unions_scopes_and_keeps_one_row(self, db_session):
        now = datetime.now(tz=timezone.utc)
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=USER_ID, client_id="client-a", scopes=["openid"], granted_at=now
        )
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=USER_ID, client_id="client-a", scopes=["email"], granted_at=now
        )
        found = await oauth2_consent_service.find_grant(db_session, USER_ID, "client-a")
        assert set(found.scope_list) == {"openid", "email"}
        rows = (await db_session.execute(select(ConsentGrant))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_grant_is_scoped_per_user(self, db_session):
        now = datetime.now(tz=timezone.utc)
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=USER_ID, client_id="client-a", scopes=["openid"], granted_at=now
        )
        assert (
            await oauth2_consent_service.find_grant(db_session, OTHER_USER_ID, "client-a")
            is None
        )

    @pytest.mark.asyncio
    async def test_revoke_grant_deletes_it(self, db_session):
        now = datetime.now(tz=timezone.utc)
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=USER_ID, client_id="client-a", scopes=["openid"], granted_at=now
        )
        await oauth2_consent_service.revoke_grant(db_session, USER_ID, "client-a")
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, "client-a") is None

    @pytest.mark.asyncio
    async def test_delete_grants_for_clients_is_bulk(self, db_session):
        now = datetime.now(tz=timezone.utc)
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=USER_ID, client_id="client-a", scopes=["openid"], granted_at=now
        )
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=OTHER_USER_ID, client_id="client-a", scopes=["openid"], granted_at=now
        )
        await oauth2_consent_service.upsert_grant(
            db_session, user_id=USER_ID, client_id="client-b", scopes=["openid"], granted_at=now
        )
        deleted = await oauth2_consent_service.delete_grants_for_clients(db_session, ["client-a"])
        assert deleted == 2
        remaining = (await db_session.execute(select(ConsentGrant))).scalars().all()
        assert [g.client_id for g in remaining] == ["client-b"]


class TestAuthorizeConsentSkip:
    @pytest.mark.asyncio
    async def test_first_authorize_prompts_consent(self, db_session):
        client = await _make_client(db_session)
        request = _authorize_get_request(client.client_id, "openid profile")
        result = await _authorize_get(db_session, request)
        assert isinstance(result, TemplateResponse)
        assert result.template_name == "oauth/authorize.html"

    @pytest.mark.asyncio
    async def test_remembered_consent_skips_and_issues_code(self, db_session):
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["openid", "profile"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        request = _authorize_get_request(client.client_id, "openid profile")
        result = await _authorize_get(db_session, request)
        assert isinstance(result, Redirect)
        assert "code=" in result.url
        assert "state=xyz" in result.url
        # A skip must not stash consent params in the session.
        assert "oauth_authorize" not in request.session

    @pytest.mark.asyncio
    async def test_subset_scopes_skip_consent(self, db_session):
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["openid", "profile", "email"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        request = _authorize_get_request(client.client_id, "openid")
        result = await _authorize_get(db_session, request)
        assert isinstance(result, Redirect)
        assert "code=" in result.url

    @pytest.mark.asyncio
    async def test_broader_scopes_reprompt(self, db_session):
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["openid"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        request = _authorize_get_request(client.client_id, "openid profile")
        result = await _authorize_get(db_session, request)
        assert isinstance(result, TemplateResponse)

    @pytest.mark.asyncio
    async def test_grant_for_other_user_does_not_skip(self, db_session):
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=OTHER_USER_ID,
            client_id=client.client_id,
            scopes=["openid", "profile"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        request = _authorize_get_request(client.client_id, "openid profile")
        result = await _authorize_get(db_session, request)
        assert isinstance(result, TemplateResponse)


class TestAuthorizePostPersistsGrant:
    @pytest.mark.asyncio
    async def test_approval_persists_grant(self, db_session):
        client = await _make_client(db_session)
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.session = {
            "user_id": USER_ID,
            "user_email": "u@example.com",
            "user_name": "User",
            "user_picture_url": "",
            "oauth_authorize": {
                "client_id": client.client_id,
                "redirect_uri": REDIRECT_URI,
                "state": "xyz",
                "scope": "openid profile",
                "code_challenge": "challenge",
                "resource": "",
            },
        }
        request.form = AsyncMock(return_value={"action": "allow"})

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
             patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
            result = await OAuth2Controller.authorize_post.fn(controller, request, db_session)

        assert isinstance(result, Redirect)
        assert "code=" in result.url
        grant = await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id)
        assert grant is not None
        assert set(grant.scope_list) == {"openid", "profile"}

    @pytest.mark.asyncio
    async def test_denial_does_not_persist_grant(self, db_session):
        client = await _make_client(db_session)
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.session = {
            "user_id": USER_ID,
            "oauth_authorize": {
                "client_id": client.client_id,
                "redirect_uri": REDIRECT_URI,
                "state": "xyz",
                "scope": "openid profile",
                "code_challenge": "challenge",
                "resource": "",
            },
        }
        request.form = AsyncMock(return_value={"action": "deny"})

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
             patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
            result = await OAuth2Controller.authorize_post.fn(controller, request, db_session)

        assert isinstance(result, Redirect)
        assert "error=access_denied" in result.url
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id) is None


class TestClientDeletionCascade:
    @pytest.mark.asyncio
    async def test_delete_client_removes_its_grants(self, db_session):
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["openid"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        await oauth2_service.delete_client(db_session, client.id)
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id) is None

    @pytest.mark.asyncio
    async def test_prune_removes_grants_of_pruned_clients(self, db_session):
        client = await oauth2_service.create_dynamic_client(
            db_session,
            display_name="Stale",
            redirect_uris=[REDIRECT_URI],
            allowed_scopes=[],
            registered_by_ip="1.1.1.1",
            issued_at=datetime.now(tz=timezone.utc) - timedelta(days=30),
        )
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["openid"],
            granted_at=datetime.now(tz=timezone.utc),
        )
        deleted = await oauth2_service.prune_stale_dynamic_clients(
            db_session, now=datetime.now(tz=timezone.utc), max_age_days=7
        )
        assert deleted == 1
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id) is None
