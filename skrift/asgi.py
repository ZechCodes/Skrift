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
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from advanced_alchemy.config import EngineConfig
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)

from skrift.db.session import SafeSQLAlchemyAsyncConfig
from litestar import Litestar
from litestar.config.compression import CompressionConfig
from litestar.config.csrf import CSRFConfig as LitestarCSRFConfig
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.exceptions import HTTPException
from litestar.middleware import DefineMiddleware
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.static_files import create_static_files_router
from litestar.template import TemplateConfig
from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.config import get_config_path, get_settings, is_config_valid
from skrift.middleware.security import SecurityHeadersMiddleware
from skrift.db.base import Base
from skrift.db.services.setting_service import (
    load_site_settings_cache,
    get_cached_site_name,
    get_cached_site_tagline,
    get_cached_site_copyright_holder,
    get_cached_site_copyright_start_year,
    get_setting,
    SETUP_COMPLETED_AT_KEY,
)
from skrift.lib.exceptions import http_exception_handler, internal_server_error_handler
from skrift.lib.markdown import render_markdown


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
                pass

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

        # If setup is locked and we have main app, it handles EVERYTHING
        if self.setup_locked and self._main_app:
            await self._main_app(scope, receive, send)
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
                pass

        if not db_url:
            return False

        try:
            return await check_setup_in_db(db_url)
        except Exception:
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
            # Fallback if template rendering fails
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


