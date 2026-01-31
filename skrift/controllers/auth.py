"""Authentication controller for OAuth login flows.

Supports multiple OAuth providers: Google, GitHub, Microsoft, Discord, Facebook, X (Twitter).
Also supports a development-only "dummy" provider for testing.
"""

import base64
import fnmatch
import hashlib
import secrets
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode, urlparse

import httpx
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import Parameter
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.config import get_settings
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.user import User
from skrift.setup.providers import DUMMY_PROVIDER_KEY, OAUTH_PROVIDERS, get_provider_info


def _is_safe_redirect_url(url: str, allowed_domains: list[str]) -> bool:
    """Check if URL is safe to redirect to.

    Supports wildcard patterns using fnmatch-style matching:
    - "*.example.com" matches any subdomain of example.com
    - "app-*.example.com" matches app-foo.example.com, app-bar.example.com, etc.
    - "example.com" (no wildcards) matches example.com and all subdomains
    """
    # Relative paths are always safe (but not protocol-relative //domain.com)
    if url.startswith("/") and not url.startswith("//"):
        return True

    # Parse absolute URL
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # Must have scheme and netloc
    if not parsed.scheme or not parsed.netloc:
        return False

    # Only allow http/https
    if parsed.scheme not in ("http", "https"):
        return False

    # Check if domain matches allowed list
    host = parsed.netloc.lower().split(":")[0]  # Remove port
    for pattern in allowed_domains:
        pattern = pattern.lower()
        # If pattern contains wildcards, use fnmatch
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(host, pattern):
                return True
        else:
            # No wildcards: exact match or subdomain match
            if host == pattern or host.endswith(f".{pattern}"):
                return True

    return False


def _get_safe_redirect_url(request: Request, allowed_domains: list[str], default: str = "/") -> str:
    """Get the next redirect URL from session, validating it's safe."""
    next_url = request.session.pop("auth_next", None)
    if next_url and _is_safe_redirect_url(next_url, allowed_domains):
        return next_url
    return default


def get_auth_url(provider: str, settings, state: str, code_challenge: str | None = None) -> str:
    """Build the OAuth authorization URL for a provider."""
    provider_info = get_provider_info(provider)
    if not provider_info:
        raise ValueError(f"Unknown provider: {provider}")

    provider_config = settings.auth.providers.get(provider)
    if not provider_config:
        raise ValueError(f"Provider {provider} not configured")

    # Build auth URL (handle Microsoft tenant placeholder)
    auth_url = provider_info.auth_url
    if "{tenant}" in auth_url:
        tenant = getattr(provider_config, "tenant_id", None) or "common"
        auth_url = auth_url.replace("{tenant}", tenant)

    params = {
        "client_id": provider_config.client_id,
        "redirect_uri": settings.auth.get_redirect_uri(provider),
        "response_type": "code",
        "scope": " ".join(provider_config.scopes),
        "state": state,
    }

    # Provider-specific parameters
    if provider == "google":
        params["access_type"] = "offline"
        params["prompt"] = "select_account"
    elif provider == "twitter":
        # Twitter requires PKCE
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
    elif provider == "discord":
        params["prompt"] = "consent"

    return f"{auth_url}?{urlencode(params)}"


async def exchange_code_for_token(
    provider: str, settings, code: str, code_verifier: str | None = None
) -> dict:
    """Exchange authorization code for access token."""
    provider_info = get_provider_info(provider)
    if not provider_info:
        raise ValueError(f"Unknown provider: {provider}")

    provider_config = settings.auth.providers.get(provider)
    if not provider_config:
        raise ValueError(f"Provider {provider} not configured")

    # Build token URL (handle Microsoft tenant placeholder)
    token_url = provider_info.token_url
    if "{tenant}" in token_url:
        tenant = getattr(provider_config, "tenant_id", None) or "common"
        token_url = token_url.replace("{tenant}", tenant)

    data = {
        "client_id": provider_config.client_id,
        "client_secret": provider_config.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.auth.get_redirect_uri(provider),
    }

    # Twitter requires PKCE code_verifier
    if provider == "twitter" and code_verifier:
        data["code_verifier"] = code_verifier

    headers = {"Accept": "application/json"}

    # GitHub needs special Accept header
    if provider == "github":
        headers["Accept"] = "application/json"

    # Twitter uses Basic auth for token exchange
    if provider == "twitter":
        credentials = base64.b64encode(
            f"{provider_config.client_id}:{provider_config.client_secret}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"
        del data["client_secret"]

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data, headers=headers)

        if response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to exchange code for tokens: {response.text}",
            )

        return response.json()


