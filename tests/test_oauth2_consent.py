"""Phase 3 MCP identity provider — remembered OAuth2 consent.

Covers the ConsentGrant service (find/upsert/union/decline/revoke/cascade), the
``/oauth/authorize`` consent-skip and re-prompt behaviour, that consent
approval persists a grant, that a user may approve only a subset of the
requested scopes, and that deleting or pruning a client removes its grants.
"""

import base64
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from jinja2 import Environment, FileSystemLoader
from litestar.datastructures import FormMultiDict
from litestar.response import Redirect, Template as TemplateResponse
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.auth.tokens import verify_signed_token
from skrift.config import SecurityHeadersConfig
from skrift.controllers.oauth2 import OAuth2Controller
from skrift.db.base import Base
from skrift.db.models import ConsentGrant, OAuth2Client  # noqa: F401  (registers tables)
from skrift.db.services import oauth2_consent_service, oauth2_service


SECRET = "test-secret-key"
USER_ID = "00000000-0000-0000-0000-000000000042"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000099"
REDIRECT_URI = "http://localhost/cb"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "skrift" / "templates"


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
    settings.oauth2_issuer = "http://localhost:8000"
    settings.oauth2_access_token_ttl = 900
    return settings


def _pkce_pair():
    verifier = "consent-code-verifier-long-enough-for-pkce"
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


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


def _consent_post_request(
    client_id,
    requested_scope,
    granted_scopes,
    *,
    action="allow",
    code_challenge="challenge",
):
    """A consent-form submission.

    ``granted_scopes`` is the list of boxes the user left checked — the
    browser submits one ``scope`` field per checked box, so an empty list
    submits none at all.
    """
    request = MagicMock()
    request.session = {
        "user_id": USER_ID,
        "user_email": "u@example.com",
        "user_name": "User",
        "user_picture_url": "",
        "oauth_authorize": {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": requested_scope,
            "code_challenge": code_challenge,
            "resource": "",
        },
    }
    fields = [("action", action)] + [("scope", scope) for scope in granted_scopes]
    request.form = AsyncMock(return_value=FormMultiDict(fields))
    return request


async def _authorize_post(db_session, request):
    controller = OAuth2Controller(owner=MagicMock())
    with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
         patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
        return await OAuth2Controller.authorize_post.fn(controller, request, db_session)


async def _exchange_code(db_session, *, client_id, code, code_verifier):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.form = AsyncMock(
        return_value=FormMultiDict(
            [
                ("grant_type", "authorization_code"),
                ("code", code),
                ("redirect_uri", REDIRECT_URI),
                ("client_id", client_id),
                ("code_verifier", code_verifier),
            ]
        )
    )
    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
        return await OAuth2Controller.token_exchange.fn(controller, request, db_session)


async def _refresh(db_session, *, client_id, refresh_token, scope):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.form = AsyncMock(
        return_value=FormMultiDict(
            [
                ("grant_type", "refresh_token"),
                ("refresh_token", refresh_token),
                ("client_id", client_id),
                ("scope", scope),
            ]
        )
    )
    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
        return await OAuth2Controller.token_exchange.fn(controller, request, db_session)


def _code_from(redirect):
    return parse_qs(urlsplit(redirect.url).query)["code"][0]


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
    async def test_declined_scopes_are_removed_from_an_existing_grant(self, db_session):
        now = datetime.now(tz=timezone.utc)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id="client-a",
            scopes=["openid", "profile", "email"],
            granted_at=now,
        )
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id="client-a",
            scopes=["openid"],
            declined_scopes=["profile", "email"],
            granted_at=now,
        )
        found = await oauth2_consent_service.find_grant(db_session, USER_ID, "client-a")
        assert set(found.scope_list) == {"openid"}

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
        request.form = AsyncMock(
            return_value=FormMultiDict(
                [("action", "allow"), ("scope", "openid"), ("scope", "profile")]
            )
        )

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


