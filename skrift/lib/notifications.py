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

from skrift.lib.hooks import hooks, NOTIFICATION_PRE_SEND, NOTIFICATION_SENT, NOTIFICATION_DISMISSED

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

    @classmethod
    def dismissed(cls, notification_id: UUID) -> Notification:
        """Create a dismissed notification event."""
        return cls(type="dismissed", id=notification_id, payload={})


class ConnectionManager:
    """Manages SSE connection queues and local notification delivery.

    Per-replica — each process has its own set of active connections.
    """

    def __init__(self) -> None:
        self._connections: dict[str, list[asyncio.Queue]] = {}
        self._connection_user_ids: dict[str, str | None] = {}

    def register(self, nid: str, user_id: str | None) -> asyncio.Queue:
        """Register a new SSE connection and return its queue."""
        q: asyncio.Queue = asyncio.Queue()
        self._connections.setdefault(nid, []).append(q)
        self._connection_user_ids[nid] = user_id
        return q

    def unregister(self, nid: str, q: asyncio.Queue) -> None:
        """Remove a connection on disconnect."""
        conns = self._connections.get(nid, [])
        try:
            conns.remove(q)
        except ValueError:
            pass
        if not conns:
            self._connections.pop(nid, None)
            self._connection_user_ids.pop(nid, None)

    def push_to_session(self, nid: str, notification: Notification) -> None:
        """Push a notification to all connections for a session."""
        for q in self._connections.get(nid, []):
            q.put_nowait(notification)

    def push_to_user(self, user_id: str, notification: Notification) -> None:
        """Push a notification to all connections for a user."""
        for sid, uid in self._connection_user_ids.items():
            if uid == user_id:
                for q in self._connections.get(sid, []):
                    q.put_nowait(notification)

    def push_to_user_except(self, user_id: str, exclude_nid: str, notification: Notification) -> None:
        """Push to all connections for a user except the given session."""
        for sid, uid in self._connection_user_ids.items():
            if uid == user_id and sid != exclude_nid:
                for q in self._connections.get(sid, []):
                    q.put_nowait(notification)

    def push_to_all(self, notification: Notification) -> None:
        """Push a notification to all active connections."""
        for queues in self._connections.values():
            for q in queues:
                q.put_nowait(notification)

    def push_to_scope(self, scope: str, scope_id: str, notification: Notification) -> None:
        """Push a notification to the appropriate scope."""
        if scope == "session":
            self.push_to_session(scope_id, notification)
        elif scope == "user":
            self.push_to_user(scope_id, notification)

    def find_session_for_user(self, user_id: str) -> str | None:
        """Find any session ID belonging to the given user."""
        for sid, uid in self._connection_user_ids.items():
            if uid == user_id:
                return sid
        return None


class NotificationService:
    """Manages notification storage, delivery, and cross-replica fanout.

    Storage and cross-replica fanout are delegated to a pluggable backend.
    Local SSE connection management is delegated to ConnectionManager.
    """

    def __init__(self) -> None:
        self._cm = ConnectionManager()
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

    async def _send(self, scope: str, scope_id: str, notification: Notification) -> None:
        """Shared implementation for send_to_session and send_to_user."""
        notification = await hooks.apply_filters(NOTIFICATION_PRE_SEND, notification, scope, scope_id)
        if notification is None:
            return

        backend = self._get_backend()

        if notification.mode != NotificationMode.EPHEMERAL:
            old_id = await backend.store(scope, scope_id, notification)
            if old_id is not None:
                self._cm.push_to_scope(scope, scope_id, Notification.dismissed(old_id))

        self._cm.push_to_scope(scope, scope_id, notification)

        await backend.publish({
            "a": "s", "sc": scope, "sid": scope_id,
            "n": notification.to_dict(),
        })

        await hooks.do_action(NOTIFICATION_SENT, notification, scope, scope_id)

    async def send_to_session(self, nid: str, notification: Notification) -> None:
        """Store a notification in the session queue and push to active connections."""
        await self._send("session", nid, notification)

    async def send_to_user(self, user_id: str, notification: Notification) -> None:
        """Store a notification in the user queue and push to all connections for this user."""
        await self._send("user", user_id, notification)

    async def broadcast(self, notification: Notification) -> None:
        """Push an ephemeral notification to ALL active connections. Not stored — won't replay on reconnect."""
        notification = await hooks.apply_filters(NOTIFICATION_PRE_SEND, notification, "broadcast", None)
        if notification is None:
            return

        self._cm.push_to_all(notification)

        await self._get_backend().publish({
            "a": "b",
            "n": notification.to_dict(),
        })

        await hooks.do_action(NOTIFICATION_SENT, notification, "broadcast", None)

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
            dismissed = Notification.dismissed(dismissed_id)
            self._cm.push_to_session(nid, dismissed)
            if user_id:
                self._cm.push_to_user_except(user_id, nid, dismissed)

            await backend.publish({
                "a": "d", "sc": "session", "sid": nid,
                "uid": user_id,
                "nid": str(dismissed_id),
            })

            await hooks.do_action(NOTIFICATION_DISMISSED, dismissed_id)

        return dismissed_id is not None

    async def get_queued(
        self, nid: str, user_id: str | None
    ) -> list[Notification]:
        """Return all queued notifications sorted oldest-first."""
        backend = self._get_backend()
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
        return self._cm.register(nid, user_id)

    def unregister_connection(self, nid: str, q: asyncio.Queue) -> None:
        """Remove a connection on disconnect."""
        self._cm.unregister(nid, q)

    async def _handle_remote(self, message: dict) -> None:
        """Process a message received from another replica via pub/sub."""
        action = message.get("a")

        if action == "s":
            # Send notification — push to matching local connections
            scope = message.get("sc")
            scope_id = message.get("sid")
            notification = _notification_from_wire(message.get("n", {}))

            self._cm.push_to_scope(scope, scope_id, notification)

        elif action == "d":
            # Dismiss
            dismissed_id = UUID(message["nid"])
            dismissed = Notification.dismissed(dismissed_id)
            sid = message.get("sid", "")
            uid = message.get("uid")

            self._cm.push_to_session(sid, dismissed)
            if uid:
                self._cm.push_to_user_except(uid, sid, dismissed)

        elif action == "b":
            # Broadcast to all local connections
            notification = _notification_from_wire(
                message.get("n", {}),
                default_mode=NotificationMode.EPHEMERAL,
            )
            self._cm.push_to_all(notification)


def _notification_from_wire(
    n_data: dict, *, default_mode: NotificationMode = NotificationMode.QUEUED
) -> Notification:
    """Deserialize a notification from a cross-replica wire message."""
    mode_val = n_data.get("mode", default_mode.value)
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
    return notification


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
    anchor_nid = notifications._cm.find_session_for_user(user_id)
    return await notifications.dismiss(anchor_nid or "", user_id, group=group)


def _ensure_nid(request) -> str:
    """Get or lazily create _nid in session."""
    nid = request.session.get("_nid")
    if not nid:
        nid = str(uuid4())
        request.session["_nid"] = nid
    return nid
