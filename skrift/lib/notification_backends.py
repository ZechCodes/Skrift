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
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable
from uuid import UUID, uuid4

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
    async def store(self, scope: str, scope_id: str, notification: Notification) -> None: ...
    async def remove(self, notification_id: UUID) -> None: ...
    async def remove_by_group(self, scope: str, scope_id: str, group: str) -> UUID | None: ...
    async def get_queued(self, scope: str, scope_id: str) -> list[Notification]: ...
    async def get_since(self, scope: str, scope_id: str, since: float) -> list[Notification]: ...
    async def get_mode(self, notification_id: UUID) -> str | None: ...
    async def publish(self, message: dict) -> None: ...
    def on_remote_message(self, callback: Callable[[dict], Any]) -> None: ...


class InMemoryBackend:
    """Dict-based storage with no cross-replica fanout. Default backend."""

    def __init__(self, **kwargs: Any) -> None:
        self._session_queues: dict[str, dict[UUID, Notification]] = {}
        self._user_queues: dict[str, dict[UUID, Notification]] = {}
        self._callback: Callable[[dict], Any] | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def store(self, scope: str, scope_id: str, notification: Notification) -> None:
        queues = self._session_queues if scope == "session" else self._user_queues
        if notification.group:
            q = queues.get(scope_id, {})
            old = self._dismiss_by_group(q, notification.group)
            if old is not None:
                return  # caller handles dismissed event push via returned old id
        queues.setdefault(scope_id, {})[notification.id] = notification

    async def remove(self, notification_id: UUID) -> None:
        for queues in (self._session_queues, self._user_queues):
            for q in queues.values():
                if notification_id in q:
                    del q[notification_id]
                    return

    async def remove_by_group(self, scope: str, scope_id: str, group: str) -> UUID | None:
        queues = self._session_queues if scope == "session" else self._user_queues
        q = queues.get(scope_id, {})
        old = self._dismiss_by_group(q, group)
        return old.id if old else None

    async def get_queued(self, scope: str, scope_id: str) -> list[Notification]:
        queues = self._session_queues if scope == "session" else self._user_queues
        q = queues.get(scope_id, {})
        return sorted(
            (n for n in q.values() if n.mode == NotificationMode.QUEUED),
            key=lambda n: n.created_at,
        )

    async def get_since(self, scope: str, scope_id: str, since: float) -> list[Notification]:
        queues = self._session_queues if scope == "session" else self._user_queues
        q = queues.get(scope_id, {})
        return sorted(
            (n for n in q.values() if n.mode == NotificationMode.TIMESERIES and n.created_at > since),
            key=lambda n: n.created_at,
        )

    async def get_mode(self, notification_id: UUID) -> str | None:
        notif = self.find_by_id(notification_id)
        return notif.mode.value if notif else None

    async def publish(self, message: dict) -> None:
        pass  # No cross-replica fanout in single-process mode

    def on_remote_message(self, callback: Callable[[dict], Any]) -> None:
        self._callback = callback

    # -- InMemory-specific helpers used by NotificationService --

    def find_by_id(self, notification_id: UUID) -> Notification | None:
        for queues in (self._session_queues, self._user_queues):
            for q in queues.values():
                if notification_id in q:
                    return q[notification_id]
        return None

    def get_session_queue(self, nid: str) -> dict[UUID, Notification]:
        return self._session_queues.get(nid, {})

    def get_user_queue(self, user_id: str) -> dict[UUID, Notification]:
        return self._user_queues.get(user_id, {})

    def store_sync(self, scope: str, scope_id: str, notification: Notification) -> Notification | None:
        """Synchronous store that returns the old notification if group-replaced."""
        queues = self._session_queues if scope == "session" else self._user_queues
        old: Notification | None = None
        if notification.group:
            q = queues.get(scope_id, {})
            old = self._dismiss_by_group(q, notification.group)
        queues.setdefault(scope_id, {})[notification.id] = notification
        return old

    def remove_sync(self, scope: str, scope_id: str, notification_id: UUID) -> bool:
        queues = self._session_queues if scope == "session" else self._user_queues
        q = queues.get(scope_id, {})
        if notification_id in q:
            del q[notification_id]
            return True
        return False

    def remove_by_group_sync(self, scope: str, scope_id: str, group: str) -> Notification | None:
        queues = self._session_queues if scope == "session" else self._user_queues
        q = queues.get(scope_id, {})
        return self._dismiss_by_group(q, group)

    def get_all_queued(self, nid: str, user_id: str | None) -> list[Notification]:
        merged: dict[UUID, Notification] = {}
        for uid, n in self._session_queues.get(nid, {}).items():
            if n.mode == NotificationMode.QUEUED:
                merged[uid] = n
        if user_id:
            for uid, n in self._user_queues.get(user_id, {}).items():
                if n.mode == NotificationMode.QUEUED:
                    merged[uid] = n
        return sorted(merged.values(), key=lambda n: n.created_at)

    def get_all_since(self, nid: str, user_id: str | None, since: float) -> list[Notification]:
        merged: dict[UUID, Notification] = {}
        for uid, n in self._session_queues.get(nid, {}).items():
            if n.mode == NotificationMode.TIMESERIES and n.created_at > since:
                merged[uid] = n
        if user_id:
            for uid, n in self._user_queues.get(user_id, {}).items():
                if n.mode == NotificationMode.TIMESERIES and n.created_at > since:
                    merged[uid] = n
        return sorted(merged.values(), key=lambda n: n.created_at)

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

    async def store(self, scope: str, scope_id: str, notification: Notification) -> None:
        from skrift.db.models.notification import StoredNotification

        if notification.group:
            await self.remove_by_group(scope, scope_id, notification.group)

        row = StoredNotification(
            id=notification.id,
            scope=scope,
            scope_id=scope_id,
            type=notification.type,
            payload_json=json.dumps(notification.payload),
            group_key=notification.group,
            delivery_mode=notification.mode.value,
            notified_at=datetime.fromtimestamp(notification.created_at, tz=timezone.utc),
        )
        async with self._session_maker() as session:
            session.add(row)
            await session.commit()

    async def remove(self, notification_id: UUID) -> None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import delete

        async with self._session_maker() as session:
            await session.execute(
                delete(StoredNotification).where(StoredNotification.id == notification_id)
            )
            await session.commit()

    async def remove_by_group(self, scope: str, scope_id: str, group: str) -> UUID | None:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select, delete

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification.id).where(
                    StoredNotification.scope == scope,
                    StoredNotification.scope_id == scope_id,
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

    async def get_queued(self, scope: str, scope_id: str) -> list[Notification]:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification)
                .where(
                    StoredNotification.scope == scope,
                    StoredNotification.scope_id == scope_id,
                    StoredNotification.delivery_mode == NotificationMode.QUEUED.value,
                )
                .order_by(StoredNotification.notified_at)
            )
            rows = result.scalars().all()
            return [
                Notification(
                    type=row.type,
                    id=row.id,
                    created_at=row.notified_at.timestamp(),
                    payload=json.loads(row.payload_json),
                    group=row.group_key,
                    mode=NotificationMode(row.delivery_mode),
                )
                for row in rows
            ]

    async def get_since(self, scope: str, scope_id: str, since: float) -> list[Notification]:
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
        async with self._session_maker() as session:
            result = await session.execute(
                select(StoredNotification)
                .where(
                    StoredNotification.scope == scope,
                    StoredNotification.scope_id == scope_id,
                    StoredNotification.delivery_mode == NotificationMode.TIMESERIES.value,
                    StoredNotification.notified_at > since_dt,
                )
                .order_by(StoredNotification.notified_at)
            )
            rows = result.scalars().all()
            return [
                Notification(
                    type=row.type,
                    id=row.id,
                    created_at=row.notified_at.timestamp(),
                    payload=json.loads(row.payload_json),
                    group=row.group_key,
                    mode=NotificationMode(row.delivery_mode),
                )
                for row in rows
            ]

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
