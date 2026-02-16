"""Authentication controller for OAuth login flows.

Supports multiple OAuth providers: Google, GitHub, Microsoft, Discord, Facebook, X (Twitter).
Also supports a development-only "dummy" provider for testing.
"""

import base64
import fnmatch
import hashlib
import secrets
from typing import Annotated
from urllib.parse import urlencode, urlparse

import httpx
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import Parameter
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.oauth_account_service import find_or_create_oauth_user
from skrift.auth.providers import NormalizedUserData, get_oauth_provider
from skrift.config import get_settings
from skrift.db.models.user import User
from skrift.forms import verify_csrf
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


async def _exchange_and_fetch(
    provider_key: str,
    settings,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    tenant: str | None = None,
) -> tuple[NormalizedUserData, dict, dict]:
    """Exchange authorization code for token, fetch user info, and extract normalized data.

    Args:
        provider_key: The OAuth provider identifier.
        settings: App settings (used to look up provider config if client_id/client_secret not given).
        code: The authorization code from the callback.
        redirect_uri: The redirect URI used in the auth request.
        code_verifier: PKCE code verifier (for Twitter).
        client_id: Override client_id (used during setup when settings aren't available).
        client_secret: Override client_secret (used during setup).
        tenant: Override tenant ID (used during setup for Microsoft).

    Returns:
        Tuple of (NormalizedUserData, raw_user_info_dict, tokens_dict).
    """
    provider = get_oauth_provider(provider_key)

    # Resolve credentials
    if client_id is None or client_secret is None:
        provider_config = settings.auth.providers.get(provider_key)
        if not provider_config:
            raise ValueError(f"Provider {provider_key} not configured")
        client_id = provider_config.client_id
        client_secret = provider_config.client_secret
        tenant = getattr(provider_config, "tenant_id", None)

    # Build token request
    token_url = provider.resolve_url(provider.provider_info.token_url, tenant)
    token_data = provider.build_token_data(client_id, client_secret, code, redirect_uri, code_verifier)
    token_headers = provider.build_token_headers(client_id, client_secret)

    # Twitter uses Basic auth — remove client_secret from POST data
    if provider_key == "twitter":
        token_data.pop("client_secret", None)

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=token_data, headers=token_headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to exchange code for tokens: {response.text}",
            )
        tokens = response.json()

    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access token received")

    # Fetch and normalize user info
    user_info = await provider.fetch_user_info(access_token)
    user_data = provider.extract_user_data(user_info)

    if not user_data.oauth_id:
        raise HTTPException(status_code=400, detail="Could not determine user ID")

    return user_data, user_info, tokens


def _set_login_session(request: Request, user: "User") -> None:
    """Rotate the session and populate it with user data.

    Preserves flash/flash_messages across the rotation so login
    success messages aren't lost.
    """
    # Preserve flash state and notification ID
    flash = request.session.get("flash")
    flash_messages = request.session.get("flash_messages")
    nid = request.session.get("_nid")

    # Clear session to rotate the cookie
    request.session.clear()

    # Repopulate with user data
    request.session["user_id"] = str(user.id)
    request.session["user_name"] = user.name
    request.session["user_email"] = user.email
    request.session["user_picture_url"] = user.picture_url

    # Restore flash state
    if flash is not None:
        request.session["flash"] = flash
    if flash_messages is not None:
        request.session["flash_messages"] = flash_messages
    if nid is not None:
        request.session["_nid"] = nid


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

        # Get the provider strategy for PKCE + auth params
        oauth_provider = get_oauth_provider(provider)

        # Generate PKCE for providers that require it (Twitter)
        code_challenge = None
        if oauth_provider.requires_pkce:
            code_verifier = secrets.token_urlsafe(64)[:128]
            request.session["oauth_code_verifier"] = code_verifier
            code_challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode()).digest()
            ).decode().rstrip("=")

        # Build auth URL
        provider_config = settings.auth.providers[provider]
        tenant = getattr(provider_config, "tenant_id", None)
        auth_url = oauth_provider.resolve_url(provider_info.auth_url, tenant)
        params = oauth_provider.build_auth_params(
            client_id=provider_config.client_id,
            redirect_uri=settings.auth.get_redirect_uri(provider),
            scopes=provider_config.scopes,
            state=state,
            code_challenge=code_challenge,
        )

        return Redirect(path=f"{auth_url}?{urlencode(params)}")

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

        if not get_provider_info(provider):
            raise NotFoundException(f"Unknown provider: {provider}")

        if error:
            request.session["flash"] = f"OAuth error: {error}"
            return Redirect(path="/auth/login")

        # Verify CSRF state
        stored_state = request.session.pop("oauth_state", None)
        if not oauth_state or oauth_state != stored_state:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not code:
            raise HTTPException(status_code=400, detail="Missing authorization code")

        code_verifier = request.session.pop("oauth_code_verifier", None)

        user_data, user_info, tokens = await _exchange_and_fetch(
            provider, settings, code,
            settings.auth.get_redirect_uri(provider),
            code_verifier,
        )

        login_result = await find_or_create_oauth_user(
            db_session, provider, user_data, user_info, tokens=tokens
        )
        await db_session.commit()

        request.session["flash"] = "Successfully logged in!"
        _set_login_session(request, login_result.user)

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

        if not await verify_csrf(request):
            request.session["flash"] = "Invalid request. Please try again."
            return Redirect(path="/auth/dummy/login")

        form_data = await request.form()
        email = form_data.get("email", "").strip()
        name = form_data.get("name", "").strip()

        if not email:
            request.session["flash"] = "Email is required"
            return Redirect(path="/auth/dummy/login")

        if not name:
            name = email.split("@")[0]

        oauth_id = f"dummy_{hashlib.sha256(email.encode()).hexdigest()[:16]}"
        dummy_metadata = {"id": oauth_id, "email": email, "name": name}

        user_data = NormalizedUserData(
            oauth_id=oauth_id, email=email, name=name, picture_url=None
        )

        login_result = await find_or_create_oauth_user(
            db_session, DUMMY_PROVIDER_KEY, user_data, dummy_metadata
        )
        await db_session.commit()

        request.session["flash"] = "Successfully logged in!"
        _set_login_session(request, login_result.user)

        return Redirect(path=_get_safe_redirect_url(request, settings.auth.allowed_redirect_domains))

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        """Clear session and redirect to home."""
        request.session.clear()
        return Redirect(path="/")
