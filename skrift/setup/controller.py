"""Setup wizard controller for first-time Skrift configuration."""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import File, Redirect, Template as TemplateResponse
from litestar.response.sse import ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skrift.db.models.role import Role, user_roles
from skrift.db.services import setting_service
from skrift.db.services.setting_service import (
    SETUP_COMPLETED_AT_KEY,
    get_setting,
)
from skrift.setup.config_writer import (
    load_config,
    update_auth_config,
    update_database_config,
)
from skrift.setup.providers import DUMMY_PROVIDER_KEY, OAUTH_PROVIDERS, get_all_providers, get_provider_info
from skrift.setup.state import (
    can_connect_to_database,
    create_setup_engine,
    get_database_url_from_yaml,
    get_first_incomplete_step,
    is_auth_configured,
    is_site_configured,
    is_theme_configured,
    run_migrations_if_needed,
    reset_migrations_flag,
)


@asynccontextmanager
async def get_setup_db_session():
    """Create a database session for setup operations.

    This is used during setup when the SQLAlchemy plugin isn't available.
    """
    db_url = get_database_url_from_yaml()
    if not db_url:
        raise RuntimeError("Database not configured")

    engine = create_setup_engine(db_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await engine.dispose()


def _resolve_env_var(value: str) -> str:
    """Resolve environment variable reference if value starts with $."""
    import os
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


def _has_themes() -> bool:
    """Check if themes are available for the setup wizard."""
    from skrift.lib.theme import themes_available
    return themes_available()


def _total_steps() -> int:
    """Return total number of setup steps (5 with themes, 4 without)."""
    return 5 if _has_themes() else 4


def _admin_step_number() -> int:
    """Return the step number for the admin step."""
    return 5 if _has_themes() else 4


def _theme_step_number() -> int:
    """Return the step number for the theme step (only valid when themes exist)."""
    return 4


class SetupController(Controller):
    """Controller for the setup wizard."""

    path = "/setup"

    async def _check_already_complete(self) -> bool:
        """Defense in depth: check if setup is already complete."""
        try:
            async with get_setup_db_session() as db_session:
                value = await get_setting(db_session, SETUP_COMPLETED_AT_KEY)
                return value is not None
        except Exception:
            return False

    @get("/")
    async def index(self, request: Request) -> Redirect:
        """Redirect to welcome page to begin setup."""
        return Redirect(path="/setup/welcome")

    @get("/welcome")
    async def welcome_step(self, request: Request) -> TemplateResponse:
        """Welcome page shown before setup begins."""
        return TemplateResponse("setup/welcome.html")

    @get("/database")
    async def database_step(self, request: Request) -> TemplateResponse | Redirect:
        """Step 1: Database configuration."""
        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)

        # If database is already configured and no errors, go to configuring page
        can_connect, _ = await can_connect_to_database()
        if can_connect and not error:
            return Redirect(path="/setup/configuring")

        # Load current config if exists
        config = load_config()
        db_config = config.get("db", {})
        current_url = db_config.get("url", "")

        # Determine current type
        db_type = "sqlite"
        if "postgresql" in current_url:
            db_type = "postgresql"

        return TemplateResponse(
            "setup/database.html",
            context={
                "flash": flash,
                "error": error,
                "step": 1,
                "total_steps": _total_steps(),
                "db_type": db_type,
                "current_url": current_url,
            },
        )

    @post("/database")
    async def save_database(self, request: Request) -> Redirect:
        """Save database configuration."""
        form_data = await request.form()
        db_type = form_data.get("db_type", "sqlite")

        try:
            if db_type == "sqlite":
                file_path = form_data.get("sqlite_path", "./app.db")
                use_env = form_data.get("sqlite_path_env") == "on"

                update_database_config(
                    db_type="sqlite",
                    url=file_path,
                    use_env_vars={"url": use_env},
                )
            else:
                # PostgreSQL
                use_env_url = form_data.get("pg_url_env") == "on"

                if use_env_url:
                    env_var = form_data.get("pg_url_envvar", "DATABASE_URL")
                    update_database_config(
                        db_type="postgresql",
                        url=env_var,
                        use_env_vars={"url": True},
                    )
                else:
                    host = form_data.get("pg_host", "localhost")
                    port = int(form_data.get("pg_port", 5432))
                    database = form_data.get("pg_database", "skrift")
                    username = form_data.get("pg_username", "postgres")
                    password = form_data.get("pg_password", "")

                    update_database_config(
                        db_type="postgresql",
                        host=host,
                        port=port,
                        database=database,
                        username=username,
                        password=password,
                    )

            # Test connection
            can_connect, error = await can_connect_to_database()
            if not can_connect:
                logger.warning("Setup database: connection test failed: %s", error)
                request.session["setup_error"] = f"Connection failed: {error}"
                return Redirect(path="/setup/database")

            # Connection successful - redirect to configuring page to run migrations
            request.session["setup_wizard_step"] = "configuring"
            return Redirect(path="/setup/configuring")

        except Exception as e:
            logger.error("Setup database: unexpected error saving config: %s", e, exc_info=True)
            request.session["setup_error"] = str(e)
            return Redirect(path="/setup/database")

    @get("/restart")
    async def restart_step(self, request: Request) -> Redirect:
        """Legacy restart route - now redirects to auth since restart is no longer required."""
        request.session["setup_wizard_step"] = "auth"
        return Redirect(path="/setup/auth")

    @get("/configuring")
    async def configuring_step(self, request: Request) -> TemplateResponse | Redirect:
        """Database configuration in progress page.

        Shows a loading spinner while migrations run via SSE.
        """
        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)

        # Verify we can connect to the database first
        can_connect, connection_error = await can_connect_to_database()
        if not can_connect:
            request.session["setup_error"] = f"Cannot connect to database: {connection_error}"
            return Redirect(path="/setup/database")

        # Reset migrations flag so they run fresh via SSE
        reset_migrations_flag()

        return TemplateResponse(
            "setup/configuring.html",
            context={
                "flash": flash,
                "error": error,
                "step": 1,
                "total_steps": _total_steps(),
            },
        )

    @get("/configuring/status")
    async def configuring_status(self, request: Request) -> ServerSentEvent:
        """SSE endpoint for database configuration status.

        Streams migration progress and completion status.
        """
        async def generate_status() -> AsyncGenerator[str, None]:
            # Send initial status
            yield json.dumps({
                "status": "running",
                "message": "Testing database connection...",
                "detail": "",
            })

            await asyncio.sleep(0.5)

            # Test connection
            can_connect, connection_error = await can_connect_to_database()
            if not can_connect:
                logger.error("Setup configuring: database connection failed: %s", connection_error)
                yield json.dumps({
                    "status": "error",
                    "message": f"Database connection failed: {connection_error}",
                })
                return

            yield json.dumps({
                "status": "running",
                "message": "Running database migrations...",
                "detail": "This may take a moment",
            })

            await asyncio.sleep(0.3)

            # Run migrations
            success, error = run_migrations_if_needed()

            if not success:
                logger.error("Setup configuring: migration failed: %s", error)
                yield json.dumps({
                    "status": "error",
                    "message": f"Migration failed: {error}",
                })
                return

            yield json.dumps({
                "status": "running",
                "message": "Verifying database schema...",
                "detail": "",
            })

            await asyncio.sleep(0.3)

            # Determine next step
            next_setup_step = await get_first_incomplete_step()
            next_step = next_setup_step.value

            # All done - include next step
            yield json.dumps({
                "status": "complete",
                "message": "Database configured successfully!",
                "next_step": next_step,
            })

        return ServerSentEvent(generate_status())

    @get("/auth")
    async def auth_step(self, request: Request) -> TemplateResponse | Redirect:
        """Step 2: Authentication providers."""
        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)

        # If auth is already configured and no errors, skip to next step
        if is_auth_configured() and not error:
            next_step = await get_first_incomplete_step()
            if next_step.value != "auth":
                request.session["setup_wizard_step"] = next_step.value
                return Redirect(path=f"/setup/{next_step.value}")

        # Get current redirect URL from request
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        redirect_base_url = f"{scheme}://{host}"

        # Get configured providers
        config = load_config()
        auth_config = config.get("auth", {})
        configured_providers = auth_config.get("providers", {})

        # Get all available providers
        all_providers = get_all_providers()

        return TemplateResponse(
            "setup/auth.html",
            context={
                "flash": flash,
                "error": error,
                "step": 2,
                "total_steps": _total_steps(),
                "redirect_base_url": redirect_base_url,
                "providers": all_providers,
                "configured_providers": configured_providers,
            },
        )

    @post("/auth")
    async def save_auth(self, request: Request) -> Redirect:
        """Save authentication configuration."""
        form_data = await request.form()

        # Get redirect base URL
        redirect_base_url = form_data.get("redirect_base_url", "http://localhost:8000")

        # Parse provider configurations
        all_providers = get_all_providers()
        providers = {}
        use_env_vars = {}

        for provider_key in all_providers.keys():
            enabled = form_data.get(f"{provider_key}_enabled") == "on"
            if not enabled:
                continue

            provider_info = all_providers[provider_key]
            provider_config = {}
            provider_env_vars = {}

            for field in provider_info.fields:
                field_key = field["key"]
                value = form_data.get(f"{provider_key}_{field_key}", "")
                use_env = form_data.get(f"{provider_key}_{field_key}_env") == "on"

                if value or not field.get("optional"):
                    provider_config[field_key] = value
                    provider_env_vars[field_key] = use_env

            if provider_config:
                providers[provider_key] = provider_config
                use_env_vars[provider_key] = provider_env_vars

        if not providers:
            request.session["setup_error"] = "Please configure at least one authentication provider"
            return Redirect(path="/setup/auth")

        try:
            update_auth_config(
                redirect_base_url=redirect_base_url,
                providers=providers,
                use_env_vars=use_env_vars,
            )

            # Determine next step using smart detection
            next_step = await get_first_incomplete_step()
            request.session["setup_wizard_step"] = next_step.value
            request.session["flash"] = "Authentication configured successfully!"
            return Redirect(path=f"/setup/{next_step.value}")

        except Exception as e:
            request.session["setup_error"] = str(e)
            return Redirect(path="/setup/auth")

    @get("/site")
    async def site_step(self, request: Request) -> TemplateResponse | Redirect:
        """Step 3: Site settings."""
        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)

        # If site is already configured and no errors, skip to next step
        if await is_site_configured() and not error:
            next_step = await get_first_incomplete_step()
            if next_step.value != "site":
                request.session["setup_wizard_step"] = next_step.value
                return Redirect(path=f"/setup/{next_step.value}")

        return TemplateResponse(
            "setup/site.html",
            context={
                "flash": flash,
                "error": error,
                "step": 3,
                "total_steps": _total_steps(),
                "settings": {
                    "site_name": "",
                    "site_tagline": "",
                    "site_copyright_holder": "",
                    "site_copyright_start_year": datetime.now().year,
                },
            },
        )

    @post("/site")
    async def save_site(self, request: Request) -> Redirect:
        """Save site settings."""
        form_data = await request.form()

        try:
            site_name = form_data.get("site_name", "").strip()
            if not site_name:
                request.session["setup_error"] = "Site name is required"
                return Redirect(path="/setup/site")

            site_tagline = form_data.get("site_tagline", "").strip()
            site_copyright_holder = form_data.get("site_copyright_holder", "").strip()
            site_copyright_start_year = form_data.get("site_copyright_start_year", "").strip()

            # Save settings to database using manual session
            async with get_setup_db_session() as db_session:
                await setting_service.set_setting(
                    db_session, setting_service.SITE_NAME_KEY, site_name
                )
                await setting_service.set_setting(
                    db_session, setting_service.SITE_TAGLINE_KEY, site_tagline
                )
                await setting_service.set_setting(
                    db_session, setting_service.SITE_COPYRIGHT_HOLDER_KEY, site_copyright_holder
                )
                await setting_service.set_setting(
                    db_session,
                    setting_service.SITE_COPYRIGHT_START_YEAR_KEY,
                    site_copyright_start_year,
                )

                # Handle optional favicon upload
                favicon_file = form_data.get("favicon")
                if favicon_file and hasattr(favicon_file, "read"):
                    content = await favicon_file.read()
                    if content:
                        from skrift.config import get_settings as get_app_settings
                        from skrift.lib.storage import StorageManager
                        from skrift.db.services.asset_service import upload_asset

                        app_settings = get_app_settings()
                        storage = StorageManager(app_settings.storage)
                        try:
                            asset = await upload_asset(
                                db_session,
                                storage,
                                filename=favicon_file.filename or "favicon",
                                data=content,
                                content_type=favicon_file.content_type or "image/png",
                            )
                            await setting_service.set_setting(
                                db_session, setting_service.SITE_FAVICON_KEY, asset.key
                            )
                        finally:
                            await storage.close()

                # Reload cache
                await setting_service.load_site_settings_cache(db_session)

            # Determine next step using smart detection - should be admin at this point
            next_step = await get_first_incomplete_step()
            request.session["setup_wizard_step"] = next_step.value
            request.session["flash"] = "Site settings saved!"
            return Redirect(path=f"/setup/{next_step.value}")

        except Exception as e:
            request.session["setup_error"] = str(e)
            return Redirect(path="/setup/site")

    @get("/theme")
    async def theme_step(self, request: Request) -> TemplateResponse | Redirect:
        """Step 4: Theme selection (only when themes are available)."""
        from skrift.lib.theme import themes_available, discover_themes

        if not themes_available():
            return Redirect(path="/setup/admin")

        # If theme is already configured and no errors, skip
        if await is_theme_configured() and not request.session.get("setup_error"):
            next_step = await get_first_incomplete_step()
            if next_step.value != "theme":
                request.session["setup_wizard_step"] = next_step.value
                return Redirect(path=f"/setup/{next_step.value}")

        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)
        themes = discover_themes()

        return TemplateResponse(
            "setup/theme.html",
            context={
                "flash": flash,
                "error": error,
                "step": _theme_step_number(),
                "total_steps": _total_steps(),
                "themes": themes,
            },
        )

    @post("/theme")
    async def save_theme(self, request: Request) -> Redirect:
        """Save theme selection."""
        from skrift.lib.theme import get_theme_info

        form_data = await request.form()
        site_theme = form_data.get("site_theme", "").strip()

        # Validate: if non-empty, must be a valid theme
        if site_theme and not get_theme_info(site_theme):
            request.session["setup_error"] = f"Unknown theme: {site_theme}"
            return Redirect(path="/setup/theme")

        try:
            async with get_setup_db_session() as db_session:
                await setting_service.set_setting(
                    db_session, setting_service.SITE_THEME_KEY, site_theme
                )
                await setting_service.load_site_settings_cache(db_session)

            next_step = await get_first_incomplete_step()
            request.session["setup_wizard_step"] = next_step.value
            request.session["flash"] = "Theme saved!"
            return Redirect(path=f"/setup/{next_step.value}")

        except Exception as e:
            request.session["setup_error"] = str(e)
            return Redirect(path="/setup/theme")

    @get("/theme-screenshot/{name:str}")
    async def theme_screenshot(self, request: Request, name: str) -> "File":
        """Serve a theme's screenshot image."""
        from litestar.response import File
        from skrift.lib.theme import get_theme_info

        info = get_theme_info(name)
        if not info or not info.screenshot:
            raise HTTPException(status_code=404, detail="Screenshot not found")

        return File(path=info.screenshot, media_type="image/png")

    @get("/admin")
    async def admin_step(self, request: Request) -> TemplateResponse:
        """Admin account creation step."""
        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)

        # Get configured providers
        config = load_config()
        auth_config = config.get("auth", {})
        configured_providers = list(auth_config.get("providers", {}).keys())

        # Get provider display info (include dummy — get_all_providers() excludes it)
        provider_info = {
            key: OAUTH_PROVIDERS[key] for key in configured_providers if key in OAUTH_PROVIDERS
        }

        return TemplateResponse(
            "setup/admin.html",
            context={
                "flash": flash,
                "error": error,
                "step": _admin_step_number(),
                "total_steps": _total_steps(),
                "providers": provider_info,
                "configured_providers": configured_providers,
            },
        )

    @get("/oauth/{provider:str}/login")
    async def setup_oauth_login(self, request: Request, provider: str) -> Redirect | TemplateResponse:
        """Redirect to OAuth provider for setup admin creation."""
        config = load_config()
        auth_config = config.get("auth", {})
        providers_config = auth_config.get("providers", {})

        if provider not in providers_config:
            raise HTTPException(status_code=404, detail=f"Provider {provider} not configured")

        provider_info = get_provider_info(provider)
        if not provider_info:
            raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

        # Dummy provider uses a local form instead of OAuth redirect
        if provider == DUMMY_PROVIDER_KEY:
            flash = request.session.pop("flash", None)
            return TemplateResponse(
                "setup/dummy_login.html",
                context={
                    "flash": flash,
                    "step": 4,
                    "total_steps": 4,
                },
            )

        provider_config = providers_config[provider]
        client_id = _resolve_env_var(provider_config.get("client_id", ""))

        # Generate CSRF state token
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        request.session["oauth_provider"] = provider
        request.session["oauth_setup"] = True

        # Build redirect URI
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        redirect_uri = f"{scheme}://{host}/auth/{provider}/callback"

        scopes = provider_config.get("scopes", provider_info.scopes)

        # Get the provider strategy for PKCE + auth params
        from skrift.auth.providers import get_oauth_provider
        oauth_provider = get_oauth_provider(provider)

        # Generate PKCE for providers that require it
        code_challenge = None
        if oauth_provider.requires_pkce:
            code_verifier = secrets.token_urlsafe(64)[:128]
            request.session["oauth_code_verifier"] = code_verifier
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode().rstrip("=")

        # Resolve tenant
        tenant = provider_config.get("tenant_id", "common")
        if isinstance(tenant, str) and tenant.startswith("$"):
            tenant = _resolve_env_var(tenant) or "common"

        auth_url = oauth_provider.resolve_url(provider_info.auth_url, tenant)
        params = oauth_provider.build_auth_params(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=code_challenge,
        )

        return Redirect(path=f"{auth_url}?{urlencode(params)}")

    @post("/dummy-login")
    async def setup_dummy_login(self, request: Request) -> Redirect:
        """Process dummy login form during setup to create admin account."""
        config = load_config()
        auth_config = config.get("auth", {})
        providers_config = auth_config.get("providers", {})

        if DUMMY_PROVIDER_KEY not in providers_config:
            raise HTTPException(status_code=404, detail="Dummy provider not configured")

        form_data = await request.form()
        email = form_data.get("email", "").strip()
        name = form_data.get("name", "").strip()

        if not email:
            request.session["flash"] = "Email is required"
            return Redirect(path=f"/setup/oauth/{DUMMY_PROVIDER_KEY}/login")

        if not name:
            name = email.split("@")[0]

        oauth_id = f"dummy_{hashlib.sha256(email.encode()).hexdigest()[:16]}"
        dummy_metadata = {"id": oauth_id, "email": email, "name": name}

        from skrift.auth.providers import NormalizedUserData
        user_data = NormalizedUserData(
            oauth_id=oauth_id, email=email, name=name, picture_url=None
        )

        async with get_setup_db_session() as db_session:
            from skrift.auth.oauth_account_service import find_or_create_oauth_user
            login_result = await find_or_create_oauth_user(
                db_session, DUMMY_PROVIDER_KEY, user_data, dummy_metadata
            )
            user = login_result.user

            # Ensure roles are synced
            from skrift.auth import sync_roles_to_database
            await sync_roles_to_database(db_session)

            # Always assign admin role during setup
            admin_role = await db_session.scalar(select(Role).where(Role.name == "admin"))
            if admin_role:
                existing = await db_session.execute(
                    select(user_roles).where(
                        user_roles.c.user_id == user.id,
                        user_roles.c.role_id == admin_role.id
                    )
                )
                if not existing.first():
                    await db_session.execute(
                        user_roles.insert().values(user_id=user.id, role_id=admin_role.id)
                    )

            # Mark setup complete
            timestamp = datetime.now(UTC).isoformat()
            await setting_service.set_setting(db_session, SETUP_COMPLETED_AT_KEY, timestamp)

        # Set session
        request.session["user_id"] = str(user.id)
        request.session["user_name"] = user.name
        request.session["user_email"] = user.email
        request.session["user_picture_url"] = user.picture_url
        request.session["flash"] = "Admin account created successfully!"
        request.session["setup_just_completed"] = True

        return Redirect(path="/setup/complete")

    @get("/complete")
    async def complete(self, request: Request) -> TemplateResponse | Redirect:
        """Setup complete page."""
        # Verify setup is actually complete in database
        if not await self._check_already_complete():
            return Redirect(path="/setup")

        # Clear the session flag if present
        request.session.pop("setup_just_completed", None)

        return TemplateResponse(
            "setup/complete.html",
            context={
                "step": _admin_step_number(),
                "total_steps": _total_steps(),
            },
        )


