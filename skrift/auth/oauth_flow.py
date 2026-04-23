"""Reusable OAuth flow helpers."""

from __future__ import annotations

import httpx
from litestar.exceptions import HTTPException

from skrift.auth.identities import ResolvedPrimaryIdentity, identity_from_oauth_user_data
from skrift.auth.providers import get_oauth_provider


async def exchange_and_fetch_oauth_identity(
    provider_key: str,
    settings,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    tenant: str | None = None,
    provider_type: str | None = None,
) -> tuple[ResolvedPrimaryIdentity, dict, dict]:
    """Exchange an OAuth code and return a generic primary-auth identity."""
    provider = get_oauth_provider(provider_key, provider_type=provider_type)

    if client_id is None or client_secret is None:
        provider_config = settings.auth.providers.get(provider_key)
        if not provider_config:
            raise ValueError(f"Provider {provider_key} not configured")
        client_id = provider_config.client_id
        client_secret = provider_config.client_secret
        tenant = getattr(provider_config, "tenant_id", None)

    token_url = provider.resolve_url(provider.provider_info.token_url, tenant)
    token_data = provider.build_token_data(client_id, client_secret, code, redirect_uri, code_verifier)
    token_headers = provider.build_token_headers(client_id, client_secret)

    from skrift.lib.observability import span

    with span("oauth.exchange:{provider_key}", provider_key=provider_key):
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

        user_info = await provider.fetch_user_info(access_token)
        user_data = provider.extract_user_data(user_info)

    if not user_data.oauth_id:
        raise HTTPException(status_code=400, detail="Could not determine user ID")

    identity = identity_from_oauth_user_data(
        method_key=provider_key,
        method_type="oauth",
        user_data=user_data,
        raw_metadata=user_info,
    )
    return identity, user_info, tokens
