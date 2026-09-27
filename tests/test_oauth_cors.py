"""CORS on the public OAuth2 endpoints for browser-based clients (#167).

Runs the real OAuth2 and discovery controllers behind the middleware list the
way ``create_app`` builds it, and checks that ``/oauth/authorize`` (cookie
authenticated, a top-level navigation) gets no CORS headers.
"""

from unittest.mock import patch

import pytest
import yaml
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from litestar import Litestar
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import skrift.db.models  # noqa: F401 — registers all models on Base.metadata
from skrift import asgi
from skrift.app_factory import create_session_config
from skrift.config import Settings
from skrift.controllers.oauth2 import OAuth2Controller
from skrift.controllers.sitemap import SitemapController
from skrift.db.base import Base
from skrift.middleware.oauth_cors import (
    OAUTH_CORS_PATHS,
    OAuthCORSMiddleware,
    build_oauth_cors_middleware,
)

SECRET_KEY = "oauth-cors-secret-key-000000000000000000000000"
ISSUER = "http://testserver.local"
ORIGIN = "https://claude.ai"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    config = {
        "domain": "localhost",
        "auth": {
            "redirect_base_url": ISSUER,
            "methods": {"dummy": {"type": "dummy", "label": "Dummy"}},
        },
        "db": {"url": "sqlite+aiosqlite:///:memory:"},
        "rate_limit": {"enabled": False},
        "oauth2_enabled": True,
        "oauth2_dynamic_registration_enabled": True,
    }
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(config))

    import skrift.config
    from skrift.config import get_settings, set_config_path

    get_settings.cache_clear()
    set_config_path(path)
    yield get_settings()
    get_settings.cache_clear()
    skrift.config._config_path_override = None


@pytest.fixture
def client(settings):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db_config = SQLAlchemyAsyncConfig(
        engine_instance=engine,
        metadata=Base.metadata,
        create_all=True,
        session_config=AsyncSessionConfig(expire_on_commit=False),
    )
    session_config = create_session_config(secret_key=SECRET_KEY, max_age=3600, secure=False)
    app = Litestar(
        route_handlers=[OAuth2Controller, SitemapController],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[*build_oauth_cors_middleware(settings), session_config.middleware],
        csrf_config=None,
    )
    with TestClient(app=app, session_config=session_config) as test_client:
        yield test_client


def _preflight(client, path, method="POST"):
    return client.options(
        path,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type, authorization",
        },
    )


@pytest.mark.parametrize("path", sorted(OAUTH_CORS_PATHS))
def test_preflight_is_answered_with_204_and_the_allowed_methods_and_headers(client, path):
    response = _preflight(client, path)

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert response.headers["access-control-allow-headers"] == "content-type, authorization"
    assert response.headers["access-control-max-age"] == "86400"
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("GET", "/.well-known/oauth-authorization-server", {}, 200),
        ("GET", "/.well-known/openid-configuration", {}, 200),
        (
            "POST",
            "/oauth/register",
            {"json": {"redirect_uris": ["https://claude.ai/cb"], "token_endpoint_auth_method": "none"}},
            201,
        ),
        ("POST", "/oauth/token", {"data": {"grant_type": "password"}}, 400),
        ("GET", "/oauth/jwks", {}, 200),
        ("POST", "/oauth/revoke", {"data": {"token": ""}}, 200),
        ("POST", "/oauth/introspect", {"data": {"token": "x"}}, 400),
        ("GET", "/oauth/userinfo", {}, 401),
    ],
)
def test_responses_carry_a_wildcard_origin(client, method, path, kwargs, status):
    response = client.request(method, path, headers={"Origin": ORIGIN}, **kwargs)

    assert response.status_code == status, response.text
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_authorize_gets_no_cors_headers(client, method):
    response = client.request(
        method, "/oauth/authorize", headers={"Origin": ORIGIN}, follow_redirects=False
    )
    preflight = _preflight(client, "/oauth/authorize", method)

    for result in (response, preflight):
        assert not any(name.startswith("access-control-") for name in result.headers)


def _create_app(settings):
    with (
        patch.object(asgi, "get_settings", return_value=settings),
        patch("skrift.config.get_settings", return_value=settings),
        patch.object(asgi, "load_controllers", list),
    ):
        app = asgi.create_app()
    # create_app wraps the Litestar app in the static/storage file middleware.
    while not isinstance(app, Litestar):
        app = app.app
    return app


def _has_cors_middleware(app):
    return any(
        isinstance(entry, DefineMiddleware) and entry.middleware is OAuthCORSMiddleware
        for entry in app.middleware
    )


def test_create_app_installs_the_middleware_for_every_oauth_cors_route():
    settings = Settings(secret_key=SECRET_KEY, oauth2_enabled=True)

    app = _create_app(settings)

    assert _has_cors_middleware(app)
    # The path list names real routes, so a renamed endpoint cannot silently
    # lose its CORS headers.
    assert OAUTH_CORS_PATHS <= {route.path for route in app.routes}


def test_create_app_leaves_cors_out_when_oauth2_is_off():
    app = _create_app(Settings(secret_key=SECRET_KEY, oauth2_enabled=False))

    assert not _has_cors_middleware(app)
