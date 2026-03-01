"""ASGI application factory for Skrift.

This module handles application creation with setup wizard support.
The application uses a dispatcher architecture:
- AppDispatcher routes requests to either the setup app or the main app
- When setup completes, the dispatcher switches all traffic to the main app
- No server restart required after setup
"""

import asyncio
import hashlib
import importlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from advanced_alchemy.config import EngineConfig
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)

from skrift.db.session import SessionCleanupMiddleware
from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.config.csrf import CSRFConfig as LitestarCSRFConfig
from litestar.middleware import DefineMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.app_factory import (
    EXCEPTION_HANDLERS,
    build_template_engine_callback,
    create_session_config,
    create_static_hasher,
    create_template_config,
    get_template_directories,
    update_template_directories,
)
from skrift.config import get_config_path, get_settings, is_config_valid
from skrift.middleware.rate_limit import RateLimitMiddleware
from skrift.middleware.security import SecurityHeadersMiddleware
from skrift.db.base import Base
from skrift.db.services.setting_service import (
    load_site_settings_cache,
    get_cached_site_name,
    get_cached_site_tagline,
    get_cached_site_name_for,
    get_cached_site_tagline_for,
    get_cached_site_copyright_holder,
    get_cached_site_copyright_start_year,
    get_cached_site_theme,
    get_cached_site_favicon_key,
    get_cached_favicon_url,
    set_cached_favicon_url,
    get_setting,
    SETUP_COMPLETED_AT_KEY,
)

logger = logging.getLogger(__name__)


class ThemeStaticURL:
    """Resolves theme-relative static paths by prepending the active theme name.

    Delegates to ``StaticHasher`` for hash computation and caching.
    """

    def __init__(self, static_hasher: "StaticHasher", theme_getter: "Callable[[], str]") -> None:
        self._static_hasher = static_hasher
        self._theme_getter = theme_getter

    def __call__(self, path: str) -> str:
        return self._static_hasher(f"{self._theme_getter()}/{path}")


class StaticHasher:
    """Resolves static file paths and caches content hashes for URL cache busting.

    Input path includes the source prefix, e.g. ``skrift/css/skrift.css``.
    Output URL is ``/static/skrift/css/skrift.css?h=abc123``.
    """

    def __init__(
        self,
        themes_dir: Path,
        site_static_dir: Path,
        package_static_dir: Path,
    ) -> None:
        self._themes_dir = themes_dir
        self._site_static_dir = site_static_dir
        self._package_static_dir = package_static_dir
        self._cache: dict[str, str] = {}

    def __call__(self, path: str) -> str:
        if path not in self._cache:
            self._cache[path] = self._compute(path)
        return self._cache[path]

    def _compute(self, path: str) -> str:
        from skrift.middleware.static import resolve_static_file

        slash_idx = path.find("/")
        if slash_idx == -1:
            return f"/static/{path}"

        source = path[:slash_idx]
        filepath = path[slash_idx + 1:]

        resolved = resolve_static_file(
            source, filepath,
            self._themes_dir, self._site_static_dir, self._package_static_dir,
        )
        if resolved is not None:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()[:8]
            return f"/static/{path}?h={digest}"
        return f"/static/{path}"


def load_controllers() -> list:
    """Load controllers from app.yaml configuration."""
    config_path = get_config_path()

    if not config_path.exists():
        return []

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if not config:
        return []

    # Add working directory to sys.path for local controller imports
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    controllers = []
    for controller_spec in config.get("controllers", []):
        module_path, class_name = controller_spec.split(":")
        module = importlib.import_module(module_path)
        controller_class = getattr(module, class_name)
        controllers.append(controller_class)

        # Auto-expand AdminController to include split sub-controllers
        if class_name == "AdminController" and module_path == "skrift.admin.controller":
            for sub_name in ("UserAdminController", "SettingsAdminController", "MediaAdminController"):
                sub_class = getattr(module, sub_name, None)
                if sub_class and sub_class not in controllers:
                    controllers.append(sub_class)

            # Generate dynamic per-type page admin controllers
            from skrift.config import load_page_types_from_yaml
            from skrift.admin.page_type_factory import create_page_type_controller
            from skrift.auth.roles import expand_roles_for_page_types

            page_types = load_page_types_from_yaml()
            expand_roles_for_page_types(page_types)

            for pt in page_types:
                controllers.append(create_page_type_controller(pt))

            # Public controllers for custom types served on primary domain
            from skrift.controllers.page_type_factory import create_public_page_type_controller

            for pt in page_types:
                if pt.name != "page" and not pt.subdomain:
                    controllers.append(create_public_page_type_controller(pt))

    return controllers


