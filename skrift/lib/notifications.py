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

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": str(self.id), **self.payload}


class NotificationService:
    """In-memory singleton managing notification queues and active SSE connections."""

    def __init__(self) -> None:
        self._session_queues: dict[str, dict[UUID, Notification]] = {}
        self._user_queues: dict[str, dict[UUID, Notification]] = {}
        self._connections: dict[str, list[asyncio.Queue]] = {}
        self._connection_user_ids: dict[str, str | None] = {}

    def send_to_session(self, nid: str, notification: Notification) -> None:
        """Store a notification in the session queue and push to active connections."""
        self._session_queues.setdefault(nid, {})[notification.id] = notification
        for q in self._connections.get(nid, []):
            q.put_nowait(notification)

    def send_to_user(self, user_id: str, notification: Notification) -> None:
        """Store a notification in the user queue and push to all connections for this user."""
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
        self, nid: str, user_id: str | None, notification_id: UUID
    ) -> bool:
        """Remove a notification from queues and send ephemeral dismissed event.

        Returns True if the notification was found and removed.
        """
        found = False

        session_q = self._session_queues.get(nid, {})
        if notification_id in session_q:
            del session_q[notification_id]
            found = True

        if user_id:
            user_q = self._user_queues.get(user_id, {})
            if notification_id in user_q:
                del user_q[notification_id]
                found = True

        if found:
            dismissed = Notification(
                type="dismissed", id=notification_id, payload={}
            )
            # Push to this session's connections
            for q in self._connections.get(nid, []):
                q.put_nowait(dismissed)
            # Push to other sessions of the same user
            if user_id:
                for other_nid, uid in self._connection_user_ids.items():
                    if uid == user_id and other_nid != nid:
                        for q in self._connections.get(other_nid, []):
                            q.put_nowait(dismissed)

        return found

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


def notify_session(nid: str, type: str, **payload) -> Notification:
    """Convenience: send a notification to a session."""
    n = Notification(type=type, payload=payload)
    notifications.send_to_session(nid, n)
    return n


def notify_user(user_id: str, type: str, **payload) -> Notification:
    """Convenience: send a notification to a user (all their sessions)."""
    n = Notification(type=type, payload=payload)
    notifications.send_to_user(user_id, n)
    return n


def notify_broadcast(type: str, **payload) -> Notification:
    """Convenience: broadcast an ephemeral notification to all active connections."""
    n = Notification(type=type, payload=payload)
    notifications.broadcast(n)
    return n


def _ensure_nid(request) -> str:
    """Get or lazily create _nid in session."""
    nid = request.session.get("_nid")
    if not nid:
        nid = str(uuid4())
        request.session["_nid"] = nid
    return nid