class TestPartialConsent:
    """A user may approve a subset of the requested scopes (RFC 6749 §3.3)."""

    @pytest.mark.asyncio
    async def test_only_checked_scopes_are_granted_and_coded(self, db_session):
        client = await _make_client(db_session)
        request = _consent_post_request(
            client.client_id, "openid profile email", ["openid", "profile"]
        )

        result = await _authorize_post(db_session, request)

        assert isinstance(result, Redirect)
        payload = verify_signed_token(_code_from(result), SECRET)
        assert payload["scope"] == "openid profile"
        grant = await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id)
        assert set(grant.scope_list) == {"openid", "profile"}

    @pytest.mark.asyncio
    async def test_every_box_checked_grants_the_full_request(self, db_session):
        client = await _make_client(db_session)
        request = _consent_post_request(
            client.client_id, "openid profile email", ["openid", "profile", "email"]
        )

        result = await _authorize_post(db_session, request)

        payload = verify_signed_token(_code_from(result), SECRET)
        assert payload["scope"] == "openid profile email"
        grant = await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id)
        assert set(grant.scope_list) == {"openid", "profile", "email"}

    @pytest.mark.asyncio
    async def test_scope_outside_the_request_is_rejected(self, db_session):
        """The authorization request is the ceiling — a submitted scope that was
        never requested is a tampered form, not a wider grant."""
        client = await _make_client(db_session)
        request = _consent_post_request(client.client_id, "openid", ["openid", "email"])

        result = await _authorize_post(db_session, request)

        assert not isinstance(result, Redirect)
        assert result.status_code == 400
        assert result.content["error"] == "invalid_scope"
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id) is None

    @pytest.mark.asyncio
    async def test_unchecking_everything_is_a_denial(self, db_session):
        client = await _make_client(db_session)
        request = _consent_post_request(client.client_id, "openid profile", [])

        result = await _authorize_post(db_session, request)

        assert isinstance(result, Redirect)
        assert "error=access_denied" in result.url
        assert "state=xyz" in result.url
        assert "code=" not in result.url
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id) is None

    @pytest.mark.asyncio
    async def test_denial_button_wins_over_checked_boxes(self, db_session):
        """Browsers submit checked boxes with whichever button was pressed."""
        client = await _make_client(db_session)
        request = _consent_post_request(
            client.client_id, "openid profile", ["openid"], action="deny"
        )

        result = await _authorize_post(db_session, request)

        assert isinstance(result, Redirect)
        assert "error=access_denied" in result.url
        assert await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id) is None

    @pytest.mark.asyncio
    async def test_scopeless_request_still_approves(self, db_session):
        """A client that asked for no scopes has nothing to uncheck, so an empty
        submission is an approval — not the uncheck-everything denial."""
        client = await _make_client(db_session)
        request = _consent_post_request(client.client_id, "", [])

        result = await _authorize_post(db_session, request)

        assert isinstance(result, Redirect)
        payload = verify_signed_token(_code_from(result), SECRET)
        assert payload["scope"] == ""

    @pytest.mark.asyncio
    async def test_declined_scope_reprompts_on_the_next_request(self, db_session):
        client = await _make_client(db_session)
        await _authorize_post(
            db_session,
            _consent_post_request(client.client_id, "openid profile email", ["openid"]),
        )

        result = await _authorize_get(
            db_session, _authorize_get_request(client.client_id, "openid profile email")
        )

        assert isinstance(result, TemplateResponse)

    @pytest.mark.asyncio
    async def test_declining_narrows_a_previously_broader_grant(self, db_session):
        """Unchecking a scope the user granted before must actually take it
        away, otherwise the union in upsert_grant would silently restore it and
        the next authorize would skip consent."""
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["openid", "profile", "email"],
            granted_at=datetime.now(tz=timezone.utc),
        )

        await _authorize_post(
            db_session,
            _consent_post_request(client.client_id, "openid profile email", ["openid"]),
        )

        grant = await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id)
        assert set(grant.scope_list) == {"openid"}
        reprompt = await _authorize_get(
            db_session, _authorize_get_request(client.client_id, "openid email")
        )
        assert isinstance(reprompt, TemplateResponse)

    @pytest.mark.asyncio
    async def test_scopes_absent_from_the_screen_survive_a_narrowed_consent(self, db_session):
        """Only the scopes actually shown to the user are re-decided; an earlier
        grant for a scope this request never mentioned still stands."""
        client = await _make_client(db_session)
        await oauth2_consent_service.upsert_grant(
            db_session,
            user_id=USER_ID,
            client_id=client.client_id,
            scopes=["email"],
            granted_at=datetime.now(tz=timezone.utc),
        )

        await _authorize_post(
            db_session,
            _consent_post_request(client.client_id, "openid profile", ["openid"]),
        )

        grant = await oauth2_consent_service.find_grant(db_session, USER_ID, client.client_id)
        assert set(grant.scope_list) == {"openid", "email"}


class TestNarrowedConsentReachesTheToken:
    @pytest.mark.asyncio
    async def test_token_response_carries_only_the_granted_scope(self, db_session):
        """RFC 6749 §3.3: the token response reports the granted scope when it
        differs from the requested one."""
        client = await _make_client(db_session)
        verifier, challenge = _pkce_pair()
        approval = await _authorize_post(
            db_session,
            _consent_post_request(
                client.client_id,
                "openid profile email",
                ["openid", "email"],
                code_challenge=challenge,
            ),
        )

        token_response = await _exchange_code(
            db_session,
            client_id=client.client_id,
            code=_code_from(approval),
            code_verifier=verifier,
        )

        assert token_response.status_code == 200
        assert token_response.content["scope"] == "openid email"

    @pytest.mark.asyncio
    async def test_refresh_cannot_widen_a_narrowed_grant(self, db_session):
        client = await _make_client(db_session)
        verifier, challenge = _pkce_pair()
        approval = await _authorize_post(
            db_session,
            _consent_post_request(
                client.client_id,
                "openid profile email",
                ["openid"],
                code_challenge=challenge,
            ),
        )
        token_response = await _exchange_code(
            db_session,
            client_id=client.client_id,
            code=_code_from(approval),
            code_verifier=verifier,
        )

        widened = await _refresh(
            db_session,
            client_id=client.client_id,
            refresh_token=token_response.content["refresh_token"],
            scope="openid profile",
        )

        assert widened.status_code == 400
        assert widened.content["error"] == "invalid_scope"


class TestConsentTemplateCheckboxes:
    @staticmethod
    def _render(**context):
        environment = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
        environment.globals.update(
            site_name=lambda: "Skrift",
            static_url=lambda path: f"/static/{path}",
            csp_nonce=lambda: "test-nonce",
            csrf_field=lambda: Markup('<input type="hidden" name="_csrf_token" value="t">'),
        )
        return environment.get_template("oauth/authorize.html").render(**context)

    def test_each_scope_renders_a_checked_checkbox_inside_the_form(self):
        html = self._render(
            client_id="abc",
            display_name="Consent App",
            scopes=["openid", "profile"],
            scope_descriptions=[
                {"name": "openid", "description": "Verify your identity"},
                {"name": "profile", "description": "See your profile"},
            ],
        )

        for scope_name in ("openid", "profile"):
            assert f'name="scope" value="{scope_name}"' in html
        assert html.count('type="checkbox"') == 2
        assert html.count("checked") == 2
        assert "Verify your identity" in html
        assert "See your profile" in html
        # The boxes are only submitted if they live inside the consent form.
        assert html.index("<form") < html.index('type="checkbox"') < html.index("</form>")


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