def load_site_controllers(specs: list[str]) -> list:
    """Load controllers from explicit import specs for a subdomain site.

    Unlike load_controllers(), this does not read app.yaml and does not
    auto-expand AdminController.
    """
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    controllers = []
    for spec in specs:
        module_path, class_name = spec.split(":")
        module = importlib.import_module(module_path)
        controllers.append(getattr(module, class_name))
    return controllers


def _load_middleware_factory(spec: str):
    """Import a single middleware factory from a module:name spec.

    Args:
        spec: String in format "module.path:factory_name"

    Returns:
        The callable middleware factory

    Raises:
        ValueError: If spec doesn't contain exactly one colon
        ImportError: If the module cannot be imported
        AttributeError: If the factory doesn't exist in the module
        TypeError: If the factory is not callable
    """
    if ":" not in spec:
        raise ValueError(
            f"Invalid middleware spec '{spec}': must be in format 'module:factory'"
        )

    parts = spec.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid middleware spec '{spec}': must contain exactly one colon"
        )

    module_path, factory_name = parts
    module = importlib.import_module(module_path)
    factory = getattr(module, factory_name)

    if not callable(factory):
        raise TypeError(
            f"Middleware factory '{spec}' is not callable"
        )

    return factory


def load_middleware() -> list:
    """Load middleware from app.yaml configuration.

    Supports two formats in app.yaml:

    Simple (no args):
        middleware:
          - myapp.middleware:create_logging_middleware

    With kwargs:
        middleware:
          - factory: myapp.middleware:create_rate_limit_middleware
            kwargs:
              requests_per_minute: 100

    Returns:
        List of middleware factories or DefineMiddleware instances
    """
    config_path = get_config_path()

    if not config_path.exists():
        return []

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if not config:
        return []

    middleware_specs = config.get("middleware", [])
    if not middleware_specs:
        return []

    # Add working directory to sys.path for local middleware imports
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    middleware = []
    for spec in middleware_specs:
        if isinstance(spec, str):
            # Simple format: "module:factory"
            factory = _load_middleware_factory(spec)
            middleware.append(factory)
        elif isinstance(spec, dict):
            # Dict format with optional kwargs
            if "factory" not in spec:
                raise ValueError(
                    f"Middleware dict spec must have 'factory' key: {spec}"
                )
            factory = _load_middleware_factory(spec["factory"])
            kwargs = spec.get("kwargs", {})
            if kwargs:
                middleware.append(DefineMiddleware(factory, **kwargs))
            else:
                middleware.append(factory)
        else:
            raise ValueError(
                f"Invalid middleware spec type: {type(spec).__name__}. "
                "Must be string or dict."
            )

    return middleware


async def check_setup_complete(db_config: SQLAlchemyAsyncConfig) -> bool:
    """Check if setup has been completed."""
    try:
        async with db_config.get_session() as session:
            value = await get_setting(session, SETUP_COMPLETED_AT_KEY)
            return value is not None
    except Exception:
        logger.debug("Setup completion check failed", exc_info=True)
        return False


# Module-level reference to the dispatcher for state updates
_dispatcher: "AppDispatcher | None" = None


def lock_setup_in_dispatcher() -> None:
    """Lock setup in the dispatcher, making /setup/* return 404.

    This is called when setup is complete and user visits the main site.
    """
    global _dispatcher
    if _dispatcher is not None:
        _dispatcher.setup_locked = True