def create_app() -> Litestar:
    """Create and configure the main Litestar application.

    This app has all routes for normal operation. It is used by the dispatcher
    after setup is complete.
    """
    # CRITICAL: Check for dummy auth in production BEFORE anything else
    from skrift.setup.providers import validate_no_dummy_auth_in_production
    validate_no_dummy_auth_in_production()

    settings = get_settings()

    # Load controllers from app.yaml
    controllers = load_controllers()

    # Load middleware from app.yaml
    user_middleware = load_middleware()

    # Database configuration
    if "sqlite" in settings.db.url:
        engine_config = EngineConfig(echo=settings.db.echo)
    else:
        engine_config = EngineConfig(
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.pool_overflow,
            pool_timeout=settings.db.pool_timeout,
            pool_pre_ping=settings.db.pool_pre_ping,
            echo=settings.db.echo,
        )

    db_config = SafeSQLAlchemyAsyncConfig(
        connection_string=settings.db.url,
        metadata=Base.metadata,
        create_all=False,
        session_config=AsyncSessionConfig(expire_on_commit=False),
        engine_config=engine_config,
    )

    # Session configuration (client-side encrypted cookies)
    session_secret = hashlib.sha256(settings.secret_key.encode()).digest()
    session_config = CookieBackendConfig(
        secret=session_secret,
        max_age=60 * 60 * 24 * 7,  # 7 days
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
        domain=settings.session.cookie_domain,
    )

    # Security headers middleware
    security_middleware = []
    if settings.security_headers.enabled:
        headers = settings.security_headers.build_headers(debug=settings.debug)
        if headers:
            security_middleware = [
                DefineMiddleware(SecurityHeadersMiddleware, headers=headers, debug=settings.debug)
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

    # Template configuration
    # Search working directory first for user overrides, then package directory
    working_dir_templates = Path(os.getcwd()) / "templates"
    template_dir = Path(__file__).parent / "templates"
    from skrift.forms import Form

    def configure_template_engine(engine):
        engine.engine.globals.update({
            "now": datetime.now,
            "site_name": get_cached_site_name,
            "site_tagline": get_cached_site_tagline,
            "site_copyright_holder": get_cached_site_copyright_holder,
            "site_copyright_start_year": get_cached_site_copyright_start_year,
            "Form": Form,
        })
        engine.engine.filters.update({"markdown": render_markdown})

    template_config = TemplateConfig(
        directory=[working_dir_templates, template_dir],
        engine=JinjaTemplateEngine,
        engine_callback=configure_template_engine,
    )

    # Static files - working directory first for user overrides, then package directory
    working_dir_static = Path(os.getcwd()) / "static"
    static_files_router = create_static_files_router(
        path="/static",
        directories=[working_dir_static, Path(__file__).parent / "static"],
    )

    from skrift.auth import sync_roles_to_database

    async def on_startup(_app: Litestar) -> None:
        """Sync roles and load site settings on startup."""
        try:
            async with db_config.get_session() as session:
                await sync_roles_to_database(session)
                await load_site_settings_cache(session)
        except Exception:
            # Database might not exist yet during setup
            pass

    return Litestar(
        on_startup=[on_startup],
        route_handlers=[*controllers, static_files_router],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        middleware=[*security_middleware, session_config.middleware, *user_middleware],
        template_config=template_config,
        compression_config=CompressionConfig(backend="gzip"),
        csrf_config=csrf_config,
        exception_handlers={
            HTTPException: http_exception_handler,
            Exception: internal_server_error_handler,
        },
        debug=settings.debug,
    )


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
    session_secret = hashlib.sha256(settings.secret_key.encode()).digest()
    session_config = CookieBackendConfig(
        secret=session_secret,
        max_age=60 * 60 * 24,  # 1 day
        httponly=True,
        secure=False,
        samesite="lax",
    )

    # Template configuration
    # Search working directory first for user overrides, then package directory
    working_dir_templates = Path(os.getcwd()) / "templates"
    template_dir = Path(__file__).parent / "templates"

    def configure_setup_template_engine(engine):
        engine.engine.globals.update({
            "now": datetime.now,
            "site_name": lambda: "Skrift",
            "site_tagline": lambda: "Setup",
            "site_copyright_holder": lambda: "",
            "site_copyright_start_year": lambda: None,
        })
        engine.engine.filters.update({"markdown": render_markdown})

    template_config = TemplateConfig(
        directory=[working_dir_templates, template_dir],
        engine=JinjaTemplateEngine,
        engine_callback=configure_setup_template_engine,
    )

    # Static files - working directory first for user overrides, then package directory
    working_dir_static = Path(os.getcwd()) / "static"
    static_files_router = create_static_files_router(
        path="/static",
        directories=[working_dir_static, Path(__file__).parent / "static"],
    )

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
                pass

    plugins = []
    route_handlers = [SetupController, SetupAuthController, static_files_router]
    db_config: SQLAlchemyAsyncConfig | None = None

    if db_url:
        # Database is configured, add SQLAlchemy plugin
        if "sqlite" in db_url:
            engine_config = EngineConfig(echo=False)
        else:
            engine_config = EngineConfig(
                pool_size=5,
                max_overflow=10,
                pool_timeout=30,
                pool_pre_ping=True,
                echo=False,
            )

        db_config = SafeSQLAlchemyAsyncConfig(
            connection_string=db_url,
            metadata=Base.metadata,
            create_all=False,
            session_config=AsyncSessionConfig(expire_on_commit=False),
            engine_config=engine_config,
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
                pass

    return Litestar(
        on_startup=[on_startup],
        route_handlers=route_handlers,
        plugins=plugins,
        middleware=[session_config.middleware],
        template_config=template_config,
        compression_config=CompressionConfig(backend="gzip"),
        exception_handlers={
            HTTPException: http_exception_handler,
            Exception: internal_server_error_handler,
        },
        debug=settings.debug,
    )


async def check_setup_in_db(db_url: str) -> bool:
    """Check if setup is complete by querying the database directly."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine(db_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with async_session() as session:
            value = await get_setting(session, SETUP_COMPLETED_AT_KEY)
            return value is not None
    except Exception:
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
        pass

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
                pass
        except Exception:
            pass

    # Also check if config is valid
    config_valid, _ = is_config_valid()

    if initial_setup_complete and config_valid:
        # Setup already done - just return the main app directly
        return create_app()

    # Try to create main app if config is valid
    main_app: Litestar | None = None
    if config_valid:
        try:
            main_app = create_app()
        except Exception:
            pass

    # Always use dispatcher - it handles lazy main app creation
    setup_app = create_setup_app()
    dispatcher = AppDispatcher(setup_app=setup_app, db_url=db_url, main_app=main_app)
    dispatcher.setup_locked = initial_setup_complete
    _dispatcher = dispatcher  # Store reference for later updates
    return dispatcher


app = create_dispatcher()
