"""Real-time notification service for Skrift CMS.

Provides session-scoped and user-scoped notifications delivered via
Server-Sent Events (SSE). Notifications persist in their backend
queues until explicitly dismissed via the DELETE endpoint.

The backend is pluggable via app.yaml — see notification_backends.py.

Usage:
    from skrift.lib.notifications import notify_session, notify_user

    await notify_session(nid, "generic", title="Page published", message="Now live.")
    await notify_user(user_id, "generic", title="New comment", message="On your post.")
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from skrift.lib.notification_backends import NotificationBackend


class NotificationMode(str, Enum):
    QUEUED = "queued"
    TIMESERIES = "timeseries"
    EPHEMERAL = "ephemeral"


class NotDismissibleError(Exception):
    """Raised when attempting to dismiss a non-queued notification."""


@dataclass
class Notification:
    type: str
    id: UUID = field(default_factory=uuid4)
    created_at: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    group: str | None = None
    mode: NotificationMode = NotificationMode.QUEUED

    def to_dict(self) -> dict[str, Any]:
        d = {
            "type": self.type,
            "id": str(self.id),
            "mode": self.mode.value,
            "created_at": self.created_at,
            **self.payload,
        }
        if self.group is not None:
            d["group"] = self.group
        return d


class NotificationService:
    """Manages notification queues and active SSE connections.

    Storage and cross-replica fanout are delegated to a pluggable backend.
    Local SSE connection management (_connections, _connection_user_ids)
    stays here since it's inherently per-replica.
    """

    def __init__(self) -> None:
        self._connections: dict[str, list[asyncio.Queue]] = {}
        self._connection_user_ids: dict[str, str | None] = {}
        self._backend: NotificationBackend | None = None

    def set_backend(self, backend: NotificationBackend) -> None:
        self._backend = backend
        backend.on_remote_message(self._handle_remote)

    def _get_backend(self) -> NotificationBackend:
        if self._backend is None:
            from skrift.lib.notification_backends import InMemoryBackend
            self._backend = InMemoryBackend()
            self._backend.on_remote_message(self._handle_remote)
        return self._backend

    async def send_to_session(self, nid: str, notification: Notification) -> None:
        """Store a notification in the session queue and push to active connections."""
        backend = self._get_backend()

        if notification.mode != NotificationMode.EPHEMERAL:
            from skrift.lib.notification_backends import InMemoryBackend
            if isinstance(backend, InMemoryBackend):
                old = backend.store_sync("session", nid, notification)
                if old is not None:
                    dismissed = Notification(type="dismissed", id=old.id, payload={})
                    for q in self._connections.get(nid, []):
                        q.put_nowait(dismissed)
            else:
                if notification.group:
                    old_id = await backend.remove_by_group("session", nid, notification.group)
                    if old_id is not None:
                        dismissed = Notification(type="dismissed", id=old_id, payload={})
                        for q in self._connections.get(nid, []):
                            q.put_nowait(dismissed)
                await backend.store("session", nid, notification)

        for q in self._connections.get(nid, []):
            q.put_nowait(notification)

        await backend.publish({
            "a": "s", "sc": "session", "sid": nid,
            "n": notification.to_dict(),
        })

    async def send_to_user(self, user_id: str, notification: Notification) -> None:
        """Store a notification in the user queue and push to all connections for this user."""
        backend = self._get_backend()

        if notification.mode != NotificationMode.EPHEMERAL:
            from skrift.lib.notification_backends import InMemoryBackend
            if isinstance(backend, InMemoryBackend):
                old = backend.store_sync("user", user_id, notification)
                if old is not None:
                    dismissed = Notification(type="dismissed", id=old.id, payload={})
                    for sid, uid in self._connection_user_ids.items():
                        if uid == user_id:
                            for q in self._connections.get(sid, []):
                                q.put_nowait(dismissed)
            else:
                if notification.group:
                    old_id = await backend.remove_by_group("user", user_id, notification.group)
                    if old_id is not None:
                        dismissed = Notification(type="dismissed", id=old_id, payload={})
                        for sid, uid in self._connection_user_ids.items():
                            if uid == user_id:
                                for q in self._connections.get(sid, []):
                                    q.put_nowait(dismissed)
                await backend.store("user", user_id, notification)

        for sid, uid in self._connection_user_ids.items():
            if uid == user_id:
                for q in self._connections.get(sid, []):
                    q.put_nowait(notification)

        await backend.publish({
            "a": "s", "sc": "user", "sid": user_id,
            "n": notification.to_dict(),
        })

    async def broadcast(self, notification: Notification) -> None:
        """Push an ephemeral notification to ALL active connections. Not stored — won't replay on reconnect."""
        for queues in self._connections.values():
            for q in queues:
                q.put_nowait(notification)

        await self._get_backend().publish({
            "a": "b",
            "n": notification.to_dict(),
        })

    async def dismiss(
        self,
        nid: str,
        user_id: str | None,
        notification_id: UUID | None = None,
        *,
        group: str | None = None,
    ) -> bool:
        """Remove a notification from queues and push a dismissed event.

        Lookup by *notification_id* (UUID) or *group* key — supply one.
        Returns True if a notification was found and removed.
        Raises NotDismissibleError if the notification is timeseries mode.
        """
        backend = self._get_backend()
        dismissed_id: UUID | None = None

        from skrift.lib.notification_backends import InMemoryBackend
        if isinstance(backend, InMemoryBackend):
            # Check mode before dismissing (InMemory path)
            if notification_id is not None:
                notif = backend.find_by_id(notification_id)
                if notif is not None and notif.mode == NotificationMode.TIMESERIES:
                    raise NotDismissibleError(
                        f"Cannot dismiss timeseries notification {notification_id}"
                    )

            # Session queue
            if notification_id is not None:
                if backend.remove_sync("session", nid, notification_id):
                    dismissed_id = notification_id
            elif group is not None:
                old = backend.remove_by_group_sync("session", nid, group)
                if old is not None:
                    dismissed_id = old.id

            # User queue
            if user_id:
                if notification_id is not None:
                    if backend.remove_sync("user", user_id, notification_id):
                        dismissed_id = notification_id
                elif group is not None:
                    old = backend.remove_by_group_sync("user", user_id, group)
                    if old is not None:
                        dismissed_id = old.id
        else:
            # DB path — check mode before deleting
            if notification_id is not None:
                mode = await backend.get_mode(notification_id)
                if mode == NotificationMode.TIMESERIES.value:
                    raise NotDismissibleError(
                        f"Cannot dismiss timeseries notification {notification_id}"
                    )
                await backend.remove(notification_id)
                dismissed_id = notification_id
            elif group is not None:
                old_id = await backend.remove_by_group("session", nid, group)
                if old_id:
                    dismissed_id = old_id
                if user_id:
                    old_id = await backend.remove_by_group("user", user_id, group)
                    if old_id:
                        dismissed_id = old_id

        if dismissed_id is not None:
            dismissed = Notification(
                type="dismissed", id=dismissed_id, payload={}
            )
            for q in self._connections.get(nid, []):
                q.put_nowait(dismissed)
            if user_id:
                for other_nid, uid in self._connection_user_ids.items():
                    if uid == user_id and other_nid != nid:
                        for q in self._connections.get(other_nid, []):
                            q.put_nowait(dismissed)

            await backend.publish({
                "a": "d", "sc": "session", "sid": nid,
                "uid": user_id,
                "nid": str(dismissed_id),
            })

        return dismissed_id is not None

    async def get_queued(
        self, nid: str, user_id: str | None
    ) -> list[Notification]:
        """Return all queued notifications sorted oldest-first."""
        backend = self._get_backend()

        from skrift.lib.notification_backends import InMemoryBackend
        if isinstance(backend, InMemoryBackend):
            return backend.get_all_queued(nid, user_id)

        session_notifs = await backend.get_queued("session", nid)
        user_notifs = await backend.get_queued("user", user_id) if user_id else []
        merged: dict[UUID, Notification] = {}
        for n in session_notifs + user_notifs:
            merged[n.id] = n
        return sorted(merged.values(), key=lambda n: n.created_at)

    async def get_since(
        self, nid: str, user_id: str | None, since: float
    ) -> list[Notification]:
        """Return timeseries notifications created after *since* timestamp."""
        backend = self._get_backend()

        from skrift.lib.notification_backends import InMemoryBackend
        if isinstance(backend, InMemoryBackend):
            return backend.get_all_since(nid, user_id, since)

        session_notifs = await backend.get_since("session", nid, since)
        user_notifs = await backend.get_since("user", user_id, since) if user_id else []
        merged: dict[UUID, Notification] = {}
        for n in session_notifs + user_notifs:
            merged[n.id] = n
        return sorted(merged.values(), key=lambda n: n.created_at)

    def register_connection(
        self, nid: str, user_id: str | None
    ) -> asyncio.Queue:
        """Register a new SSE connection and return its queue."""
        q: asyncio.Queue = asyncio.Queue()
        self._connections.setdefault(nid, []).append(q)
        self._connection_user_ids[nid] = user_id
        return q

    def unregister_connection(self, nid: str, q: asyncio.Queue) -> None:
        """Remove a connection on disconnect."""
        conns = self._connections.get(nid, [])
        try:
            conns.remove(q)
        except ValueError:
            pass
        if not conns:
            self._connections.pop(nid, None)
            self._connection_user_ids.pop(nid, None)

    async def _handle_remote(self, message: dict) -> None:
        """Process a message received from another replica via pub/sub."""
        action = message.get("a")

        if action == "s":
            # Send notification — push to matching local connections
            scope = message.get("sc")
            scope_id = message.get("sid")
            n_data = message.get("n", {})
            mode_val = n_data.get("mode", NotificationMode.QUEUED.value)
            notification = Notification(
                type=n_data.get("type", ""),
                id=UUID(n_data["id"]),
                payload={
                    k: v for k, v in n_data.items()
                    if k not in ("type", "id", "group", "mode", "created_at")
                },
                group=n_data.get("group"),
                mode=NotificationMode(mode_val),
            )
            if "created_at" in n_data:
                notification.created_at = n_data["created_at"]

            if scope == "session":
                for q in self._connections.get(scope_id, []):
                    q.put_nowait(notification)
            elif scope == "user":
                for sid, uid in self._connection_user_ids.items():
                    if uid == scope_id:
                        for q in self._connections.get(sid, []):
                            q.put_nowait(notification)

        elif action == "d":
            # Dismiss
            dismissed_id = UUID(message["nid"])
            dismissed = Notification(type="dismissed", id=dismissed_id, payload={})
            sid = message.get("sid", "")
            uid = message.get("uid")

            for q in self._connections.get(sid, []):
                q.put_nowait(dismissed)
            if uid:
                for other_sid, other_uid in self._connection_user_ids.items():
                    if other_uid == uid and other_sid != sid:
                        for q in self._connections.get(other_sid, []):
                            q.put_nowait(dismissed)

        elif action == "b":
            # Broadcast to all local connections
            n_data = message.get("n", {})
            mode_val = n_data.get("mode", NotificationMode.EPHEMERAL.value)
            notification = Notification(
                type=n_data.get("type", ""),
                id=UUID(n_data["id"]),
                payload={
                    k: v for k, v in n_data.items()
                    if k not in ("type", "id", "group", "mode", "created_at")
                },
                group=n_data.get("group"),
                mode=NotificationMode(mode_val),
            )
            if "created_at" in n_data:
                notification.created_at = n_data["created_at"]
            for queues in self._connections.values():
                for q in queues:
                    q.put_nowait(notification)