class AppDispatcher:
    """ASGI dispatcher that routes between setup and main apps.

    Uses a simple setup_locked flag:
    - When True: /setup/* returns 404 (via main app), all traffic goes to main app
    - When False: Setup routes work, check DB to determine routing for other paths

    The main_app can be None at startup if config isn't valid yet. It will be
    lazily created after setup completes.
    """

    def __init__(
        self,
        setup_app: ASGIApp,
        db_url: str | None = None,
        main_app: Litestar | None = None,
    ) -> None:
        self._main_app = main_app
        self._main_app_error: str | None = None
        self._main_app_started = main_app is not None  # Track if lifespan started
        self.setup_app = setup_app
        self.setup_locked = False  # When True, setup is inaccessible
        self._db_url = db_url
        self._lifespan_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None

    async def _get_or_create_main_app(self) -> Litestar | None:
        """Get the main app, creating it lazily if needed."""
        if self._main_app is None and self._main_app_error is None:
            try:
                self._main_app = create_app()
                # Run lifespan startup for the newly created app
                await self._start_main_app_lifespan()
            except Exception as e:
                logger.error("Failed to create main app", exc_info=True)
                self._main_app_error = str(e)
                self._main_app = None
        return self._main_app

    async def _start_main_app_lifespan(self) -> None:
        """Start the main app's lifespan (runs startup handlers)."""
        if self._main_app is None or self._main_app_started:
            return

        startup_complete = asyncio.Event()
        startup_failed: str | None = None
        self._shutdown_event = asyncio.Event()
        message_queue: asyncio.Queue = asyncio.Queue()

        # Queue the startup message
        await message_queue.put({"type": "lifespan.startup"})

        async def receive():
            # First return startup, then wait for shutdown signal
            msg = await message_queue.get()
            return msg

        async def send(message):
            nonlocal startup_failed
            if message["type"] == "lifespan.startup.complete":
                startup_complete.set()
            elif message["type"] == "lifespan.startup.failed":
                startup_failed = message.get("message", "Startup failed")
                startup_complete.set()

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

        async def run_lifespan():
            try:
                await self._main_app(scope, receive, send)
            except Exception:
                logger.warning("Lifespan handler error", exc_info=True)

        # Start lifespan handler in background
        self._lifespan_task = asyncio.create_task(run_lifespan())

        # Wait for startup to complete
        await startup_complete.wait()

        if startup_failed:
            raise RuntimeError(startup_failed)

        self._main_app_started = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan events go to setup app if no main app yet
            app = self._main_app or self.setup_app
            await app(scope, receive, send)
            return

        path = scope.get("path", "")

        # If setup is locked, main app handles EVERYTHING
        if self.setup_locked:
            main_app = self._main_app or await self._get_or_create_main_app()
            if main_app:
                await main_app(scope, receive, send)
                return
            # Can't create main app — show error page
            await self._error_response(
                send,
                f"Setup complete but cannot start application: {self._main_app_error}",
            )
            return

        # Setup not locked - /setup/* always goes to setup app
        if path.startswith("/setup") or path.startswith("/static"):
            await self.setup_app(scope, receive, send)
            return

        # Check if setup is complete in DB
        if await self._is_setup_complete_in_db():
            # Setup complete - try to get/create main app
            main_app = await self._get_or_create_main_app()
            if main_app:
                self.setup_locked = True
                await main_app(scope, receive, send)
            else:
                # Can't create main app - show error
                await self._error_response(
                    send,
                    f"Setup complete but cannot start application: {self._main_app_error}"
                )
        else:
            # Setup not complete
            # Route /auth/* to setup app for OAuth callbacks during setup
            if path.startswith("/auth"):
                await self.setup_app(scope, receive, send)
            else:
                # Redirect other paths to /setup
                await self._redirect(send, "/setup")

    async def _is_setup_complete_in_db(self) -> bool:
        """Check if setup is complete in the database."""
        db_url = self._db_url

        # Try to get db_url dynamically if not set at startup
        # (setup may have configured the database after server started)
        if not db_url:
            try:
                from skrift.setup.state import get_database_url_from_yaml
                db_url = get_database_url_from_yaml()
                if db_url:
                    self._db_url = db_url  # Cache for future requests
            except Exception:
                logger.debug("Could not get database URL from config", exc_info=True)

        if not db_url:
            return False

        try:
            return await check_setup_in_db(db_url)
        except Exception:
            logger.debug("DB setup check failed", exc_info=True)
            return False

    async def _redirect(self, send: Send, location: str) -> None:
        """Send a redirect response."""
        await send({
            "type": "http.response.start",
            "status": 302,
            "headers": [(b"location", location.encode()), (b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})

    def _get_config_error_hint(self, message: str) -> str | None:
        """Return actionable guidance for known configuration errors."""
        if "secret_key" in message.lower():
            return (
                "Skrift requires a SECRET_KEY environment variable. "
                "Create a .env file in your project directory with:\n\n"
                "SECRET_KEY=your-secret-key-here\n"
                "DEBUG=true\n\n"
                "Or set it directly in your shell:\n\n"
                "export SECRET_KEY=your-secret-key-here"
            )
        return None

    async def _error_response(self, send: Send, message: str) -> None:
        """Send an error response using the built-in error template."""
        hint = self._get_config_error_hint(message)

        try:
            template_engine = self.setup_app.template_engine
            template = template_engine.get_template("error.html")
            body = template.render(
                status_code=500,
                message=message,
                hint=hint,
            ).encode()
        except Exception:
            logger.warning("Error template failed to render", exc_info=True)
            body = f"<h1>Configuration Error</h1><p>{message}</p>".encode()

        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _build_site_app(
    settings,
    site_name: str,
    site_config,
    db_config: SQLAlchemyAsyncConfig,
    session_config,
    csrf_config,
    security_middleware: list,
    rate_limit_middleware: list,
    user_middleware: list,
    static_url,
    themes_dir: Path,
    site_static_dir: Path,
    package_static_dir: Path,
    page_types: list | None = None,
) -> ASGIApp:
    """Build a lightweight Litestar app for a subdomain site.

    Shares the database engine, session config, CSRF config, and middleware
    with the primary app. Gets its own controllers and template directories.
    """
    from skrift.app_factory import get_template_directories_for_theme
    from skrift.forms import Form, csrf_field as _csrf_field

    controllers = load_site_controllers(site_config.controllers)

    if page_types:
        from skrift.controllers.page_type_factory import create_public_page_type_controller

        for pt in page_types:
            controllers.append(create_public_page_type_controller(pt, for_subdomain=True))

    theme = site_config.theme or settings.theme

    def _sized_url(url: str, size: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}size={size}"

    template_dirs = get_template_directories_for_theme(theme)
    engine_callback = build_template_engine_callback(
        extra_globals={
            "site_name": lambda _sub=site_config.subdomain: get_cached_site_name_for(_sub),
            "site_tagline": lambda _sub=site_config.subdomain: get_cached_site_tagline_for(_sub),
            "site_copyright_holder": get_cached_site_copyright_holder,
            "site_copyright_start_year": get_cached_site_copyright_start_year,
            "active_theme": lambda _t=theme: _t,
            "themes_available": lambda: False,
            "Form": Form,
            "csrf_field": _csrf_field,
            "static_url": static_url,
            "theme_url": ThemeStaticURL(static_url, lambda _t=theme: _t),
            "login_url": lambda: f"https://{settings.domain}/auth/login",
            "favicon_url": get_cached_favicon_url,
        },
        extra_filters={"sized": _sized_url},
        register_for_updates=False,
    )
    template_config = create_template_config(template_dirs, engine_callback)

    site_app = Litestar(
        route_handlers=controllers,
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[
            DefineMiddleware(SessionCleanupMiddleware),
            *security_middleware,
            *rate_limit_middleware,
            session_config.middleware,
            *user_middleware,
        ],
        template_config=template_config,
        compression_config=CompressionConfig(backend="gzip"),
        csrf_config=csrf_config,
        exception_handlers=EXCEPTION_HANDLERS,
        debug=settings.debug,
    )

    from skrift.middleware.static import StaticFilesMiddleware
    return StaticFilesMiddleware(
        site_app,
        themes_dir=themes_dir,
        site_static_dir=site_static_dir,
        package_static_dir=package_static_dir,
    )


def create_app() -> ASGIApp:
    """Create and configure the main Litestar application.

    This app has all routes for normal operation. It is used by the dispatcher
    after setup is complete.

    When ``domain`` and ``sites`` are configured in app.yaml, the returned app
    is a :class:`~skrift.middleware.site_dispatch.SiteDispatcher` that routes
    subdomain requests to lightweight per-site Litestar apps.
    """
    # CRITICAL: Check for dummy auth in production BEFORE anything else
    from skrift.setup.providers import validate_no_dummy_auth_in_production
    validate_no_dummy_auth_in_production()

    settings = get_settings()

    from skrift.lib import observability
    observability.configure(settings)
    observability.instrument_httpx()

    # Load controllers from app.yaml
    controllers = load_controllers()

    # Load middleware from app.yaml
    user_middleware = load_middleware()

    # Database schema configuration
    if settings.db.db_schema:
        if "sqlite" in settings.db.url:
            raise ValueError(
                f"Database schema '{settings.db.db_schema}' is configured but SQLite does not support schemas. "
                "For dev environments, use app.dev.yaml to override the database configuration."
            )
        Base.metadata.schema = settings.db.db_schema

    # Database configuration
    if "sqlite" in settings.db.url:
        engine_config = EngineConfig(echo=settings.db.echo)
    else:
        engine_kwargs: dict[str, Any] = dict(
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.pool_overflow,
            pool_timeout=settings.db.pool_timeout,
            pool_pre_ping=settings.db.pool_pre_ping,
            echo=settings.db.echo,
        )
        if settings.db.db_schema:
            engine_kwargs["execution_options"] = {
                "schema_translate_map": {None: settings.db.db_schema},
            }

        engine_config = EngineConfig(**engine_kwargs)

    db_config = SQLAlchemyAsyncConfig(
        connection_string=settings.db.url,
        metadata=Base.metadata,
        create_all=False,
        session_config=AsyncSessionConfig(expire_on_commit=False),
        engine_config=engine_config,
    )

    # Session configuration (client-side encrypted cookies)
    session_config = create_session_config(
        secret_key=settings.secret_key,
        max_age=settings.session.max_age,
        secure=not settings.debug,
        cookie_domain=settings.session.cookie_domain,
        cookie_name=settings.session.cookie_name,
    )

    # Security headers middleware
    security_middleware = []
    if settings.security_headers.enabled:
        headers = settings.security_headers.build_headers(debug=settings.debug)
        csp_value = settings.security_headers.content_security_policy
        if headers or csp_value:
            security_middleware = [
                DefineMiddleware(
                    SecurityHeadersMiddleware,
                    headers=headers,
                    csp_value=csp_value,
                    csp_nonce=settings.security_headers.csp_nonce,
                    debug=settings.debug,
                    cache_authenticated=settings.security_headers.cache_authenticated,
                )
            ]

    # Rate limiting middleware
    rate_limit_middleware = []
    if settings.rate_limit.enabled:
        rate_limit_middleware = [
            DefineMiddleware(
                RateLimitMiddleware,
                requests_per_minute=settings.rate_limit.requests_per_minute,
                auth_requests_per_minute=settings.rate_limit.auth_requests_per_minute,
                paths=settings.rate_limit.paths,
            )
        ]

    # CSRF configuration (if enabled in app.yaml)
    csrf_config = None
    if settings.csrf is not None:
        csrf_config = LitestarCSRFConfig(
            secret=settings.secret_key,
            cookie_secure=not settings.debug,
            cookie_httponly=False,
            cookie_samesite="lax",
            cookie_domain=settings.session.cookie_domain,
            exclude=settings.csrf.exclude or None,
        )

    # Static files
    from skrift.lib.theme import get_themes_dir
    themes_dir = get_themes_dir()
    site_static_dir = Path(os.getcwd()) / "static"
    package_static_dir = Path(__file__).parent / "static"
    static_url = create_static_hasher(
        themes_dir=themes_dir,
        site_static_dir=site_static_dir,
        package_static_dir=package_static_dir,
    )

    # Storage manager
    from skrift.lib.storage import StorageManager

    storage_manager = StorageManager(settings.storage)

    async def _asset_url(key_or_asset, store=None):
        """Template helper: resolve an asset URL."""
        from skrift.db.models.asset import Asset

        if isinstance(key_or_asset, Asset):
            backend = await storage_manager.get(key_or_asset.store)
            return await backend.get_url(key_or_asset.key)
        backend = await storage_manager.get(store)
        return await backend.get_url(key_or_asset)

    async def _resolve_favicon_url():
        """Resolve the favicon storage key to a URL and cache it."""
        key = get_cached_site_favicon_key()
        if not key:
            set_cached_favicon_url("")
            return
        backend = await storage_manager.get()
        url = await backend.get_url(key)
        set_cached_favicon_url(url)

    # Template configuration
    from skrift.forms import Form, csrf_field as _csrf_field
    from skrift.lib.theme import themes_available

    def _sized_url(url: str, size: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}size={size}"

    template_dirs = get_template_directories()
    engine_callback = build_template_engine_callback(
        extra_globals={
            "site_name": get_cached_site_name,
            "site_tagline": get_cached_site_tagline,
            "site_copyright_holder": get_cached_site_copyright_holder,
            "site_copyright_start_year": get_cached_site_copyright_start_year,
            "active_theme": get_cached_site_theme,
            "themes_available": themes_available,
            "Form": Form,
            "csrf_field": _csrf_field,
            "static_url": static_url,
            "theme_url": ThemeStaticURL(static_url, get_cached_site_theme),
            "asset_url": _asset_url,
            "favicon_url": get_cached_favicon_url,
        },
        extra_filters={"sized": _sized_url},
    )
    template_config = create_template_config(template_dirs, engine_callback)

    from skrift.controllers.notifications import NotificationsController
    from skrift.controllers.notification_webhook import NotificationsWebhookController
    from skrift.controllers.oauth2 import OAuth2Controller
    from skrift.controllers.sitemap import SitemapController
    from skrift.auth import sync_roles_to_database
    from skrift.lib.notification_backends import InMemoryBackend, load_backend
    from skrift.lib.notifications import notifications as notification_service

    # OAuth2 controller — only registered when clients are configured
    oauth2_handlers: list = []
    if settings.oauth2.clients:
        oauth2_handlers.append(OAuth2Controller)
        # Exempt token endpoint from CSRF since it's called by external clients
        if settings.csrf is not None:
            settings.csrf.exclude.append("/oauth/token")

    # Webhook controller — only registered when a secret is configured
    webhook_handlers: list = []
    if settings.notifications.webhook_secret:
        webhook_handlers.append(NotificationsWebhookController)
        # Exempt webhook from CSRF since it uses bearer-token auth
        if settings.csrf is not None:
            settings.csrf.exclude.append("/notifications/webhook")

    # Notification backend setup
    if settings.notifications.backend:
        backend_cls = load_backend(settings.notifications.backend)
        backend = backend_cls(settings=settings, session_maker=db_config.get_session)
    else:
        backend = InMemoryBackend()

    async def on_startup(_app: Litestar) -> None:
        """Sync roles, load site settings, and start notification backend on startup."""
        try:
            async with db_config.get_session() as session:
                await sync_roles_to_database(session)
                await load_site_settings_cache(session)
        except Exception:
            logger.info("Startup cache init skipped (DB may not exist)", exc_info=True)

        await _resolve_favicon_url()

        update_template_directories()

        observability.instrument_sqlalchemy(db_config.get_engine())

        from skrift.lib.hooks import hooks
        await hooks.do_action("logfire_configured")

        notification_service.set_backend(backend)
        await backend.start()

        # Ensure local storage directories exist
        for store_cfg in settings.storage.stores.values():
            if store_cfg.backend == "local":
                Path(store_cfg.local_path).mkdir(parents=True, exist_ok=True)

    async def on_shutdown(_app: Litestar) -> None:
        """Stop notification backend and storage on shutdown."""
        await notification_service._get_backend().stop()
        await storage_manager.close()

    app = Litestar(
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        route_handlers=[NotificationsController, SitemapController, *oauth2_handlers, *webhook_handlers, *controllers],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[DefineMiddleware(SessionCleanupMiddleware), *security_middleware, *rate_limit_middleware, session_config.middleware, *user_middleware],
        template_config=template_config,
        compression_config=CompressionConfig(backend="gzip", exclude="/notifications/stream"),
        csrf_config=csrf_config,
        exception_handlers=EXCEPTION_HANDLERS,
        debug=settings.debug,
    )
    app.state.webhook_secret = settings.notifications.webhook_secret
    app.state.storage_manager = storage_manager

    from skrift.middleware.storage import StorageFilesMiddleware
    from skrift.middleware.static import StaticFilesMiddleware
    primary_asgi = StorageFilesMiddleware(
        StaticFilesMiddleware(
            observability.instrument_app(app),
            themes_dir=themes_dir,
            site_static_dir=site_static_dir,
            package_static_dir=package_static_dir,
        ),
        storage_config=settings.storage,
    )

    # Build subdomain → page_types mapping from config
    from skrift.config import load_page_types_from_yaml
    all_page_types = load_page_types_from_yaml()
    subdomain_page_types: dict[str, list] = {}
    for pt in all_page_types:
        if pt.subdomain:
            subdomain_page_types.setdefault(pt.subdomain, []).append(pt)

    # Subdomain site dispatch — when domain is set and sites or page type subdomains exist
    forced_subdomain = os.environ.get("SKRIFT_SUBDOMAIN", "")
    need_dispatch = (settings.sites or subdomain_page_types) and (settings.domain or forced_subdomain)

    if forced_subdomain and not need_dispatch:
        raise SystemExit(
            f"--subdomain '{forced_subdomain}' requires sites or page type subdomains "
            f"to be configured in app.yaml"
        )

    if need_dispatch:
        from skrift.middleware.site_dispatch import SiteDispatcher
        from skrift.config import SiteConfig

        site_apps = {}
        for name, site_cfg in settings.sites.items():
            site_apps[site_cfg.subdomain] = _build_site_app(
                settings=settings,
                site_name=name,
                site_config=site_cfg,
                db_config=db_config,
                session_config=session_config,
                csrf_config=csrf_config,
                security_middleware=security_middleware,
                rate_limit_middleware=rate_limit_middleware,
                user_middleware=user_middleware,
                static_url=static_url,
                themes_dir=themes_dir,
                site_static_dir=site_static_dir,
                package_static_dir=package_static_dir,
                page_types=subdomain_page_types.pop(site_cfg.subdomain, []),
            )

        # Auto-create apps for subdomains referenced only by page types
        for subdomain, pts in subdomain_page_types.items():
            auto_cfg = SiteConfig(subdomain=subdomain)
            site_apps[subdomain] = _build_site_app(
                settings=settings,
                site_name=subdomain,
                site_config=auto_cfg,
                db_config=db_config,
                session_config=session_config,
                csrf_config=csrf_config,
                security_middleware=security_middleware,
                rate_limit_middleware=rate_limit_middleware,
                user_middleware=user_middleware,
                static_url=static_url,
                themes_dir=themes_dir,
                site_static_dir=site_static_dir,
                package_static_dir=package_static_dir,
                page_types=pts,
            )

        if forced_subdomain:
            if forced_subdomain not in site_apps:
                available = ", ".join(sorted(site_apps.keys())) or "(none)"
                raise SystemExit(
                    f"Subdomain '{forced_subdomain}' not found. "
                    f"Available subdomains: {available}"
                )

        return SiteDispatcher(
            primary_app=primary_asgi,
            site_apps=site_apps,
            domain=settings.domain or "localhost",
            force_subdomain=forced_subdomain,
        )

    return primary_asgi


def create_setup_app() -> Litestar:
    """Create an application for the setup wizard.

    This app handles only setup routes (/setup/*, /auth/*, /static/*).
    The AppDispatcher handles routing non-setup paths.
    """
    from pydantic_settings import BaseSettings
    from skrift.setup.state import get_database_url_from_yaml

    class MinimalSettings(BaseSettings):
        debug: bool = True
        secret_key: str = "setup-wizard-temporary-secret-key-change-me"

    settings = MinimalSettings()

    # Session configuration
    session_config = create_session_config(settings.secret_key)

    # Static files
    from skrift.lib.theme import get_themes_dir
    themes_dir = get_themes_dir()
    site_static_dir = Path(os.getcwd()) / "static"
    package_static_dir = Path(__file__).parent / "static"
    static_url = create_static_hasher(
        themes_dir=themes_dir,
        site_static_dir=site_static_dir,
        package_static_dir=package_static_dir,
    )

    # Template configuration (setup app never uses themes)
    from skrift.app_factory import get_template_directories_for_theme
    setup_template_dirs = get_template_directories_for_theme("")
    engine_callback = build_template_engine_callback(
        extra_globals={
            "site_name": lambda: "Skrift",
            "site_tagline": lambda: "Setup",
            "site_copyright_holder": lambda: "",
            "site_copyright_start_year": lambda: None,
            "static_url": static_url,
        },
    )
    template_config = create_template_config(setup_template_dirs, engine_callback)

    # Import controllers
    from skrift.setup.controller import SetupController, SetupAuthController

    # Check if database is configured - if so, include SQLAlchemy
    db_url = get_database_url_from_yaml()

    # Also try to get the raw db URL from config (before env var resolution)
    if not db_url:
        config_path = get_config_path()
        if config_path.exists():
            try:
                with open(config_path, "r") as f:
                    raw_config = yaml.safe_load(f)
                raw_db_url = raw_config.get("db", {}).get("url", "")
                # If it's an env var reference but env var isn't set,
                # check if there's a local SQLite fallback we can use
                if raw_db_url.startswith("$"):
                    for db_file in ["./app.db", "./data.db", "./skrift.db"]:
                        if Path(db_file).exists():
                            db_url = f"sqlite+aiosqlite:///{db_file}"
                            break
            except Exception:
                logger.debug("Could not resolve raw DB URL", exc_info=True)

    plugins = []
    route_handlers = [SetupController, SetupAuthController]
    db_config: SQLAlchemyAsyncConfig | None = None

    if db_url:
        # Database schema configuration (mirrors create_app)
        from skrift.setup.state import get_database_schema_from_yaml
        db_schema = get_database_schema_from_yaml()
        if db_schema and "sqlite" not in db_url:
            Base.metadata.schema = db_schema

        # Database is configured, add SQLAlchemy plugin
        if "sqlite" in db_url:
            engine_config = EngineConfig(echo=False)
        else:
            engine_kwargs: dict[str, Any] = dict(
                pool_size=5,
                max_overflow=10,
                pool_timeout=30,
                pool_pre_ping=True,
                echo=False,
            )
            if db_schema:
                engine_kwargs["execution_options"] = {
                    "schema_translate_map": {None: db_schema},
                }
            engine_config = EngineConfig(**engine_kwargs)

        db_config = SQLAlchemyAsyncConfig(
            connection_string=db_url,
            metadata=Base.metadata,
            create_all=False,
            session_config=AsyncSessionConfig(expire_on_commit=False),
            engine_config=engine_config,
            session_maker_app_state_key="setup_session_maker_class",
        )
        plugins.append(SQLAlchemyPlugin(config=db_config))

    async def on_startup(_app: Litestar) -> None:
        """Initialize setup state and sync roles if database is available."""
        if db_config is not None:
            try:
                from skrift.auth import sync_roles_to_database
                async with db_config.get_session() as session:
                    await sync_roles_to_database(session)
            except Exception:
                logger.info("Setup startup: role sync skipped", exc_info=True)

    from skrift.middleware.static import StaticFilesMiddleware
    litestar_app = Litestar(
        on_startup=[on_startup],
        route_handlers=route_handlers,
        plugins=plugins,
        middleware=[DefineMiddleware(SessionCleanupMiddleware), session_config.middleware],
        template_config=template_config,
        compression_config=CompressionConfig(backend="gzip"),
        exception_handlers=EXCEPTION_HANDLERS,
        debug=settings.debug,
    )
    return StaticFilesMiddleware(
        litestar_app,
        themes_dir=themes_dir,
        site_static_dir=site_static_dir,
        package_static_dir=package_static_dir,
    )


async def check_setup_in_db(db_url: str) -> bool:
    """Check if setup is complete by querying the database directly."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from skrift.setup.state import create_setup_engine

    engine = create_setup_engine(db_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with async_session() as session:
            value = await get_setting(session, SETUP_COMPLETED_AT_KEY)
            return value is not None
    except Exception:
        logger.debug("DB setup check query failed", exc_info=True)
        return False
    finally:
        await engine.dispose()


def create_dispatcher() -> ASGIApp:
    """Create the ASGI app dispatcher.

    This is the main entry point. The dispatcher handles routing between
    setup and main apps, with lazy creation of the main app after setup completes.
    """
    # CRITICAL: Check for dummy auth in production BEFORE anything else
    from skrift.setup.providers import validate_no_dummy_auth_in_production
    validate_no_dummy_auth_in_production()

    global _dispatcher
    from skrift.setup.state import get_database_url_from_yaml

    # Get database URL first
    db_url: str | None = None
    try:
        db_url = get_database_url_from_yaml()
    except Exception:
        logger.debug("Could not get database URL at startup", exc_info=True)

    # Check if setup is already complete
    initial_setup_complete = False
    if db_url:
        try:
            initial_setup_complete = asyncio.get_event_loop().run_until_complete(
                check_setup_in_db(db_url)
            )
        except RuntimeError:
            # No running event loop, try creating one
            try:
                initial_setup_complete = asyncio.run(check_setup_in_db(db_url))
            except Exception:
                logger.debug("Async setup check failed", exc_info=True)
        except Exception:
            logger.debug("Event loop setup check failed", exc_info=True)

    # Also check if config is valid
    config_valid, _ = is_config_valid()

    if initial_setup_complete and config_valid:
        # Setup already done - just return the main app directly
        return create_app()

    setup_app = create_setup_app()

    # Try to create main app if config is valid
    main_app: Litestar | None = None
    if config_valid:
        try:
            main_app = create_app()
        except Exception:
            logger.debug("Could not pre-create main app", exc_info=True)
    dispatcher = AppDispatcher(setup_app=setup_app, db_url=db_url, main_app=main_app)
    dispatcher.setup_locked = initial_setup_complete
    _dispatcher = dispatcher  # Store reference for later updates
    return dispatcher


app = create_dispatcher()
