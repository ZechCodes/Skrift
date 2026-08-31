"""M6 — `/oauth/userinfo` honors granted scope strictly.

Previously an access token with an empty `scope` field triggered a
backwards-compat branch that returned the full profile + email claim
set — a silent scope bypass.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.db.base import Base
from skrift.db.models.role import Role
from skrift.db.models.user import User

from skrift.auth.tokens import create_signed_token
from skrift.controllers.oauth2 import ACCESS_TOKEN_TTL, OAuth2Controller


SECRET = "test-secret-key"
USER_ID = "00000000-0000-0000-0000-000000000042"


def _settings():
    s = MagicMock()
    s.secret_key = SECRET
    return s


def _access_token(scope: str) -> str:
    return create_signed_token(
        {
            "type": "access",
            "user_id": USER_ID,
            "email": "u@example.com",
            "name": "U User",
            "picture_url": "https://x/p.png",
            "client_id": "abc",
            "scope": scope,
        },
        SECRET,
        ACCESS_TOKEN_TTL,
    )


async def _userinfo(
    token: str, *, email_verified: bool = True, db_session: AsyncMock | None = None
) -> MagicMock:
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.headers = {"authorization": f"Bearer {token}"}
    if db_session is None:
        db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc, \
         patch("skrift.controllers.oauth2.oauth_service") as mock_oauth_svc:
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        mock_oauth_svc.is_email_verified_for_user = AsyncMock(return_value=email_verified)
        return await OAuth2Controller.userinfo.fn(controller, request, db_session)


@pytest.mark.asyncio
async def test_empty_scope_returns_only_sub():
    """A token minted with empty scope must NOT leak email/name/picture.
    This is the M6 regression — the prior behavior returned everything."""
    result = await _userinfo(_access_token(""))
    assert result.status_code == 200
    assert result.content == {"sub": USER_ID}
    assert "email" not in result.content
    assert "email_verified" not in result.content
    assert "name" not in result.content
    assert "picture" not in result.content


@pytest.mark.asyncio
async def test_openid_scope_returns_only_sub():
    result = await _userinfo(_access_token("openid"))
    assert result.content == {"sub": USER_ID}


@pytest.mark.asyncio
async def test_email_scope_returns_sub_and_email():
    result = await _userinfo(_access_token("openid email"))
    assert result.content == {
        "sub": USER_ID,
        "email": "u@example.com",
        "email_verified": True,
    }


@pytest.mark.asyncio
async def test_email_verified_reflects_server_records():
    """`email_verified` must mirror this server's own verification state,
    not a blanket true — issue #151."""
    result = await _userinfo(_access_token("openid email"), email_verified=False)
    assert result.content == {
        "sub": USER_ID,
        "email": "u@example.com",
        "email_verified": False,
    }


@pytest.mark.asyncio
async def test_profile_scope_returns_sub_name_picture():
    result = await _userinfo(_access_token("openid profile"))
    assert result.content == {
        "sub": USER_ID,
        "name": "U User",
        "picture": "https://x/p.png",
    }


@pytest.mark.asyncio
async def test_all_scopes_returns_full_claim_set():
    result = await _userinfo(_access_token("openid profile email"))
    assert result.content == {
        "sub": USER_ID,
        "email": "u@example.com",
        "email_verified": True,
        "name": "U User",
        "picture": "https://x/p.png",
    }


@pytest.mark.asyncio
async def test_legacy_path_does_not_query_the_user_row():
    """Characterization: the legacy HMAC path is pure payload decoding.

    The profile travels in the token, so no `User` row is loaded. Any claim
    that needs live database state must gate its own lookup behind its scope
    so tokens that did not request it keep paying nothing.
    """
    db_session = AsyncMock()
    result = await _userinfo(_access_token("openid profile email"), db_session=db_session)
    assert result.status_code == 200
    assert db_session.get.await_count == 0


class TestGroupsClaimLegacyPath:
    """The `groups` claim on the legacy HMAC path.

    Roles are read live from the user record, never from the token payload —
    a legacy token minted before the role change must not carry stale
    membership.
    """

    @staticmethod
    def _db_session(roles):
        db_session = AsyncMock()
        user = MagicMock()
        user.roles = roles
        db_session.get = AsyncMock(return_value=user)
        return db_session

    @staticmethod
    def _role(name):
        role = MagicMock()
        role.name = name
        return role

    @pytest.mark.asyncio
    async def test_groups_scope_grants_the_claim(self):
        db_session = self._db_session([self._role("llm-users")])
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content == {"sub": USER_ID, "groups": ["llm-users"]}

    @pytest.mark.asyncio
    async def test_multiple_roles_all_appear_sorted(self):
        """Users hold many roles — every one is reported, order-independently."""
        db_session = self._db_session(
            [self._role("editor"), self._role("admin"), self._role("llm-users")]
        )
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content["groups"] == ["admin", "editor", "llm-users"]

    @pytest.mark.asyncio
    async def test_user_with_no_roles_yields_empty_list(self):
        db_session = self._db_session([])
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content == {"sub": USER_ID, "groups": []}

    @pytest.mark.asyncio
    async def test_missing_user_row_yields_empty_list(self):
        db_session = AsyncMock()
        db_session.get = AsyncMock(return_value=None)
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content == {"sub": USER_ID, "groups": []}

    @pytest.mark.asyncio
    async def test_without_the_scope_the_claim_is_omitted_and_no_query_runs(self):
        """The lookup is opt-in: no groups scope, no claim and no database hit."""
        db_session = self._db_session([self._role("llm-users")])
        result = await _userinfo(_access_token("openid profile email"), db_session=db_session)
        assert "groups" not in result.content
        assert db_session.get.await_count == 0

    @pytest.mark.asyncio
    async def test_groups_does_not_leak_other_claims(self):
        db_session = self._db_session([self._role("llm-users")])
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content == {"sub": USER_ID, "groups": ["llm-users"]}
        assert "email" not in result.content
        assert "name" not in result.content
        assert "picture" not in result.content


class TestGroupsClaimAgainstRealSession:
    """The groups claim reads ``user.roles`` off a User loaded inside an async
    request. Every other groups test hands userinfo a MagicMock whose ``roles``
    is already a plain list, which cannot lazy-load and so cannot notice if the
    relationship stops being eagerly loaded. These drive a real session so the
    ``lazy="selectin"`` contract on ``User.roles`` is actually pinned: drop it
    and these fail with MissingGreenlet while the mocked tests stay green.
    """

    @pytest_asyncio.fixture
    async def db_session(self):
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            yield session
        await engine.dispose()

    async def _seed(self, session, role_names):
        user = User(
            id=UUID(USER_ID),
            email="u@example.com",
            name="U User",
            picture_url="https://x/p.png",
        )
        for name in role_names:
            user.roles.append(Role(name=name, display_name=name.title()))
        session.add(user)
        await session.commit()
        # Expire everything so the next load is a genuine round trip through
        # the relationship loader rather than an identity-map hit.
        session.expunge_all()

    @pytest.mark.asyncio
    async def test_roles_load_through_a_real_session(self, db_session):
        await self._seed(db_session, ["llm-users", "admin"])
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.status_code == 200
        assert result.content["groups"] == ["admin", "llm-users"]

    @pytest.mark.asyncio
    async def test_user_with_no_roles_yields_empty_list(self, db_session):
        await self._seed(db_session, [])
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content["groups"] == []

    @pytest.mark.asyncio
    async def test_missing_user_row_yields_empty_list(self, db_session):
        result = await _userinfo(_access_token("openid groups"), db_session=db_session)
        assert result.content["groups"] == []
