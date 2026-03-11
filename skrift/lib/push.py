"""Web Push notification support with VAPID authentication.

Provides three layers:

1. Low-level ``send_push()`` — sends to all subscriptions for a user.
2. VAPID key management — auto-generates on first use, cached in memory.
3. Unified ``notify()`` — SSE for connected clients, Web Push fallback
   for disconnected users. Single dispatch point for app code.

Usage:
    from skrift.lib.push import send_push, get_vapid_public_key, notify

    # Get the public key for frontend subscription
    public_key = await get_vapid_public_key(db_session)

    # Send a push notification to a user
    await send_push(
        db_session,
        user_id="some-uuid",
        title="New message",
        body="Claude needs your input",
        url="/chat/session-abc",
    )

    # Unified: SSE when connected, push when not
    await notify(
        db_session,
        user_id="some-uuid",
        event="new_message",
        data={"title": "New message", "body": "Claude is waiting"},
        push_fallback=True,
    )
"""

import base64
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# VAPID key setting keys
VAPID_PRIVATE_KEY = "webpush:vapid_private"
VAPID_PUBLIC_KEY = "webpush:vapid_public"

# In-memory cache for VAPID keys (loaded once from DB)
_vapid_private_key: str | None = None
_vapid_public_key: str | None = None


def _generate_vapid_keys() -> tuple[str, str]:
    """Generate a new VAPID keypair.

    Returns:
        Tuple of (private_key_b64url, public_key_b64url) in unpadded base64url format.
    """
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

    private_key = generate_private_key(SECP256R1())

    # DER-encoded private key (PKCS8) for pywebpush compatibility
    private_bytes = private_key.private_bytes(
        Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
    )
    private_b64 = base64.urlsafe_b64encode(private_bytes).decode().rstrip("=")

    # Uncompressed public key point (65 bytes) for the browser
    public_bytes = private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    public_b64 = base64.urlsafe_b64encode(public_bytes).decode().rstrip("=")

    return private_b64, public_b64


async def _ensure_vapid_keys(db_session: AsyncSession) -> tuple[str, str]:
    """Ensure VAPID keys exist, generating them on first use.

    Returns:
        Tuple of (private_key_b64url, public_key_b64url).
    """
    global _vapid_private_key, _vapid_public_key

    if _vapid_private_key and _vapid_public_key:
        return _vapid_private_key, _vapid_public_key

    from skrift.db.services.setting_service import get_setting, set_setting

    private = await get_setting(db_session, VAPID_PRIVATE_KEY)
    public = await get_setting(db_session, VAPID_PUBLIC_KEY)

    if private and public:
        _vapid_private_key = private
        _vapid_public_key = public
        return private, public

    # Generate new keypair
    private, public = _generate_vapid_keys()
    await set_setting(db_session, VAPID_PRIVATE_KEY, private)
    await set_setting(db_session, VAPID_PUBLIC_KEY, public)

    _vapid_private_key = private
    _vapid_public_key = public
    logger.info("Generated new VAPID keypair for Web Push")
    return private, public


async def get_vapid_public_key(db_session: AsyncSession) -> str:
    """Get the VAPID public key (base64url-encoded, unpadded).

    Generates a keypair on first use.
    """
    _, public = await _ensure_vapid_keys(db_session)
    return public


async def save_subscription(
    db_session: AsyncSession,
    user_id: str,
    endpoint: str,
    key_p256dh: str,
    key_auth: str,
) -> None:
    """Save or update a push subscription for a user."""
    from skrift.db.models.push_subscription import PushSubscription

    # Check if subscription already exists (by endpoint)
    result = await db_session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.user_id = user_id
        existing.key_p256dh = key_p256dh
        existing.key_auth = key_auth
    else:
        sub = PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            key_p256dh=key_p256dh,
            key_auth=key_auth,
        )
        db_session.add(sub)

    await db_session.commit()


async def remove_subscription(db_session: AsyncSession, endpoint: str) -> bool:
    """Remove a push subscription by endpoint. Returns True if found."""
    from skrift.db.models.push_subscription import PushSubscription

    result = await db_session.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return False

    await db_session.delete(sub)
    await db_session.commit()
    return True


