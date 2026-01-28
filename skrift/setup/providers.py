"""OAuth provider definitions and configuration for the setup wizard."""

from dataclasses import dataclass

DUMMY_PROVIDER_KEY = "dummy"


@dataclass
class OAuthProviderInfo:
    """Information about an OAuth provider."""

    name: str
    auth_url: str
    token_url: str
    userinfo_url: str
    scopes: list[str]
    console_url: str
    fields: list[dict]
    instructions: str
    icon: str = ""


OAUTH_PROVIDERS = {
    "google": OAuthProviderInfo(
        name="Google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://www.googleapis.com/oauth2/v2/userinfo",
        scopes=["openid", "email", "profile"],
        console_url="https://console.cloud.google.com/apis/credentials",
        fields=[
            {"key": "client_id", "label": "Client ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "password"},
        ],
        instructions="""
1. Go to the Google Cloud Console
2. Create a new project or select an existing one
3. Enable the Google+ API
4. Go to Credentials → Create Credentials → OAuth Client ID
5. Choose "Web application"
6. Add the redirect URI shown below
7. Copy the Client ID and Client Secret
        """.strip(),
        icon="google",
    ),
    "github": OAuthProviderInfo(
        name="GitHub",
        auth_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scopes=["read:user", "user:email"],
        console_url="https://github.com/settings/developers",
        fields=[
            {"key": "client_id", "label": "Client ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "password"},
        ],
        instructions="""
1. Go to GitHub Settings → Developer settings → OAuth Apps
2. Click "New OAuth App"
3. Fill in the application details
4. Add the redirect URI shown below as the callback URL
5. Copy the Client ID and generate a Client Secret
        """.strip(),
        icon="github",
    ),
    "microsoft": OAuthProviderInfo(
        name="Microsoft",
        auth_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        userinfo_url="https://graph.microsoft.com/v1.0/me",
        scopes=["openid", "email", "profile"],
        console_url="https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        fields=[
            {"key": "client_id", "label": "Client (Application) ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "password"},
            {
                "key": "tenant_id",
                "label": "Tenant ID",
                "type": "text",
                "placeholder": "common",
                "optional": True,
            },
        ],
        instructions="""
1. Go to Azure Portal → App registrations
2. Click "New registration"
3. Enter a name and select the account types you want to support
4. Add the redirect URI shown below (Web platform)
5. Under Certificates & secrets, create a new client secret
6. Copy the Application (client) ID and secret value
7. For Tenant ID: use "common" for any Microsoft account, or your specific tenant ID
        """.strip(),
        icon="microsoft",
    ),
    "discord": OAuthProviderInfo(
        name="Discord",
        auth_url="https://discord.com/api/oauth2/authorize",
        token_url="https://discord.com/api/oauth2/token",
        userinfo_url="https://discord.com/api/users/@me",
        scopes=["identify", "email"],
        console_url="https://discord.com/developers/applications",
        fields=[
            {"key": "client_id", "label": "Client ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "password"},
        ],
        instructions="""
1. Go to Discord Developer Portal
2. Create a new application
3. Go to OAuth2 → General
4. Add the redirect URI shown below
5. Copy the Client ID and Client Secret
        """.strip(),
        icon="discord",
    ),
    "facebook": OAuthProviderInfo(
        name="Facebook",
        auth_url="https://www.facebook.com/v18.0/dialog/oauth",
        token_url="https://graph.facebook.com/v18.0/oauth/access_token",
        userinfo_url="https://graph.facebook.com/me?fields=id,name,email,picture",
        scopes=["email", "public_profile"],
        console_url="https://developers.facebook.com/apps/",
        fields=[
            {"key": "client_id", "label": "App ID", "type": "text"},
            {"key": "client_secret", "label": "App Secret", "type": "password"},
        ],
        instructions="""
1. Go to Meta for Developers
2. Create a new app (Consumer type)
3. Add Facebook Login product
4. Go to Settings → Basic to find App ID and Secret
5. Add the redirect URI shown below in Facebook Login settings
        """.strip(),
        icon="facebook",
    ),
    "twitter": OAuthProviderInfo(
        name="X (Twitter)",
        auth_url="https://twitter.com/i/oauth2/authorize",
        token_url="https://api.twitter.com/2/oauth2/token",
        userinfo_url="https://api.twitter.com/2/users/me",
        scopes=["users.read", "tweet.read"],
        console_url="https://developer.twitter.com/en/portal/dashboard",
        fields=[
            {"key": "client_id", "label": "Client ID", "type": "text"},
            {"key": "client_secret", "label": "Client Secret", "type": "password"},
        ],
        instructions="""
1. Go to X Developer Portal
2. Create a new project and app
3. Enable OAuth 2.0 in User authentication settings
4. Add the redirect URI shown below
5. Copy the Client ID and Client Secret (OAuth 2.0)
        """.strip(),
        icon="twitter",
    ),
    DUMMY_PROVIDER_KEY: OAuthProviderInfo(
        name="Dummy (Development Only)",
        auth_url="",
        token_url="",
        userinfo_url="",
        scopes=[],
        console_url="",
        fields=[],
        instructions="Development-only provider. DO NOT use in production.",
        icon="dummy",
    ),
}


def get_provider_info(provider: str) -> OAuthProviderInfo | None:
    """Get provider info by key."""
    return OAUTH_PROVIDERS.get(provider)


def get_all_providers() -> dict[str, OAuthProviderInfo]:
    """Get all available OAuth providers (excluding dev-only providers)."""
    return {k: v for k, v in OAUTH_PROVIDERS.items() if k != DUMMY_PROVIDER_KEY}


def validate_no_dummy_auth_in_production() -> None:
    """Exit process if dummy auth is configured in production."""
    import os
    import signal
    import sys

    from skrift.config import get_environment, load_raw_app_config

    if get_environment() != "production":
        return

    config = load_raw_app_config()
    if config is None:
        return

    providers = config.get("auth", {}).get("providers", {})
    if DUMMY_PROVIDER_KEY in providers:
        # Only print if we haven't already (use env var as cross-process flag)
        if not os.environ.get("_SKRIFT_DUMMY_ERROR_PRINTED"):
            os.environ["_SKRIFT_DUMMY_ERROR_PRINTED"] = "1"
            sys.stderr.write(
                "\n"
                "======================================================================\n"
                "SECURITY ERROR: Dummy auth provider is configured in production.\n"
                "Remove 'dummy' from auth.providers in app.yaml.\n"
                "Server will NOT start.\n"
                "======================================================================\n\n"
            )
            sys.stderr.flush()

        # Kill parent process (uvicorn reloader) to stop respawning
        try:
            os.kill(os.getppid(), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        os._exit(1)