# Global singleton
notifications = NotificationService()


async def notify_session(
    nid: str,
    type: str,
    *,
    group: str | None = None,
    mode: NotificationMode = NotificationMode.QUEUED,
    **payload,
) -> Notification:
    """Convenience: send a notification to a session."""
    n = Notification(type=type, payload=payload, group=group, mode=mode)
    await notifications.send_to_session(nid, n)
    return n


async def notify_user(
    user_id: str,
    type: str,
    *,
    group: str | None = None,
    mode: NotificationMode = NotificationMode.QUEUED,
    **payload,
) -> Notification:
    """Convenience: send a notification to a user (all their sessions)."""
    n = Notification(type=type, payload=payload, group=group, mode=mode)
    await notifications.send_to_user(user_id, n)
    return n


async def notify_broadcast(type: str, *, group: str | None = None, **payload) -> Notification:
    """Convenience: broadcast an ephemeral notification to all active connections."""
    n = Notification(type=type, payload=payload, group=group, mode=NotificationMode.EPHEMERAL)
    await notifications.broadcast(n)
    return n


async def dismiss_session_group(nid: str, group: str) -> bool:
    """Dismiss the notification with *group* from the session queue."""
    return await notifications.dismiss(nid, None, group=group)


async def dismiss_user_group(user_id: str, group: str) -> bool:
    """Dismiss the notification with *group* from the user queue (all sessions)."""
    # Find any session belonging to this user to anchor the dismiss event push.
    anchor_nid: str | None = None
    for sid, uid in notifications._connection_user_ids.items():
        if uid == user_id:
            anchor_nid = sid
            break
    return await notifications.dismiss(anchor_nid or "", user_id, group=group)


def _ensure_nid(request) -> str:
    """Get or lazily create _nid in session."""
    nid = request.session.get("_nid")
    if not nid:
        nid = str(uuid4())
        request.session["_nid"] = nid
    return nid
