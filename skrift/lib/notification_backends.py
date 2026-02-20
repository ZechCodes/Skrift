"""Pluggable notification backends for cross-replica fanout.

Built-in backends:
- InMemoryBackend: Dict-based storage, no fanout (single-process, default)
- RedisBackend: DB storage + Redis pub/sub fanout
- PgNotifyBackend: DB storage + PostgreSQL LISTEN/NOTIFY fanout
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable
from uuid import UUID

from skrift.lib.notifications import Notification, NotificationMode

if TYPE_CHECKING:
    from skrift.config import Settings

logger = logging.getLogger(__name__)

QUEUED_TTL_HOURS = 24
TIMESERIES_TTL_DAYS = 7


def load_backend(spec: str) -> type:
    """Import a backend class from a 'module:ClassName' string."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid backend spec '{spec}': must be in format 'module:ClassName'"
        )
    parts = spec.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid backend spec '{spec}': must contain exactly one colon"
        )
    module_path, class_name = parts
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


@runtime_checkable
class NotificationBackend(Protocol):
    """Interface for notification storage and cross-replica fanout."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # Storage — source_key based
    async def store(self, source_key: str, notification: Notification) -> UUID | None: ...
    async def remove(self, notification_id: UUID) -> str | None: ...
    async def remove_by_group(self, source_key: str, group: str) -> UUID | None: ...
    async def get_mode(self, notification_id: UUID) -> str | None: ...

    # Batch queries across multiple source keys
    async def get_queued_multi(self, source_keys: Collection[str]) -> list[Notification]: ...
    async def get_since_multi(self, source_keys: Collection[str], since: float) -> list[Notification]: ...

    # Persistent subscriptions
    async def get_persistent_subscriptions(self, subscriber_key: str) -> list[str]: ...
    async def add_subscription(self, subscriber_key: str, source_key: str) -> None: ...
    async def remove_subscription(self, subscriber_key: str, source_key: str) -> None: ...

    # Per-subscriber dismissals
    async def find_by_group(self, source_key: str, group: str) -> UUID | None: ...
    async def dismiss_for_subscriber(self, subscriber_key: str, notification_id: UUID) -> str | None: ...
    async def get_dismissed_ids(self, subscriber_key: str, notification_ids: Collection[UUID]) -> set[UUID]: ...
    async def cleanup_dismissed(self) -> None: ...

    # Cross-replica fanout
    async def publish(self, message: dict) -> None: ...
    def on_remote_message(self, callback: Callable[[dict], Any]) -> None: ...


class InMemoryBackend:
    """Dict-based storage with no cross-replica fanout. Default backend."""

    def __init__(self, **kwargs: Any) -> None:
        self._queues: dict[str, dict[UUID, Notification]] = {}
        self._subscriptions: dict[str, set[str]] = {}  # subscriber_key → {source_keys}
        self._dismissed: dict[str, set[UUID]] = {}  # subscriber_key → {notification_ids}
        self._callback: Callable[[dict], Any] | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def store(self, source_key: str, notification: Notification) -> UUID | None:
        old_id: UUID | None = None
        if notification.group:
            q = self._queues.get(source_key, {})
            old = self._dismiss_by_group(q, notification.group)
            if old is not None:
                old_id = old.id
        self._queues.setdefault(source_key, {})[notification.id] = notification
        return old_id

    async def remove(self, notification_id: UUID) -> str | None:
        for source_key, q in self._queues.items():
            if notification_id in q:
                del q[notification_id]
                return source_key
        return None

    async def remove_by_group(self, source_key: str, group: str) -> UUID | None:
        q = self._queues.get(source_key, {})
        old = self._dismiss_by_group(q, group)
        return old.id if old else None

    async def get_queued_multi(self, source_keys: Collection[str]) -> list[Notification]:
        merged: dict[UUID, Notification] = {}
        for key in source_keys:
            q = self._queues.get(key, {})
            for n in q.values():
                if n.mode == NotificationMode.QUEUED:
                    merged[n.id] = n
        return sorted(merged.values(), key=lambda n: n.created_at)

    async def get_since_multi(self, source_keys: Collection[str], since: float) -> list[Notification]:
        merged: dict[UUID, Notification] = {}
        for key in source_keys:
            q = self._queues.get(key, {})
            for n in q.values():
                if n.mode == NotificationMode.TIMESERIES and n.created_at > since:
                    merged[n.id] = n
        return sorted(merged.values(), key=lambda n: n.created_at)

    async def get_mode(self, notification_id: UUID) -> str | None:
        notif = self._find_by_id(notification_id)
        return notif.mode.value if notif else None

    async def get_persistent_subscriptions(self, subscriber_key: str) -> list[str]:
        return list(self._subscriptions.get(subscriber_key, set()))

    async def add_subscription(self, subscriber_key: str, source_key: str) -> None:
        self._subscriptions.setdefault(subscriber_key, set()).add(source_key)

    async def remove_subscription(self, subscriber_key: str, source_key: str) -> None:
        subs = self._subscriptions.get(subscriber_key)
        if subs:
            subs.discard(source_key)

    async def publish(self, message: dict) -> None:
        pass  # No cross-replica fanout in single-process mode

    def on_remote_message(self, callback: Callable[[dict], Any]) -> None:
        self._callback = callback

    async def find_by_group(self, source_key: str, group: str) -> UUID | None:
        q = self._queues.get(source_key, {})
        for nid, notif in q.items():
            if notif.group == group:
                return nid
        return None

    async def dismiss_for_subscriber(self, subscriber_key: str, notification_id: UUID) -> str | None:
        for source_key, q in self._queues.items():
            if notification_id in q:
                self._dismissed.setdefault(subscriber_key, set()).add(notification_id)
                return source_key
        return None

    async def get_dismissed_ids(self, subscriber_key: str, notification_ids: Collection[UUID]) -> set[UUID]:
        dismissed = self._dismissed.get(subscriber_key, set())
        return dismissed & set(notification_ids)

    async def cleanup_dismissed(self) -> None:
        live_ids: set[UUID] = set()
        for q in self._queues.values():
            live_ids.update(q.keys())
        for sub_key in list(self._dismissed):
            self._dismissed[sub_key] = self._dismissed[sub_key] & live_ids
            if not self._dismissed[sub_key]:
                del self._dismissed[sub_key]

    def _find_by_id(self, notification_id: UUID) -> Notification | None:
        for q in self._queues.values():
            if notification_id in q:
                return q[notification_id]
        return None

    @staticmethod
    def _dismiss_by_group(queue: dict[UUID, Notification], group: str) -> Notification | None:
        for nid, notif in queue.items():
            if notif.group == group:
                del queue[nid]
                return notif
        return None


class _DatabaseStorageMixin:
    """Shared DB operations for Redis and PgNotify backends."""

    _session_maker: Any
    _cleanup_task: asyncio.Task | None

    def _init_db(self, session_maker: Any) -> None:
        self._session_maker = session_maker
        self._cleanup_task = None

    async def _start_cleanup(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _stop_cleanup(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(600)  # 10 minutes
                await self._delete_old_notifications()
                await self.cleanup_dismissed()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Notification cleanup error", exc_info=True)

    async def _delete_old_notifications(self) -> None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import delete, or_, and_

        queued_cutoff = datetime.now(timezone.utc) - timedelta(hours=QUEUED_TTL_HOURS)
        timeseries_cutoff = datetime.now(timezone.utc) - timedelta(days=TIMESERIES_TTL_DAYS)
        async with self._session_maker() as session:
            await session.execute(
                delete(StoredNotification).where(
                    or_(
                        and_(
                            StoredNotification.delivery_mode == NotificationMode.QUEUED.value,
                            StoredNotification.notified_at < queued_cutoff,
                        ),
                        and_(
                            StoredNotification.delivery_mode == NotificationMode.TIMESERIES.value,
                            StoredNotification.notified_at < timeseries_cutoff,
                        ),
                    )
                )
            )
            await session.commit()

    async def store(self, source_key: str, notification: Notification) -> UUID | None:
        from skrift.db.models.notification import StoredNotification

        old_id: UUID | None = None
        if notification.group:
            old_id = await self.remove_by_group(source_key, notification.group)

        # Derive scope/scope_id from source_key for backwards compat
        scope, scope_id = _parse_source_key(source_key)

        row = StoredNotification(
            id=notification.id,
            scope=scope,
            scope_id=scope_id,
            source_key=source_key,
            type=notification.type,
            payload_json=json.dumps(notification.payload),
            group_key=notification.group,
            delivery_mode=notification.mode.value,
            notified_at=datetime.fromtimestamp(notification.created_at, tz=timezone.utc),
        )
        async with self._session_maker() as session:
            session.add(row)
            await session.commit()
        return old_id

    async def remove(self, notification_id: UUID) -> str | None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select, delete

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification.source_key).where(
                    StoredNotification.id == notification_id
                )
            )
            source_key = result.scalar_one_or_none()
            if source_key is not None:
                await session.execute(
                    delete(StoredNotification).where(StoredNotification.id == notification_id)
                )
                await session.commit()
            return source_key

    async def remove_by_group(self, source_key: str, group: str) -> UUID | None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select, delete

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification.id).where(
                    StoredNotification.source_key == source_key,
                    StoredNotification.group_key == group,
                )
            )
            old_id = result.scalar_one_or_none()
            if old_id:
                await session.execute(
                    delete(StoredNotification).where(StoredNotification.id == old_id)
                )
                await session.commit()
            return old_id

    async def get_queued_multi(self, source_keys: Collection[str]) -> list[Notification]:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        if not source_keys:
            return []

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification)
                .where(
                    StoredNotification.source_key.in_(source_keys),
                    StoredNotification.delivery_mode == NotificationMode.QUEUED.value,
                )
                .order_by(StoredNotification.notified_at)
            )
            rows = result.scalars().all()
            return [self._row_to_notification(row) for row in rows]

    async def get_since_multi(self, source_keys: Collection[str], since: float) -> list[Notification]:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        if not source_keys:
            return []

        since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification)
                .where(
                    StoredNotification.source_key.in_(source_keys),
                    StoredNotification.delivery_mode == NotificationMode.TIMESERIES.value,
                    StoredNotification.notified_at > since_dt,
                )
                .order_by(StoredNotification.notified_at)
            )
            rows = result.scalars().all()
            return [self._row_to_notification(row) for row in rows]

    async def get_persistent_subscriptions(self, subscriber_key: str) -> list[str]:
        from skrift.db.models.notification import NotificationSubscription
        from sqlalchemy import select

        async with self._session_maker() as session:
            result = await session.execute(
                select(NotificationSubscription.source_key).where(
                    NotificationSubscription.subscriber_key == subscriber_key
                )
            )
            return list(result.scalars().all())

    async def add_subscription(self, subscriber_key: str, source_key: str) -> None:
        from skrift.db.models.notification import NotificationSubscription
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with self._session_maker() as session:
            stmt = pg_insert(NotificationSubscription).values(
                subscriber_key=subscriber_key,
                source_key=source_key,
            ).on_conflict_do_nothing(
                constraint="uq_notification_sub_subscriber_source"
            )
            await session.execute(stmt)
            await session.commit()

    async def remove_subscription(self, subscriber_key: str, source_key: str) -> None:
        from skrift.db.models.notification import NotificationSubscription
        from sqlalchemy import delete

        async with self._session_maker() as session:
            await session.execute(
                delete(NotificationSubscription).where(
                    NotificationSubscription.subscriber_key == subscriber_key,
                    NotificationSubscription.source_key == source_key,
                )
            )
            await session.commit()

    @staticmethod
    def _row_to_notification(row) -> Notification:
        return Notification(
            type=row.type,
            id=row.id,
            created_at=row.notified_at.timestamp(),
            payload=json.loads(row.payload_json),
            group=row.group_key,
            mode=NotificationMode(row.delivery_mode),
        )

    async def get_mode(self, notification_id: UUID) -> str | None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification.delivery_mode).where(
                    StoredNotification.id == notification_id
                )
            )
            return result.scalar_one_or_none()

    async def find_by_group(self, source_key: str, group: str) -> UUID | None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification.id).where(
                    StoredNotification.source_key == source_key,
                    StoredNotification.group_key == group,
                )
            )
            return result.scalar_one_or_none()

    async def dismiss_for_subscriber(self, subscriber_key: str, notification_id: UUID) -> str | None:
        from skrift.db.models.notification import DismissedNotification, StoredNotification
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification.source_key).where(
                    StoredNotification.id == notification_id
                )
            )
            source_key = result.scalar_one_or_none()
            if source_key is None:
                return None

            stmt = pg_insert(DismissedNotification).values(
                subscriber_key=subscriber_key,
                notification_id=notification_id,
            ).on_conflict_do_nothing(
                constraint="uq_dismissed_subscriber_notification"
            )
            await session.execute(stmt)
            await session.commit()
            return source_key

    async def get_dismissed_ids(self, subscriber_key: str, notification_ids: Collection[UUID]) -> set[UUID]:
        from skrift.db.models.notification import DismissedNotification
        from sqlalchemy import select

        if not notification_ids:
            return set()

        async with self._session_maker() as session:
            result = await session.execute(
                select(DismissedNotification.notification_id).where(
                    DismissedNotification.subscriber_key == subscriber_key,
                    DismissedNotification.notification_id.in_(notification_ids),
                )
            )
            return set(result.scalars().all())

    async def cleanup_dismissed(self) -> None:
        from skrift.db.models.notification import DismissedNotification, StoredNotification
        from sqlalchemy import delete, select

        async with self._session_maker() as session:
            await session.execute(
                delete(DismissedNotification).where(
                    DismissedNotification.notification_id.notin_(
                        select(StoredNotification.id)
                    )
                )
            )
            await session.commit()


def _parse_source_key(source_key: str) -> tuple[str, str]:
    """Parse a source_key into (scope, scope_id) for backwards compat."""
    if source_key == "global":
        return ("broadcast", "")
    if ":" in source_key:
        scope, scope_id = source_key.split(":", 1)
        return (scope, scope_id)
    return (source_key, "")


class RedisBackend(_DatabaseStorageMixin):
    """DB storage + Redis pub/sub for cross-replica fanout."""

    def __init__(self, *, settings: Settings, session_maker: Any, **kwargs: Any) -> None:
        self._init_db(session_maker)
        self._redis_url = settings.redis.url
        self._channel = settings.redis.make_key("skrift", "notifications")
        self._client: Any = None
        self._pubsub: Any = None
        self._reader_task: asyncio.Task | None = None
        self._callback: Callable[[dict], Any] | None = None

    async def start(self) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError(
                "redis package is required for RedisBackend. "
                "Install it with: pip install 'skrift[redis]'"
            )
        self._client = aioredis.Redis.from_url(self._redis_url)
        self._pubsub = self._client.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._start_cleanup()

    async def stop(self) -> None:
        await self._stop_cleanup()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe(self._channel)
            await self._pubsub.aclose()
        if self._client:
            await self._client.aclose()

    async def publish(self, message: dict) -> None:
        if self._client:
            await self._client.publish(self._channel, json.dumps(message))

    def on_remote_message(self, callback: Callable[[dict], Any]) -> None:
        self._callback = callback

    async def _reader_loop(self) -> None:
        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    data = json.loads(message["data"])
                    if self._callback:
                        await self._callback(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Redis reader error", exc_info=True)
                await asyncio.sleep(1)


class PgNotifyBackend(_DatabaseStorageMixin):
    """DB storage + PostgreSQL LISTEN/NOTIFY for cross-replica fanout."""

    CHANNEL = "skrift_notifications"

    def __init__(self, *, settings: Settings, session_maker: Any, **kwargs: Any) -> None:
        self._init_db(session_maker)
        # Derive raw DSN from SQLAlchemy URL (strip +asyncpg suffix)
        self._dsn = settings.db.url.replace("+asyncpg", "").replace("postgresql://", "postgresql://")
        self._listener_conn: Any = None
        self._reader_task: asyncio.Task | None = None
        self._callback: Callable[[dict], Any] | None = None
        self._backoff = 1

    async def start(self) -> None:
        await self._connect_listener()
        await self._start_cleanup()

    async def stop(self) -> None:
        await self._stop_cleanup()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._listener_conn:
            try:
                await self._listener_conn.close()
            except Exception:
                pass

    async def _connect_listener(self) -> None:
        import asyncpg

        self._listener_conn = await asyncpg.connect(self._dsn)
        await self._listener_conn.add_listener(self.CHANNEL, self._on_notification)
        self._reader_task = asyncio.create_task(self._keepalive_loop())
        self._backoff = 1

    def _on_notification(self, connection: Any, pid: int, channel: str, payload: str) -> None:
        if self._callback:
            try:
                data = json.loads(payload)
                asyncio.get_event_loop().create_task(self._callback(data))
            except Exception:
                logger.warning("PgNotify parse error", exc_info=True)

    async def _keepalive_loop(self) -> None:
        """Keep the listener connection alive, reconnecting with backoff on failure."""
        while True:
            try:
                # Periodic check that connection is alive
                await asyncio.sleep(30)
                if self._listener_conn.is_closed():
                    raise ConnectionError("Listener connection closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("PgNotify listener lost, reconnecting...", exc_info=True)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 60)
                try:
                    await self._connect_listener()
                except Exception:
                    logger.warning("PgNotify reconnect failed", exc_info=True)

    async def publish(self, message: dict) -> None:
        payload = json.dumps(message)
        async with self._session_maker() as session:
            await session.execute(
                __import__("sqlalchemy").text(
                    f"SELECT pg_notify('{self.CHANNEL}', :payload)"
                ),
                {"payload": payload},
            )
            await session.commit()

    def on_remote_message(self, callback: Callable[[dict], Any]) -> None:
        self._callback = callback