async def send_push(
    db_session: AsyncSession,
    user_id: str,
    title: str,
    body: str,
    url: str | None = None,
    tag: str | None = None,
) -> int:
    """Send a Web Push notification to all of a user's subscribed browsers.

    Args:
        db_session: Database session
        user_id: Target user ID
        title: Notification title
        body: Notification body text
        url: Optional URL to open when notification is clicked
        tag: Optional tag for notification grouping/replacement

    Returns:
        Number of notifications successfully sent.
    """
    from pywebpush import WebPushException, webpush_async

    from skrift.db.models.push_subscription import PushSubscription

    private_key, _ = await _ensure_vapid_keys(db_session)

    # Fetch all subscriptions for this user
    result = await db_session.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id)
    )
    subscriptions = list(result.scalars().all())

    if not subscriptions:
        return 0

    # Build the push payload
    payload = {"title": title, "body": body}
    if url:
        payload["url"] = url
    if tag:
        payload["tag"] = tag
    data = json.dumps(payload)

    # Add padding back for pywebpush
    padded_key = private_key + "=" * (4 - len(private_key) % 4) if len(private_key) % 4 else private_key

    sent = 0
    expired_ids: list[UUID] = []

    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {
                "p256dh": sub.key_p256dh,
                "auth": sub.key_auth,
            },
        }

        try:
            await webpush_async(
                subscription_info=subscription_info,
                data=data,
                vapid_private_key=padded_key,
                vapid_claims={"sub": "mailto:push@skrift.dev"},
            )
            sub.last_used_at = datetime.now(timezone.utc)
            sent += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                # Subscription expired or unsubscribed — clean up
                expired_ids.append(sub.id)
                logger.debug("Removing expired push subscription %s", sub.endpoint)
            else:
                logger.warning("Web Push failed for %s: %s", sub.endpoint, e)
        except Exception:
            logger.exception("Unexpected error sending push to %s", sub.endpoint)

    # Clean up expired subscriptions
    if expired_ids:
        await db_session.execute(
            delete(PushSubscription).where(PushSubscription.id.in_(expired_ids))
        )

    await db_session.commit()
    return sent


async def notify(
    db_session: AsyncSession,
    user_id: str,
    event: str,
    data: dict | None = None,
    *,
    push_fallback: bool = True,
    push_title: str | None = None,
    push_body: str | None = None,
    push_url: str | None = None,
    push_tag: str | None = None,
    group: str | None = None,
) -> None:
    """Unified notification dispatch: SSE for connected clients, Web Push for disconnected.

    Always sends via SSE. If ``push_fallback=True`` and the user has no active
    SSE connections, also sends a Web Push notification.

    Args:
        db_session: Database session
        user_id: Target user ID
        event: Notification event type (e.g. "new_message")
        data: Payload dict for the SSE notification
        push_fallback: If True, send push when no SSE connection is active
        push_title: Title for push notification (falls back to data["title"])
        push_body: Body for push notification (falls back to data["body"])
        push_url: URL to open on click (falls back to data["url"])
        push_tag: Tag for push grouping (falls back to event type)
        group: Notification group key for SSE
    """
    from skrift.lib.notifications import NotificationMode, notifications, notify_user

    payload = data or {}

    # Always send via SSE
    await notify_user(
        user_id,
        event,
        mode=NotificationMode.TIMESERIES,
        group=group,
        **payload,
    )

    # Check push_notify from payload: True=always, False=never, None=auto (fallback)
    push_notify = payload.get("push_notify")
    if push_notify is False or not push_fallback:
        return

    should_push = push_notify is True
    if not should_push:
        # Auto mode: only send push if no active SSE connection
        user_key = f"user:{user_id}"
        has_sse = notifications._registry.has_listeners(user_key)

        if not has_sse:
            children = notifications._registry._subscribers.get(user_key, set())
            has_sse = any(
                notifications._registry.has_listeners(child)
                for child in children
            )

        should_push = not has_sse

    if should_push:
            title = push_title or payload.get("title", event)
            body = push_body or payload.get("body", "")
            url = push_url or payload.get("url")
            tag = push_tag or event

            try:
                await send_push(
                    db_session,
                    user_id=user_id,
                    title=title,
                    body=body,
                    url=url,
                    tag=tag,
                )
            except Exception:
                logger.exception("Push fallback failed for user %s", user_id)


def setup_push_hook(session_maker) -> None:
    """Register a NOTIFICATION_SENT hook that triggers push fallback.

    Call this during app startup to enable automatic push notifications
    for users without active SSE connections.

    Args:
        session_maker: Async session factory (e.g. db_config.get_session)
    """
    from skrift.lib.hooks import NOTIFICATION_SENT, hooks
    from skrift.lib.notifications import Notification

    async def _push_on_notification(notification: Notification, scope: str, scope_id: str | None) -> None:
        """Hook: send push notification when user has no active SSE connection."""
        if scope != "user" or not scope_id:
            return

        # Check push_notify payload flag: True=always, False=never, None=auto
        push_notify = notification.payload.get("push_notify")
        if push_notify is False:
            return

        if push_notify is not True:
            # Auto mode: only send push if no active SSE connection
            from skrift.lib.notifications import notifications

            user_key = f"user:{scope_id}"
            has_sse = notifications._registry.has_listeners(user_key)
            if not has_sse:
                children = notifications._registry._subscribers.get(user_key, set())
                has_sse = any(
                    notifications._registry.has_listeners(child)
                    for child in children
                )

            if has_sse:
                return

        # No SSE — send push
        title = notification.payload.get("title", notification.type)
        body = notification.payload.get("body", "")
        url = notification.payload.get("url")
        tag = notification.payload.get("tag") or notification.group or notification.type

        try:
            async with session_maker() as db_session:
                await send_push(
                    db_session,
                    user_id=scope_id,
                    title=title,
                    body=body,
                    url=url,
                    tag=tag,
                )
        except Exception:
            logger.exception("Push hook failed for user %s", scope_id)

    hooks.add_action(NOTIFICATION_SENT, _push_on_notification, priority=50)
