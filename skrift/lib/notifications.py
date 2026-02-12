"""Real-time notification service for Skrift CMS.

Provides session-scoped and user-scoped notifications delivered via
Server-Sent Events (SSE). Notifications persist in their in-memory
queues until explicitly dismissed via the DELETE endpoint.

Usage:
    from skrift.lib.notifications import notify_session, notify_user

    notify_session(nid, "generic", title="Page published", message="Now live.")
    notify_user(user_id, "generic", title="New comment", message="On your post.")
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


@dataclass
class Notification:
    type: str
    id: UUID = field(default_factory=uuid4)
    created_at: float = field(default_factory=time.monotonic)
    payload: dict[str, Any] = field(default_factory=dict)
    group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"type": self.type, "id": str(self.id), **self.payload}
        if self.group is not None:
            d["group"] = self.group
        return d


class NotificationService:
    """In-memory singleton managing notification queues and active SSE connections."""

    def __init__(self) -> None:
        self._session_queues: dict[str, dict[UUID, Notification]] = {}
        self._user_queues: dict[str, dict[UUID, Notification]] = {}
        self._connections: dict[str, list[asyncio.Queue]] = {}
        self._connection_user_ids: dict[str, str | None] = {}

    def _dismiss_by_group(
        self, queue: dict[UUID, Notification], group: str
    ) -> Notification | None:
        """Find and remove a notification with the given group key from *queue*."""
        for nid, notif in queue.items():
            if notif.group == group:
                del queue[nid]
                return notif
        return None

    def send_to_session(self, nid: str, notification: Notification) -> None:
        """Store a notification in the session queue and push to active connections."""
        if notification.group:
            session_q = self._session_queues.get(nid, {})
            old = self._dismiss_by_group(session_q, notification.group)
            if old is not None:
                dismissed = Notification(
                    type="dismissed", id=old.id, payload={}
                )
                for q in self._connections.get(nid, []):
                    q.put_nowait(dismissed)

        self._session_queues.setdefault(nid, {})[notification.id] = notification
        for q in self._connections.get(nid, []):
            q.put_nowait(notification)

    def send_to_user(self, user_id: str, notification: Notification) -> None:
        """Store a notification in the user queue and push to all connections for this user."""
        if notification.group:
            user_q = self._user_queues.get(user_id, {})
            old = self._dismiss_by_group(user_q, notification.group)
            if old is not None:
                dismissed = Notification(
                    type="dismissed", id=old.id, payload={}
                )
                for sid, uid in self._connection_user_ids.items():
                    if uid == user_id:
                        for q in self._connections.get(sid, []):
                            q.put_nowait(dismissed)

        self._user_queues.setdefault(user_id, {})[notification.id] = notification
        for nid, uid in self._connection_user_ids.items():
            if uid == user_id:
                for q in self._connections.get(nid, []):
                    q.put_nowait(notification)

    def broadcast(self, notification: Notification) -> None:
        """Push an ephemeral notification to ALL active connections. Not stored — won't replay on reconnect."""
        for queues in self._connections.values():
            for q in queues:
                q.put_nowait(notification)

    def dismiss(
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
        """
        dismissed_id: UUID | None = None

        session_q = self._session_queues.get(nid, {})
        if notification_id is not None:
            if notification_id in session_q:
                del session_q[notification_id]
                dismissed_id = notification_id
        elif group is not None:
            old = self._dismiss_by_group(session_q, group)
            if old is not None:
                dismissed_id = old.id

        if user_id:
            user_q = self._user_queues.get(user_id, {})
            if notification_id is not None:
                if notification_id in user_q:
                    del user_q[notification_id]
                    dismissed_id = notification_id
            elif group is not None:
                old = self._dismiss_by_group(user_q, group)
                if old is not None:
                    dismissed_id = old.id

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

        return dismissed_id is not None

    def get_queued(
        self, nid: str, user_id: str | None
    ) -> list[Notification]:
        """Return all queued notifications sorted oldest-first."""
        merged: dict[UUID, Notification] = {}
        merged.update(self._session_queues.get(nid, {}))
        if user_id:
            merged.update(self._user_queues.get(user_id, {}))
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


# Global singleton
notifications = NotificationService()


def notify_session(nid: str, type: str, *, group: str | None = None, **payload) -> Notification:
    """Convenience: send a notification to a session."""
    n = Notification(type=type, payload=payload, group=group)
    notifications.send_to_session(nid, n)
    return n


def notify_user(user_id: str, type: str, *, group: str | None = None, **payload) -> Notification:
    """Convenience: send a notification to a user (all their sessions)."""
    n = Notification(type=type, payload=payload, group=group)
    notifications.send_to_user(user_id, n)
    return n


def notify_broadcast(type: str, *, group: str | None = None, **payload) -> Notification:
    """Convenience: broadcast an ephemeral notification to all active connections."""
    n = Notification(type=type, payload=payload, group=group)
    notifications.broadcast(n)
    return n


def dismiss_session_group(nid: str, group: str) -> bool:
    """Dismiss the notification with *group* from the session queue."""
    return notifications.dismiss(nid, None, group=group)


def dismiss_user_group(user_id: str, group: str) -> bool:
    """Dismiss the notification with *group* from the user queue (all sessions)."""
    # Find any session belonging to this user to anchor the dismiss event push.
    anchor_nid: str | None = None
    for sid, uid in notifications._connection_user_ids.items():
        if uid == user_id:
            anchor_nid = sid
            break
    return notifications.dismiss(anchor_nid or "", user_id, group=group)


def _ensure_nid(request) -> str:
    """Get or lazily create _nid in session."""
    nid = request.session.get("_nid")
    if not nid:
        nid = str(uuid4())
        request.session["_nid"] = nid
    return nid
