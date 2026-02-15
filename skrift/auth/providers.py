"""OAuth provider strategy classes for normalizing provider-specific behavior."""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from litestar.exceptions import HTTPException

from skrift.setup.providers import OAuthProviderInfo, get_provider_info


@dataclass
class NormalizedUserData:
    """Provider-agnostic user data extracted from OAuth responses."""

    oauth_id: str | None
    email: str | None
    name: str | None
    picture_url: str | None


class OAuthProvider(ABC):
    """Base class for OAuth provider strategies."""

    def __init__(self, provider_key: str, provider_info: OAuthProviderInfo):
        self.provider_key = provider_key
        self.provider_info = provider_info

    @property
    def requires_pkce(self) -> bool:
        return False

    def resolve_url(self, url: str, tenant: str | None = None) -> str:
        """Resolve {tenant} placeholder in URLs."""
        if "{tenant}" in url:
            url = url.replace("{tenant}", tenant or "common")
        return url

    def build_auth_params(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: list[str],
        state: str,
        code_challenge: str | None = None,
    ) -> dict:
        """Build the authorization URL query parameters."""
        return {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }

    def build_token_data(
        self,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> dict:
        """Build token exchange POST data."""
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }

    def build_token_headers(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> dict:
        """Build headers for the token exchange request."""
        return {"Accept": "application/json"}

    async def fetch_user_info(self, access_token: str) -> dict:
        """Fetch user info from the provider's userinfo endpoint."""
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient() as client:
            response = await client.get(self.provider_info.userinfo_url, headers=headers)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")
            return response.json()

    @abstractmethod
    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        """Extract normalized user data from provider-specific response."""
        ...


class GoogleProvider(OAuthProvider):
    def build_auth_params(self, client_id, redirect_uri, scopes, state, code_challenge=None):
        params = super().build_auth_params(client_id, redirect_uri, scopes, state, code_challenge)
        params["access_type"] = "offline"
        params["prompt"] = "select_account"
        return params

    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        return NormalizedUserData(
            oauth_id=user_info.get("id"),
            email=user_info.get("email"),
            name=user_info.get("name"),
            picture_url=user_info.get("picture"),
        )


class GitHubProvider(OAuthProvider):
    async def fetch_user_info(self, access_token: str) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient() as client:
            response = await client.get(self.provider_info.userinfo_url, headers=headers)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")
            user_info = response.json()

            if not user_info.get("email"):
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

            return user_info

    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        return NormalizedUserData(
            oauth_id=str(user_info.get("id")),
            email=user_info.get("email"),
            name=user_info.get("name") or user_info.get("login"),
            picture_url=user_info.get("avatar_url"),
        )


class MicrosoftProvider(OAuthProvider):
    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        return NormalizedUserData(
            oauth_id=user_info.get("id"),
            email=user_info.get("mail") or user_info.get("userPrincipalName"),
            name=user_info.get("displayName"),
            picture_url=None,
        )


class DiscordProvider(OAuthProvider):
    def build_auth_params(self, client_id, redirect_uri, scopes, state, code_challenge=None):
        params = super().build_auth_params(client_id, redirect_uri, scopes, state, code_challenge)
        params["prompt"] = "consent"
        return params

    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        avatar = user_info.get("avatar")
        user_id = user_info.get("id")
        avatar_url = None
        if avatar and user_id:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
        return NormalizedUserData(
            oauth_id=user_id,
            email=user_info.get("email"),
            name=user_info.get("global_name") or user_info.get("username"),
            picture_url=avatar_url,
        )


class FacebookProvider(OAuthProvider):
    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        picture = user_info.get("picture", {}).get("data", {})
        return NormalizedUserData(
            oauth_id=user_info.get("id"),
            email=user_info.get("email"),
            name=user_info.get("name"),
            picture_url=picture.get("url") if not picture.get("is_silhouette") else None,
        )


class TwitterProvider(OAuthProvider):
    @property
    def requires_pkce(self) -> bool:
        return True

    def build_auth_params(self, client_id, redirect_uri, scopes, state, code_challenge=None):
        params = super().build_auth_params(client_id, redirect_uri, scopes, state, code_challenge)
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return params

    def build_token_data(self, client_id, client_secret, code, redirect_uri, code_verifier=None):
        data = super().build_token_data(client_id, client_secret, code, redirect_uri, code_verifier)
        if code_verifier:
            data["code_verifier"] = code_verifier
        return data

    def build_token_headers(self, client_id=None, client_secret=None):
        headers = super().build_token_headers()
        if client_id and client_secret:
            credentials = base64.b64encode(
                f"{client_id}:{client_secret}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {credentials}"
        return headers

    async def fetch_user_info(self, access_token: str) -> dict:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient() as client:
            response = await client.get(self.provider_info.userinfo_url, headers=headers)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")
            user_info = response.json()

            data = user_info.get("data", {})
            return {
                "id": data.get("id"),
                "name": data.get("name"),
                "username": data.get("username"),
                "email": None,
            }

    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        return NormalizedUserData(
            oauth_id=user_info.get("id"),
            email=user_info.get("email"),
            name=user_info.get("name") or user_info.get("username"),
            picture_url=None,
        )


class GenericProvider(OAuthProvider):
    """Fallback provider for unknown/custom OAuth providers."""

    def extract_user_data(self, user_info: dict) -> NormalizedUserData:
        return NormalizedUserData(
            oauth_id=str(user_info.get("id", user_info.get("sub"))),
            email=user_info.get("email"),
            name=user_info.get("name"),
            picture_url=user_info.get("picture"),
        )


_PROVIDER_CLASSES: dict[str, type[OAuthProvider]] = {
    "google": GoogleProvider,
    "github": GitHubProvider,
    "microsoft": MicrosoftProvider,
    "discord": DiscordProvider,
    "facebook": FacebookProvider,
    "twitter": TwitterProvider,
}


def get_oauth_provider(provider_key: str) -> OAuthProvider:
    """Get an OAuth provider strategy instance by key.

    Returns a GenericProvider for unknown provider keys.
    """
    provider_info = get_provider_info(provider_key)
    if not provider_info:
        raise ValueError(f"Unknown provider: {provider_key}")

    cls = _PROVIDER_CLASSES.get(provider_key, GenericProvider)
    return cls(provider_key, provider_info)