async def fetch_user_info(provider: str, access_token: str) -> dict:
    """Fetch user information from the OAuth provider."""
    provider_info = get_provider_info(provider)
    if not provider_info:
        raise ValueError(f"Unknown provider: {provider}")

    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(provider_info.userinfo_url, headers=headers)

        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch user info")

        user_info = response.json()

        # GitHub requires separate email fetch if email is private
        if provider == "github" and not user_info.get("email"):
            email_response = await client.get(
                "https://api.github.com/user/emails", headers=headers
            )
            if email_response.status_code == 200:
                emails = email_response.json()
                primary_email = next(
                    (e["email"] for e in emails if e.get("primary")), None
                )
                if primary_email:
                    user_info["email"] = primary_email

        # Twitter has different structure
        if provider == "twitter":
            data = user_info.get("data", {})
            user_info = {
                "id": data.get("id"),
                "name": data.get("name"),
                "username": data.get("username"),
                "email": None,  # Twitter OAuth 2.0 doesn't provide email by default
            }

        return user_info


def extract_user_data(provider: str, user_info: dict) -> dict:
    """Extract normalized user data from provider-specific response."""
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
            "picture_url": None,  # Microsoft Graph requires separate call for photo
        }
    elif provider == "discord":
        avatar = user_info.get("avatar")
        user_id = user_info.get("id")
        avatar_url = None
        if avatar and user_id:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
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
        return {
            "oauth_id": user_info.get("id"),
            "email": user_info.get("email"),
            "name": user_info.get("name") or user_info.get("username"),
            "picture_url": None,
        }
    else:
        return {
            "oauth_id": str(user_info.get("id", user_info.get("sub"))),
            "email": user_info.get("email"),
            "name": user_info.get("name"),
            "picture_url": user_info.get("picture"),
        }


