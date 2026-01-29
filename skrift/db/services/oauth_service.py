"""OAuth service for accessing OAuth account data and provider metadata."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.oauth_account import OAuthAccount


async def get_oauth_account_by_user_and_provider(
    db_session: AsyncSession,
    user_id: UUID,
    provider: str,
) -> OAuthAccount | None:
    """Get a specific OAuth account for a user and provider.

    Args:
        db_session: Database session
        user_id: User UUID
        provider: OAuth provider name (e.g., 'discord', 'github')

    Returns:
        OAuthAccount or None if not found
    """
    result = await db_session.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user_id,
            OAuthAccount.provider == provider,
        )
    )
    return result.scalar_one_or_none()


async def get_oauth_accounts_by_user(
    db_session: AsyncSession,
    user_id: UUID,
) -> list[OAuthAccount]:
    """Get all OAuth accounts linked to a user.

    Args:
        db_session: Database session
        user_id: User UUID

    Returns:
        List of OAuthAccount objects
    """
    result = await db_session.execute(
        select(OAuthAccount).where(OAuthAccount.user_id == user_id)
    )
    return list(result.scalars().all())


async def get_provider_metadata(
    db_session: AsyncSession,
    user_id: UUID,
    provider: str,
) -> dict[str, Any] | None:
    """Get the raw provider metadata for a user's OAuth account.

    Args:
        db_session: Database session
        user_id: User UUID
        provider: OAuth provider name

    Returns:
        Provider metadata dict or None if not found
    """
    oauth_account = await get_oauth_account_by_user_and_provider(
        db_session, user_id, provider
    )
    if oauth_account:
        return oauth_account.provider_metadata
    return None


def extract_metadata_field(
    metadata: dict[str, Any] | None,
    *keys: str,
    default: Any = None,
) -> Any:
    """Safely extract a nested field from metadata.

    Args:
        metadata: Provider metadata dict
        *keys: Sequence of keys for nested access (e.g., 'picture', 'data', 'url')
        default: Default value if field not found

    Returns:
        Field value or default
    """
    if metadata is None:
        return default

    current = metadata
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


async def get_provider_username(
    db_session: AsyncSession,
    user_id: UUID,
    provider: str,
) -> str | None:
    """Get the username from a provider's metadata.

    Provider-specific username fields:
    - Discord: username
    - GitHub: login
    - Twitter: username
    - Google: email (no username concept)
    - Microsoft: userPrincipalName
    - Facebook: name (no username concept)

    Args:
        db_session: Database session
        user_id: User UUID
        provider: OAuth provider name

    Returns:
        Username string or None
    """
    metadata = await get_provider_metadata(db_session, user_id, provider)
    if metadata is None:
        return None

    # Provider-specific username extraction
    username_fields = {
        "discord": "username",
        "github": "login",
        "twitter": "username",
        "microsoft": "userPrincipalName",
    }

    field = username_fields.get(provider)
    if field:
        return extract_metadata_field(metadata, field)

    # Fallback for providers without usernames
    return extract_metadata_field(metadata, "email") or extract_metadata_field(
        metadata, "name"
    )


async def get_provider_avatar_url(
    db_session: AsyncSession,
    user_id: UUID,
    provider: str,
) -> str | None:
    """Get the avatar URL from a provider's metadata.

    Provider-specific avatar URL construction:
    - Discord: Constructed from id + avatar hash
    - GitHub: avatar_url
    - Google: picture
    - Microsoft: No direct URL (requires Graph API call)
    - Facebook: picture.data.url
    - Twitter: No avatar in basic userinfo

    Args:
        db_session: Database session
        user_id: User UUID
        provider: OAuth provider name

    Returns:
        Avatar URL string or None
    """
    metadata = await get_provider_metadata(db_session, user_id, provider)
    if metadata is None:
        return None

    if provider == "discord":
        # Discord avatar URL must be constructed
        user_id_discord = extract_metadata_field(metadata, "id")
        avatar_hash = extract_metadata_field(metadata, "avatar")
        if user_id_discord and avatar_hash:
            return f"https://cdn.discordapp.com/avatars/{user_id_discord}/{avatar_hash}.png"
        return None

    if provider == "github":
        return extract_metadata_field(metadata, "avatar_url")

    if provider == "google":
        return extract_metadata_field(metadata, "picture")

    if provider == "facebook":
        return extract_metadata_field(metadata, "picture", "data", "url")

    # Microsoft and Twitter don't provide direct avatar URLs
    return None
