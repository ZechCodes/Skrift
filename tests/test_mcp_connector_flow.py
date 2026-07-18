"""End-to-end MCP identity-provider flow — the exact Claude custom-connector sequence.

Drives a real in-process Skrift app (OAuth2 + Sitemap controllers, session
middleware, SQLite-backed) through discovery → Dynamic Client Registration →
PKCE authorize/consent → token exchange with an RFC 8707 resource → independent
JWT verification against the published JWKS → a ``BearerJWTOnly``-guarded
route → refresh rotation → remembered-consent skip.
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet
from litestar import Litestar, get
from litestar.testing import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import skrift.db.models  # noqa: F401 — registers all models on Base.metadata
from skrift.app_factory import (
    build_template_engine_callback,
    create_session_config,
    create_template_config,
    get_template_directories_for_theme,
)
from skrift.auth.bearer import BearerJWTOnly
from skrift.auth.guards import auth_guard
from skrift.auth.session_keys import (
    SESSION_USER_EMAIL,
    SESSION_USER_ID,
    SESSION_USER_NAME,
    SESSION_USER_PICTURE_URL,
)
from skrift.controllers.oauth2 import OAuth2Controller
from skrift.controllers.sitemap import SitemapController
from skrift.db.base import Base
from skrift.db.models.user import User
from skrift.forms.core import CSRF_FIELD_NAME, CSRF_SESSION_KEY


SECRET_KEY = "mcp-connector-flow-secret-key-0000000000000000"
ISSUER = "http://testserver.local"
RESOURCE = "https://app.example.com/mcp"
OTHER_RESOURCE = "https://other.example.com/api"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
REQUESTED_SCOPE = "openid profile email"
CONSENT_CSRF = "mcp-consent-csrf-token"


@get("/mcp/ping", guards=[auth_guard, BearerJWTOnly(audience=RESOURCE)])
async def mcp_ping() -> dict:
    return {"ok": True}


@pytest.fixture
def app_yaml(tmp_path, monkeypatch):
    """Point ``get_settings()`` at a tmp app.yaml with the MCP features on."""
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    config: dict = {
        "domain": "localhost",
        "auth": {
            "redirect_base_url": ISSUER,
            "methods": {"dummy": {"type": "dummy", "label": "Dummy"}},
        },
        "db": {"url": "sqlite+aiosqlite:///:memory:"},
        "session": {"max_age": 3600, "cookie_name": "session"},
        "rate_limit": {"enabled": False},
        "oauth2_enabled": True,
        "oauth2_dynamic_registration_enabled": True,
    }
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(config))

    from skrift.config import get_settings, set_config_path

    get_settings.cache_clear()
    set_config_path(path)
    yield path
    get_settings.cache_clear()
    import skrift.config

    skrift.config._config_path_override = None


@pytest.fixture
def engine():
    """Shared in-memory SQLite engine (StaticPool keeps the same connection)."""
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
def app(app_yaml, engine):
    """A real Skrift-shaped Litestar app: OAuth2 AS + discovery + a guarded route."""
    db_config = SQLAlchemyAsyncConfig(
        engine_instance=engine,
        metadata=Base.metadata,
        create_all=True,
        session_config=AsyncSessionConfig(expire_on_commit=False),
    )
    session_config = create_session_config(secret_key=SECRET_KEY, max_age=3600, secure=False)
    template_config = create_template_config(
        get_template_directories_for_theme(""),
        build_template_engine_callback(
            {
                "site_name": lambda: "Test Site",
                "static_url": lambda path: f"/static/{path}",
                "csrf_field": _csrf_field_global(),
            },
            register_for_updates=False,
        ),
    )

    app = Litestar(
        route_handlers=[OAuth2Controller, SitemapController, mcp_ping],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[session_config.middleware],
        template_config=template_config,
        csrf_config=None,
        debug=True,
    )
    app.state.session_config = session_config
    # auth_guard/resolve_bearer_jwt_permissions read this exact key. Advanced
    # Alchemy de-dupes ``session_maker_app_state_key`` across a process-wide
    # registry, so under the full suite the plugin may store its maker under
    # ``session_maker_class_1`` — pin the canonical key deterministically.
    app.state.session_maker_class = async_sessionmaker(engine, expire_on_commit=False)
    return app


def _csrf_field_global():
    from jinja2 import pass_context

    from skrift.forms import csrf_field

    @pass_context
    def render_csrf_field(context):
        return csrf_field(context["request"])

    return render_csrf_field


@pytest.fixture
def client(app):
    with TestClient(app=app, session_config=app.state.session_config) as test_client:
        yield test_client


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _decode_jwt_segment(segment: str) -> dict:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def _encode_jwt_segment(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


async def _seed_user(engine) -> str:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(email="connector-user@example.com", name="Connector User")
        session.add(user)
        await session.commit()
        return str(user.id)


def _login(client: TestClient, user_id: str) -> None:
    client.set_session_data(
        {
            SESSION_USER_ID: user_id,
            SESSION_USER_EMAIL: "connector-user@example.com",
            SESSION_USER_NAME: "Connector User",
            SESSION_USER_PICTURE_URL: "",
            CSRF_SESSION_KEY: CONSENT_CSRF,
        }
    )


def _authorize_params(client_id: str, challenge: str, *, resource: str, state: str) -> dict:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": REQUESTED_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }


def _code_from_redirect(response, *, expected_state: str) -> str:
    assert response.status_code in (301, 302, 303, 307), response.text
    location = response.headers["location"]
    assert location.startswith(REDIRECT_URI)
    callback_params = parse_qs(urlsplit(location).query)
    assert callback_params["state"] == [expected_state]
    return callback_params["code"][0]


def _exchange_code(client: TestClient, *, client_id: str, code: str, verifier: str, resource: str) -> dict:
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_full_claude_connector_flow(client, engine):
    # 1. RFC 8414 discovery advertises everything the connector needs.
    discovery = client.get("/.well-known/oauth-authorization-server")
    assert discovery.status_code == 200
    metadata = discovery.json()
    assert metadata["issuer"] == ISSUER
    assert metadata["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert metadata["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert metadata["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert metadata["jwks_uri"] == f"{ISSUER}/oauth/jwks"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert "none" in metadata["token_endpoint_auth_methods_supported"]

    # 2. Dynamic Client Registration mints a public (secretless) client.
    registration = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "client_name": "Claude Custom Connector",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": REQUESTED_SCOPE,
        },
    )
    assert registration.status_code == 201, registration.text
    registered = registration.json()
    client_id = registered["client_id"]
    assert client_id
    assert "client_secret" not in registered
    assert registered["token_endpoint_auth_method"] == "none"

    # 3. Authorize with PKCE S256 + resource: consent screen, then approval.
    user_id = _run(_seed_user(engine))
    _login(client, user_id)

    verifier, challenge = _generate_pkce_pair()
    consent_page = client.get(
        "/oauth/authorize",
        params=_authorize_params(client_id, challenge, resource=RESOURCE, state="first-state"),
        follow_redirects=False,
    )
    assert consent_page.status_code == 200, consent_page.text
    assert "Claude Custom Connector" in consent_page.text

    approval = client.post(
        "/oauth/authorize",
        data={"action": "allow", CSRF_FIELD_NAME: CONSENT_CSRF},
        follow_redirects=False,
    )
    code = _code_from_redirect(approval, expected_state="first-state")

    # 4. Token exchange: code + PKCE verifier + resource → ES256 JWT + refresh.
    token_body = _exchange_code(
        client, client_id=client_id, code=code, verifier=verifier, resource=RESOURCE
    )
    access_token = token_body["access_token"]
    refresh_token = token_body["refresh_token"]
    assert access_token.count(".") == 2
    assert refresh_token
    assert token_body["token_type"] == "bearer"
    assert token_body["scope"] == REQUESTED_SCOPE

    # 5. Verify the access token independently against the published JWKS.
    jwks_response = client.get("/oauth/jwks")
    assert jwks_response.status_code == 200
    jwks = jwks_response.json()["keys"]
    assert jwks

    header = _decode_jwt_segment(access_token.split(".")[0])
    assert header["alg"] == "ES256"
    assert header["kid"] in {key["kid"] for key in jwks}

    key_set = KeySet.import_key_set({"keys": jwks})
    claims = jose_jwt.decode(access_token, key_set, algorithms=["ES256"]).claims
    assert claims["iss"] == ISSUER
    assert claims["aud"] == RESOURCE
    assert claims["sub"] == user_id
    assert claims["exp"] > time.time()

    # 6. The Bearer guard admits the token; tampered / wrong-aud tokens get 401.
    protected = client.get("/mcp/ping", headers={"Authorization": f"Bearer {access_token}"})
    assert protected.status_code == 200, protected.text
    assert protected.json() == {"ok": True}

    header_b64, payload_b64, signature_b64 = access_token.split(".")
    tampered_claims = _decode_jwt_segment(payload_b64)
    tampered_claims["aud"] = OTHER_RESOURCE
    tampered_token = f"{header_b64}.{_encode_jwt_segment(tampered_claims)}.{signature_b64}"
    tampered = client.get("/mcp/ping", headers={"Authorization": f"Bearer {tampered_token}"})
    assert tampered.status_code == 401
    assert "invalid_token" in tampered.headers.get("www-authenticate", "")

    other_verifier, other_challenge = _generate_pkce_pair()
    other_authorize = client.get(
        "/oauth/authorize",
        params=_authorize_params(
            client_id, other_challenge, resource=OTHER_RESOURCE, state="other-state"
        ),
        follow_redirects=False,
    )
    other_code = _code_from_redirect(other_authorize, expected_state="other-state")
    wrong_audience_token = _exchange_code(
        client, client_id=client_id, code=other_code, verifier=other_verifier, resource=OTHER_RESOURCE
    )["access_token"]
    assert jose_jwt.decode(wrong_audience_token, key_set, algorithms=["ES256"]).claims["aud"] == OTHER_RESOURCE
    wrong_audience = client.get(
        "/mcp/ping", headers={"Authorization": f"Bearer {wrong_audience_token}"}
    )
    assert wrong_audience.status_code == 401
    assert "invalid_token" in wrong_audience.headers.get("www-authenticate", "")

    # 7. Refresh rotation: new tokens issued, the old refresh token is dead.
    refreshed = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )
    assert refreshed.status_code == 200, refreshed.text
    refreshed_body = refreshed.json()
    assert refreshed_body["access_token"].count(".") == 2
    assert refreshed_body["refresh_token"] != refresh_token
    assert jose_jwt.decode(
        refreshed_body["access_token"], key_set, algorithms=["ES256"]
    ).claims["aud"] == RESOURCE

    replayed = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"

    # 8. Second authorize with the same client+scopes skips the consent screen.
    repeat_verifier, repeat_challenge = _generate_pkce_pair()
    repeat_authorize = client.get(
        "/oauth/authorize",
        params=_authorize_params(
            client_id, repeat_challenge, resource=RESOURCE, state="repeat-state"
        ),
        follow_redirects=False,
    )
    repeat_code = _code_from_redirect(repeat_authorize, expected_state="repeat-state")
    repeat_body = _exchange_code(
        client, client_id=client_id, code=repeat_code, verifier=repeat_verifier, resource=RESOURCE
    )
    repeat_protected = client.get(
        "/mcp/ping", headers={"Authorization": f"Bearer {repeat_body['access_token']}"}
    )
    assert repeat_protected.status_code == 200


def _run(coro):
    """Run ``coro`` on a loop that outlives the call — the StaticPool-pinned
    aiosqlite connection must stay usable for the TestClient afterwards."""
    import asyncio

    policy = asyncio.get_event_loop_policy()
    loop = getattr(policy, "_mcp_flow_loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        policy._mcp_flow_loop = loop
    return loop.run_until_complete(coro)