async def mark_setup_complete(db_session: AsyncSession | None = None) -> None:
    """Mark setup as complete by setting the timestamp.

    Args:
        db_session: Optional database session. If not provided, creates one.
    """
    timestamp = datetime.now(UTC).isoformat()
    if db_session:
        await setting_service.set_setting(db_session, SETUP_COMPLETED_AT_KEY, timestamp)
    else:
        async with get_setup_db_session() as session:
            await setting_service.set_setting(session, SETUP_COMPLETED_AT_KEY, timestamp)


class SetupAuthController(Controller):
    """Auth controller for setup OAuth callbacks.

    This handles OAuth callbacks at /auth/{provider}/callback during setup,
    matching the redirect URI configured in OAuth providers.
    """

    path = "/auth"

    @get("/{provider:str}/callback")
    async def setup_oauth_callback(
        self,
        request: Request,
        provider: str,
        code: str | None = None,
        oauth_state: Annotated[str | None, Parameter(query="state")] = None,
        error: str | None = None,
    ) -> Redirect:
        """Handle OAuth callback during setup."""
        if not request.session.get("oauth_setup"):
            raise HTTPException(status_code=400, detail="Invalid OAuth flow")

        if error:
            request.session["setup_error"] = f"OAuth error: {error}"
            return Redirect(path="/setup/admin")

        # Verify CSRF state
        stored_state = request.session.pop("oauth_state", None)
        if not oauth_state or oauth_state != stored_state:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not code:
            raise HTTPException(status_code=400, detail="Missing authorization code")

        config = load_config()
        auth_config = config.get("auth", {})
        providers_config = auth_config.get("providers", {})

        if provider not in providers_config:
            raise HTTPException(status_code=404, detail=f"Provider {provider} not configured")

        provider_config = providers_config[provider]
        client_id = _resolve_env_var(provider_config.get("client_id", ""))
        client_secret = _resolve_env_var(provider_config.get("client_secret", ""))

        # Build redirect URI
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        redirect_uri = f"{scheme}://{host}/auth/{provider}/callback"

        code_verifier = request.session.pop("oauth_code_verifier", None)

        # Resolve tenant for Microsoft
        tenant = provider_config.get("tenant_id", "common")
        if isinstance(tenant, str) and tenant.startswith("$"):
            tenant = _resolve_env_var(tenant) or "common"

        from skrift.controllers.auth import _exchange_and_fetch
        user_data, user_info, tokens = await _exchange_and_fetch(
            provider, None, code, redirect_uri, code_verifier,
            client_id=client_id, client_secret=client_secret, tenant=tenant,
        )

        # Create user and mark setup complete
        async with get_setup_db_session() as db_session:
            from skrift.auth.oauth_account_service import find_or_create_oauth_user
            login_result = await find_or_create_oauth_user(
                db_session, provider, user_data, user_info, tokens=tokens
            )
            user = login_result.user

            # Ensure roles are synced
            from skrift.auth import sync_roles_to_database
            await sync_roles_to_database(db_session)

            # Always assign admin role during setup
            admin_role = await db_session.scalar(select(Role).where(Role.name == "admin"))
            if admin_role:
                existing = await db_session.execute(
                    select(user_roles).where(
                        user_roles.c.user_id == user.id,
                        user_roles.c.role_id == admin_role.id
                    )
                )
                if not existing.first():
                    await db_session.execute(
                        user_roles.insert().values(user_id=user.id, role_id=admin_role.id)
                    )

            # Mark setup complete
            timestamp = datetime.now(UTC).isoformat()
            await setting_service.set_setting(db_session, SETUP_COMPLETED_AT_KEY, timestamp)

        # Clear setup flag
        request.session.pop("oauth_setup", None)

        # Set session
        request.session["user_id"] = str(user.id)
        request.session["user_name"] = user.name
        request.session["user_email"] = user.email
        request.session["user_picture_url"] = user.picture_url
        request.session["flash"] = "Admin account created successfully!"
        request.session["setup_just_completed"] = True

        return Redirect(path="/setup/complete")