class AuthController(Controller):
    path = "/auth"

    @get("/{provider:str}/login")
    async def oauth_login(
        self,
        request: Request,
        provider: str,
        next_url: Annotated[str | None, Parameter(query="next")] = None,
    ) -> Redirect | TemplateResponse:
        """Redirect to OAuth provider consent screen, or show dummy login form."""
        settings = get_settings()
        provider_info = get_provider_info(provider)

        # Store next URL in session if provided and valid
        if next_url and _is_safe_redirect_url(next_url, settings.auth.allowed_redirect_domains):
            request.session["auth_next"] = next_url

        if not provider_info:
            raise NotFoundException(f"Unknown provider: {provider}")

        if provider not in settings.auth.providers:
            raise NotFoundException(f"Provider {provider} not configured")

        # Dummy provider shows local login form instead of redirecting to OAuth
        if provider == DUMMY_PROVIDER_KEY:
            flash = request.session.pop("flash", None)
            return TemplateResponse(
                "auth/dummy_login.html",
                context={"flash": flash},
            )

        # Generate CSRF state token
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        request.session["oauth_provider"] = provider

        # Generate PKCE for Twitter
        code_challenge = None
        if provider == "twitter":
            code_verifier = secrets.token_urlsafe(64)[:128]
            request.session["oauth_code_verifier"] = code_verifier
            # S256 challenge
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode().rstrip("=")

        auth_url = get_auth_url(provider, settings, state, code_challenge)
        return Redirect(path=auth_url)

    @get("/{provider:str}/callback")
    async def oauth_callback(
        self,
        request: Request,
        db_session: AsyncSession,
        provider: str,
        code: str | None = None,
        oauth_state: Annotated[str | None, Parameter(query="state")] = None,
        error: str | None = None,
    ) -> Redirect:
        """Handle OAuth callback from provider."""
        settings = get_settings()
        provider_info = get_provider_info(provider)

        if not provider_info:
            raise NotFoundException(f"Unknown provider: {provider}")

        # Check for OAuth errors
        if error:
            request.session["flash"] = f"OAuth error: {error}"
            return Redirect(path="/auth/login")

        # Verify CSRF state
        stored_state = request.session.pop("oauth_state", None)
        if not oauth_state or oauth_state != stored_state:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not code:
            raise HTTPException(status_code=400, detail="Missing authorization code")

        # Get PKCE verifier if present (for Twitter)
        code_verifier = request.session.pop("oauth_code_verifier", None)

        # Exchange code for tokens
        tokens = await exchange_code_for_token(
            provider, settings, code, code_verifier
        )
        access_token = tokens.get("access_token")

        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")

        # Fetch user info
        user_info = await fetch_user_info(provider, access_token)
        user_data = extract_user_data(provider, user_info)

        oauth_id = user_data["oauth_id"]
        if not oauth_id:
            raise HTTPException(status_code=400, detail="Could not determine user ID")

        email = user_data["email"]

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
            # Update provider email if changed
            if email:
                oauth_account.provider_email = email
            # Update provider metadata
            oauth_account.provider_metadata = user_info
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
                    provider_metadata=user_info,
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
                    provider_metadata=user_info,
                    user_id=user.id,
                )
                db_session.add(oauth_account)

        await db_session.commit()

        # Set session with user info
        request.session["user_id"] = str(user.id)
        request.session["user_name"] = user.name
        request.session["user_email"] = user.email
        request.session["user_picture_url"] = user.picture_url
        request.session["flash"] = "Successfully logged in!"

        return Redirect(path=_get_safe_redirect_url(request, settings.auth.allowed_redirect_domains))

    @get("/login")
    async def login_page(
        self,
        request: Request,
        next_url: Annotated[str | None, Parameter(query="next")] = None,
    ) -> TemplateResponse:
        """Show login page with available providers."""
        flash = request.session.pop("flash", None)
        settings = get_settings()

        # Store next URL in session if provided and valid
        if next_url and _is_safe_redirect_url(next_url, settings.auth.allowed_redirect_domains):
            request.session["auth_next"] = next_url

        # Get configured providers (excluding dummy from main list)
        configured_providers = list(settings.auth.providers.keys())
        providers = {
            key: OAUTH_PROVIDERS[key]
            for key in configured_providers
            if key in OAUTH_PROVIDERS and key != DUMMY_PROVIDER_KEY
        }

        # Check if dummy provider is configured
        has_dummy = DUMMY_PROVIDER_KEY in settings.auth.providers

        return TemplateResponse(
            "auth/login.html",
            context={
                "flash": flash,
                "providers": providers,
                "has_dummy": has_dummy,
            },
        )

    @post("/dummy-login")
    async def dummy_login_submit(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Redirect:
        """Process dummy login form submission."""
        settings = get_settings()

        if DUMMY_PROVIDER_KEY not in settings.auth.providers:
            raise NotFoundException("Dummy provider not configured")

        # Parse form data from request
        form_data = await request.form()
        email = form_data.get("email", "").strip()
        name = form_data.get("name", "").strip()

        if not email:
            request.session["flash"] = "Email is required"
            return Redirect(path="/auth/dummy/login")

        # Default name to email username if not provided
        if not name:
            name = email.split("@")[0]

        # Generate deterministic oauth_id from email
        oauth_id = f"dummy_{hashlib.sha256(email.encode()).hexdigest()[:16]}"

        # Create synthetic metadata for dummy provider
        dummy_metadata = {
            "id": oauth_id,
            "email": email,
            "name": name,
        }

        # Step 1: Check if OAuth account already exists
        result = await db_session.execute(
            select(OAuthAccount)
            .options(selectinload(OAuthAccount.user))
            .where(
                OAuthAccount.provider == DUMMY_PROVIDER_KEY,
                OAuthAccount.provider_account_id == oauth_id,
            )
        )
        oauth_account = result.scalar_one_or_none()

        if oauth_account:
            # Existing OAuth account - update user profile
            user = oauth_account.user
            user.name = name
            user.email = email
            user.last_login_at = datetime.now(UTC)
            oauth_account.provider_email = email
            oauth_account.provider_metadata = dummy_metadata
        else:
            # Step 2: Check if a user with this email already exists
            result = await db_session.execute(
                select(User).where(User.email == email)
            )
            user = result.scalar_one_or_none()

            if user:
                # Link new OAuth account to existing user
                oauth_account = OAuthAccount(
                    provider=DUMMY_PROVIDER_KEY,
                    provider_account_id=oauth_id,
                    provider_email=email,
                    provider_metadata=dummy_metadata,
                    user_id=user.id,
                )
                db_session.add(oauth_account)
                # Update user profile
                user.name = name
                user.last_login_at = datetime.now(UTC)
            else:
                # Step 3: Create new user + OAuth account
                user = User(
                    email=email,
                    name=name,
                    last_login_at=datetime.now(UTC),
                )
                db_session.add(user)
                await db_session.flush()

                oauth_account = OAuthAccount(
                    provider=DUMMY_PROVIDER_KEY,
                    provider_account_id=oauth_id,
                    provider_email=email,
                    provider_metadata=dummy_metadata,
                    user_id=user.id,
                )
                db_session.add(oauth_account)

        await db_session.commit()

        # Set session with user info
        request.session["user_id"] = str(user.id)
        request.session["user_name"] = user.name
        request.session["user_email"] = user.email
        request.session["user_picture_url"] = user.picture_url
        request.session["flash"] = "Successfully logged in!"

        return Redirect(path=_get_safe_redirect_url(request, settings.auth.allowed_redirect_domains))

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        """Clear session and redirect to home."""
        request.session.clear()
        return Redirect(path="/")
