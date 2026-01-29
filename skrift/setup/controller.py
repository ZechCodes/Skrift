"""Setup wizard controller for first-time Skrift configuration."""

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from litestar.response import Redirect, Template as TemplateResponse
from litestar.response.sse import ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import selectinload

from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.role import Role, user_roles
from skrift.db.models.user import User
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
from skrift.setup.providers import get_all_providers, get_provider_info
from skrift.setup.state import (
    can_connect_to_database,
    get_database_url_from_yaml,
    get_first_incomplete_step,
    is_auth_configured,
    is_site_configured,
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

    engine = create_async_engine(db_url)
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
        """Redirect to the first incomplete setup step.

        Uses smart detection to skip already-configured steps, allowing users
        to resume setup without re-entering existing configuration.
        """
        # Check if database is configured and connectable
        can_connect, _ = await can_connect_to_database()
        if can_connect:
            # Database is configured - go through configuring page to run migrations
            return Redirect(path="/setup/configuring")

        # Database not configured - go to database step
        request.session["setup_wizard_step"] = "database"
        return Redirect(path="/setup/database")

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
                "total_steps": 4,
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
                request.session["setup_error"] = f"Connection failed: {error}"
                return Redirect(path="/setup/database")

            # Connection successful - redirect to configuring page to run migrations
            request.session["setup_wizard_step"] = "configuring"
            return Redirect(path="/setup/configuring")

        except Exception as e:
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
                "total_steps": 4,
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
            if is_auth_configured():
                if await is_site_configured():
                    next_step = "admin"
                else:
                    next_step = "site"
            else:
                next_step = "auth"

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
                "total_steps": 4,
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
                "total_steps": 4,
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

    @get("/admin")
    async def admin_step(self, request: Request) -> TemplateResponse:
        """Step 4: Create admin account."""
        flash = request.session.pop("flash", None)
        error = request.session.pop("setup_error", None)

        # Get configured providers
        config = load_config()
        auth_config = config.get("auth", {})
        configured_providers = list(auth_config.get("providers", {}).keys())

        # Get provider display info
        all_providers = get_all_providers()
        provider_info = {
            key: all_providers[key] for key in configured_providers if key in all_providers
        }

        return TemplateResponse(
            "setup/admin.html",
            context={
                "flash": flash,
                "error": error,
                "step": 4,
                "total_steps": 4,
                "providers": provider_info,
                "configured_providers": configured_providers,
            },
        )

    @get("/oauth/{provider:str}/login")
    async def setup_oauth_login(self, request: Request, provider: str) -> Redirect:
        """Redirect to OAuth provider for setup admin creation."""
        config = load_config()
        auth_config = config.get("auth", {})
        providers_config = auth_config.get("providers", {})

        if provider not in providers_config:
            raise HTTPException(status_code=404, detail=f"Provider {provider} not configured")

        provider_info = get_provider_info(provider)
        if not provider_info:
            raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

        provider_config = providers_config[provider]

        # Resolve env var references in config
        client_id = self._resolve_env_var(provider_config.get("client_id", ""))

        # Generate CSRF state token
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        request.session["oauth_provider"] = provider
        request.session["oauth_setup"] = True

        # Build redirect URI - use the standard /auth callback URL
        # This matches what's configured in the OAuth provider console
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        redirect_uri = f"{scheme}://{host}/auth/{provider}/callback"

        # Get scopes
        scopes = provider_config.get("scopes", provider_info.scopes)

        # Generate PKCE for Twitter
        code_challenge = None
        if provider == "twitter":
            code_verifier = secrets.token_urlsafe(64)[:128]
            request.session["oauth_code_verifier"] = code_verifier
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode().rstrip("=")

        # Build auth URL
        auth_url = provider_info.auth_url
        if "{tenant}" in auth_url:
            tenant = provider_config.get("tenant_id", "common")
            if isinstance(tenant, str) and tenant.startswith("$"):
                tenant = self._resolve_env_var(tenant) or "common"
            auth_url = auth_url.replace("{tenant}", tenant)

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }

        if provider == "google":
            params["access_type"] = "offline"
            params["prompt"] = "select_account"
        elif provider == "twitter" and code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        elif provider == "discord":
            params["prompt"] = "consent"

        return Redirect(path=f"{auth_url}?{urlencode(params)}")

    def _resolve_env_var(self, value: str) -> str:
        """Resolve environment variable reference if value starts with $."""
        import os
        if value.startswith("$"):
            return os.environ.get(value[1:], "")
        return value

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
                "step": 4,
                "total_steps": 4,
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
        # Check if this is a setup flow
        if not request.session.get("oauth_setup"):
            # Not a setup flow, return error
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

        provider_info = get_provider_info(provider)
        provider_config = providers_config[provider]

        # Resolve env vars
        client_id = self._resolve_env_var(provider_config.get("client_id", ""))
        client_secret = self._resolve_env_var(provider_config.get("client_secret", ""))

        # Build redirect URI
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        redirect_uri = f"{scheme}://{host}/auth/{provider}/callback"

        # Get PKCE verifier if present
        code_verifier = request.session.pop("oauth_code_verifier", None)

        # Exchange code for token
        token_url = provider_info.token_url
        if "{tenant}" in token_url:
            tenant = provider_config.get("tenant_id", "common")
            if isinstance(tenant, str) and tenant.startswith("$"):
                tenant = self._resolve_env_var(tenant) or "common"
            token_url = token_url.replace("{tenant}", tenant)

        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }

        if provider == "twitter" and code_verifier:
            data["code_verifier"] = code_verifier

        headers = {"Accept": "application/json"}
        if provider == "github":
            headers["Accept"] = "application/json"
        if provider == "twitter":
            credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"
            del data["client_secret"]

        async with httpx.AsyncClient() as client:
            response = await client.post(token_url, data=data, headers=headers)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Token exchange failed: {response.text}")
            tokens = response.json()

        access_token = tokens.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

        # Fetch user info
        async with httpx.AsyncClient() as client:
            user_headers = {"Authorization": f"Bearer {access_token}"}
            response = await client.get(provider_info.userinfo_url, headers=user_headers)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")
            user_info = response.json()

            # GitHub email handling
            if provider == "github" and not user_info.get("email"):
                email_response = await client.get("https://api.github.com/user/emails", headers=user_headers)
                if email_response.status_code == 200:
                    emails = email_response.json()
                    primary_email = next((e["email"] for e in emails if e.get("primary")), None)
                    if primary_email:
                        user_info["email"] = primary_email

        # Extract user data based on provider
        user_data = self._extract_user_data(provider, user_info)
        oauth_id = user_data["oauth_id"]
        if not oauth_id:
            raise HTTPException(status_code=400, detail="Could not determine user ID")

        email = user_data["email"]

        # Create user and mark setup complete
        async with get_setup_db_session() as db_session:
            # Step 1: Check if OAuth account already exists
            result = await db_session.execute(
                select(OAuthAccount)
                .options(selectinload(OAuthAccount.user))
                .where(OAuthAccount.provider == provider, OAuthAccount.provider_account_id == oauth_id)
            )
            oauth_account = result.scalar_one_or_none()

            if oauth_account:
                # Existing OAuth account - update user profile
                user = oauth_account.user
                user.name = user_data["name"]
                if user_data["picture_url"]:
                    user.picture_url = user_data["picture_url"]
                user.last_login_at = datetime.now(UTC)
                if email:
                    oauth_account.provider_email = email
            else:
                # Step 2: Check if a user with this email already exists
                user = None
                if email:
                    result = await db_session.execute(
                        select(User).where(User.email == email)
                    )
                    user = result.scalar_one_or_none()

                if user:
                    # Link new OAuth account to existing user
                    oauth_account = OAuthAccount(
                        provider=provider,
                        provider_account_id=oauth_id,
                        provider_email=email,
                        user_id=user.id,
                    )
                    db_session.add(oauth_account)
                    # Update user profile
                    user.name = user_data["name"]
                    if user_data["picture_url"]:
                        user.picture_url = user_data["picture_url"]
                    user.last_login_at = datetime.now(UTC)
                else:
                    # Step 3: Create new user + OAuth account
                    user = User(
                        email=email,
                        name=user_data["name"],
                        picture_url=user_data["picture_url"],
                        last_login_at=datetime.now(UTC),
                    )
                    db_session.add(user)
                    await db_session.flush()

                    oauth_account = OAuthAccount(
                        provider=provider,
                        provider_account_id=oauth_id,
                        provider_email=email,
                        user_id=user.id,
                    )
                    db_session.add(oauth_account)

            # Ensure roles are synced (they may not exist if DB was created after server start)
            from skrift.auth import sync_roles_to_database
            await sync_roles_to_database(db_session)

            # Always assign admin role during setup (whether user is new or existing)
            admin_role = await db_session.scalar(select(Role).where(Role.name == "admin"))
            if admin_role:
                # Check if user already has admin role
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

        # Note: Don't call mark_setup_complete_in_dispatcher() here.
        # The switch happens in /setup/complete after rendering the page.

        return Redirect(path="/setup/complete")

    def _resolve_env_var(self, value: str) -> str:
        """Resolve environment variable reference if value starts with $."""
        import os
        if value.startswith("$"):
            return os.environ.get(value[1:], "")
        return value

    def _extract_user_data(self, provider: str, user_info: dict) -> dict:
        """Extract normalized user data from provider response."""
        if provider == "google":
            return {
                "oauth_id": user_info.get("id"),
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture_url": user_info.get("picture"),
            }
        elif provider == "github":
            return {
                "oauth_id": str(user_info.get("id")),
                "email": user_info.get("email"),
                "name": user_info.get("name") or user_info.get("login"),
                "picture_url": user_info.get("avatar_url"),
            }
        elif provider == "microsoft":
            return {
                "oauth_id": user_info.get("id"),
                "email": user_info.get("mail") or user_info.get("userPrincipalName"),
                "name": user_info.get("displayName"),
                "picture_url": None,
            }
        elif provider == "discord":
            avatar = user_info.get("avatar")
            user_id = user_info.get("id")
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png" if avatar and user_id else None
            return {
                "oauth_id": user_id,
                "email": user_info.get("email"),
                "name": user_info.get("global_name") or user_info.get("username"),
                "picture_url": avatar_url,
            }
        elif provider == "facebook":
            picture = user_info.get("picture", {}).get("data", {})
            return {
                "oauth_id": user_info.get("id"),
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture_url": picture.get("url") if not picture.get("is_silhouette") else None,
            }
        elif provider == "twitter":
            data = user_info.get("data", user_info)
            return {
                "oauth_id": data.get("id"),
                "email": data.get("email"),
                "name": data.get("name") or data.get("username"),
                "picture_url": None,
            }
        else:
            return {
                "oauth_id": str(user_info.get("id", user_info.get("sub"))),
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture_url": user_info.get("picture"),
            }
