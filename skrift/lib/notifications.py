"""Real-time notification service for Skrift CMS.

Provides source-based notifications delivered via Server-Sent Events (SSE).
Sources form a DAG: ``global`` -> ``user:alice`` -> ``session:abc``.
Custom sources (e.g., ``blog:tech``) can be declared; users subscribe to them.

The backend is pluggable via app.yaml — see notification_backends.py.

Usage:
    from skrift.lib.notifications import notify_session, notify_user, notify_source

    await notify_session(nid, "generic", title="Page published", message="Now live.")
    await notify_user(user_id, "generic", title="New comment", message="On your post.")
    await notify_source("blog:tech", "new_post", title="New post published")
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
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


def _parse_source_key(source_key: str) -> tuple[str, str | None]:
    """Derive ``(scope, scope_id)`` from a source_key for hook backwards compat.

    - ``"session:abc"``  -> ``("session", "abc")``
    - ``"user:alice"``   -> ``("user", "alice")``
    - ``"global"``       -> ``("broadcast", None)``
    - ``"blog:tech"``    -> ``("blog", "tech")``
    """
    if source_key == "global":
        return ("broadcast", None)
    if ":" in source_key:
        scope, scope_id = source_key.split(":", 1)
        return (scope, scope_id)
    return (source_key, None)


class SourceRegistry:
    """In-memory subscription DAG and listener registry.

    Replaces ``ConnectionManager``. Pure synchronous data structure (no ``await``),
    safe under single-threaded asyncio.

    The graph has two edge directions:
    - ``_subscriptions[child] = {parents}`` — upward edges (child subscribes to parent)
    - ``_subscribers[parent] = {children}`` — downward edges (reverse index)

    Publishing to a source cascades **downstream** (toward listeners).
    """

    def __init__(self) -> None:
        self._listeners: dict[str, set[asyncio.Queue]] = {}
        self._subscriptions: dict[str, set[str]] = {}  # child -> {parents}
        self._subscribers: dict[str, set[str]] = {}    # parent -> {children}

    def add_listener(self, source_key: str, queue: asyncio.Queue) -> None:
        self._listeners.setdefault(source_key, set()).add(queue)

    def remove_listener(self, source_key: str, queue: asyncio.Queue) -> None:
        listeners = self._listeners.get(source_key)
        if listeners:
            listeners.discard(queue)
            if not listeners:
                del self._listeners[source_key]

    def has_listeners(self, source_key: str) -> bool:
        return bool(self._listeners.get(source_key))

    def subscribe(self, child: str, parent: str) -> None:
        """Add an edge: child subscribes to parent (idempotent)."""
        self._subscriptions.setdefault(child, set()).add(parent)
        self._subscribers.setdefault(parent, set()).add(child)

    def unsubscribe(self, child: str, parent: str) -> None:
        """Remove an edge (idempotent)."""
        subs = self._subscriptions.get(child)
        if subs:
            subs.discard(parent)
            if not subs:
                del self._subscriptions[child]
        rev = self._subscribers.get(parent)
        if rev:
            rev.discard(child)
            if not rev:
                del self._subscribers[parent]

    def unsubscribe_all(self, child: str) -> None:
        """Remove all upstream edges for a child (session teardown)."""
        parents = self._subscriptions.pop(child, set())
        for parent in parents:
            rev = self._subscribers.get(parent)
            if rev:
                rev.discard(child)
                if not rev:
                    del self._subscribers[parent]

    def resolve_downstream(self, source_key: str) -> set[str]:
        """BFS following ``_subscribers`` edges — find all downstream targets."""
        visited: set[str] = set()
        queue: deque[str] = deque([source_key])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            for child in self._subscribers.get(node, ()):
                if child not in visited:
                    queue.append(child)
        return visited

    def resolve_upstream(self, source_key: str) -> set[str]:
        """BFS following ``_subscriptions`` edges — find all upstream sources."""
        visited: set[str] = set()
        queue: deque[str] = deque([source_key])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            for parent in self._subscriptions.get(node, ()):
                if parent not in visited:
                    queue.append(parent)
        return visited

    def push(self, source_key: str, notification: Notification) -> None:
        """Resolve downstream targets and push to all listeners on those targets."""
        targets = self.resolve_downstream(source_key)
        for target in targets:
            for q in self._listeners.get(target, ()):
                q.put_nowait(notification)


class NotificationService:
    """Manages notification storage, delivery, and cross-replica fanout.

    Storage and cross-replica fanout are delegated to a pluggable backend.
    Local SSE connection management is delegated to SourceRegistry.
    """

    def __init__(self) -> None:
        self._registry = SourceRegistry()
        self._backend: NotificationBackend | None = None
        self._publisher_id: str = str(uuid4())
        self._loaded_user_subs: set[str] = set()

    def set_backend(self, backend: NotificationBackend) -> None:
        self._backend = backend
        backend.on_remote_message(self._handle_remote)

    def _get_backend(self) -> NotificationBackend:
        if self._backend is None:
            from skrift.lib.notification_backends import InMemoryBackend
            self._backend = InMemoryBackend()
            self._backend.on_remote_message(self._handle_remote)
        return self._backend

    async def _send(self, source_key: str, notification: Notification) -> None:
        """Unified send: store, push locally via graph, fanout to replicas."""
        scope, scope_id = _parse_source_key(source_key)

        notification = await hooks.apply_filters(NOTIFICATION_PRE_SEND, notification, scope, scope_id)
        if notification is None:
            return

        backend = self._get_backend()

        if notification.mode != NotificationMode.EPHEMERAL:
            old_id = await backend.store(source_key, notification)
            if old_id is not None:
                self._registry.push(source_key, Notification.dismissed(old_id))

        self._registry.push(source_key, notification)

        await backend.publish({
            "a": "s", "sk": source_key, "pid": self._publisher_id,
            "n": notification.to_dict(),
        })

        await hooks.do_action(NOTIFICATION_SENT, notification, scope, scope_id)

    async def send_to_session(self, nid: str, notification: Notification) -> None:
        """Store a notification in the session queue and push to active connections."""
        await self._send(f"session:{nid}", notification)

    async def send_to_user(self, user_id: str, notification: Notification) -> None:
        """Store a notification in the user queue and push to all connections for this user."""
        await self._send(f"user:{user_id}", notification)

    async def broadcast(self, notification: Notification) -> None:
        """Push an ephemeral notification to ALL active connections. Not stored."""
        notification = await hooks.apply_filters(NOTIFICATION_PRE_SEND, notification, "broadcast", None)
        if notification is None:
            return

        self._registry.push("global", notification)

        await self._get_backend().publish({
            "a": "s", "sk": "global", "pid": self._publisher_id,
            "n": notification.to_dict(),
        })

        await hooks.do_action(NOTIFICATION_SENT, notification, "broadcast", None)

    async def send(self, source_key: str, notification: Notification) -> None:
        """Publish to any source key."""
        await self._send(source_key, notification)

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
        dismiss_source_key: str | None = None

        if notification_id is not None:
            mode = await backend.get_mode(notification_id)
            if mode == NotificationMode.TIMESERIES.value:
                raise NotDismissibleError(
                    f"Cannot dismiss timeseries notification {notification_id}"
                )
            dismiss_source_key = await backend.remove(notification_id)
            dismissed_id = notification_id
        elif group is not None:
            for key in self._storage_keys_for(nid, user_id):
                old_id = await backend.remove_by_group(key, group)
                if old_id:
                    dismissed_id = old_id
                    dismiss_source_key = key

        if dismissed_id is not None:
            dismissed = Notification.dismissed(dismissed_id)

            # Push through the graph — the source_key cascading handles delivery
            # to all relevant sessions
            if dismiss_source_key:
                self._registry.push(dismiss_source_key, dismissed)
            else:
                # Fallback: push to session and user directly
                self._registry.push(f"session:{nid}", dismissed)
                if user_id:
                    self._registry.push(f"user:{user_id}", dismissed)

            await backend.publish({
                "a": "d", "sk": dismiss_source_key or f"session:{nid}",
                "pid": self._publisher_id,
                "nid": str(dismissed_id),
            })

            await hooks.do_action(NOTIFICATION_DISMISSED, dismissed_id)

        return dismissed_id is not None

    async def get_queued(
        self, nid: str, user_id: str | None
    ) -> list[Notification]:
        """Return all queued notifications sorted oldest-first."""
        keys = self._storage_keys_for(nid, user_id)
        return await self._get_backend().get_queued_multi(keys)

    async def get_since(
        self, nid: str, user_id: str | None, since: float
    ) -> list[Notification]:
        """Return timeseries notifications created after *since* timestamp."""
        keys = self._storage_keys_for(nid, user_id)
        return await self._get_backend().get_since_multi(keys, since)

    def _storage_keys_for(self, nid: str, user_id: str | None) -> list[str]:
        """Compute the set of source_keys to query for stored notifications.

        Uses the upstream graph from ``session:{nid}`` but excludes ``global``
        (ephemeral-only source).
        """
        session_key = f"session:{nid}"
        upstream = self._registry.resolve_upstream(session_key)
        # Always include the direct keys even without graph edges
        upstream.add(session_key)
        if user_id:
            upstream.add(f"user:{user_id}")
        upstream.discard("global")
        return list(upstream)

    async def register_connection(
        self, nid: str, user_id: str | None
    ) -> asyncio.Queue:
        """Register a new SSE connection and return its queue.

        Sets up ephemeral graph edges and loads persistent subscriptions.
        """
        session_key = f"session:{nid}"
        q: asyncio.Queue = asyncio.Queue()

        # Ephemeral edges: session -> global, session -> user, user -> global
        self._registry.subscribe(session_key, "global")
        if user_id:
            user_key = f"user:{user_id}"
            self._registry.subscribe(session_key, user_key)
            self._registry.subscribe(user_key, "global")

            # Load persistent subscriptions for this user (once per process)
            if user_key not in self._loaded_user_subs:
                self._loaded_user_subs.add(user_key)
                backend = self._get_backend()
                persistent = await backend.get_persistent_subscriptions(user_key)
                for source in persistent:
                    self._registry.subscribe(user_key, source)

        self._registry.add_listener(session_key, q)
        return q

    def unregister_connection(self, nid: str, q: asyncio.Queue) -> None:
        """Remove a connection on disconnect."""
        session_key = f"session:{nid}"
        self._registry.remove_listener(session_key, q)

        # If no more listeners on this session, tear down ephemeral edges
        if not self._registry.has_listeners(session_key):
            self._registry.unsubscribe_all(session_key)

    async def subscribe(self, subscriber_key: str, source_key: str) -> None:
        """Add a persistent subscription (DB + local graph)."""
        await self._get_backend().add_subscription(subscriber_key, source_key)
        self._registry.subscribe(subscriber_key, source_key)

    async def unsubscribe(self, subscriber_key: str, source_key: str) -> None:
        """Remove a persistent subscription (DB + local graph)."""
        await self._get_backend().remove_subscription(subscriber_key, source_key)
        self._registry.unsubscribe(subscriber_key, source_key)

    async def _handle_remote(self, message: dict) -> None:
        """Process a message received from another replica via pub/sub."""
        # Self-echo prevention
        if message.get("pid") == self._publisher_id:
            return

        action = message.get("a")
        source_key = message.get("sk", "")

        if action == "s":
            notification = _notification_from_wire(message.get("n", {}))
            self._registry.push(source_key, notification)

        elif action == "d":
            dismissed_id = UUID(message["nid"])
            dismissed = Notification.dismissed(dismissed_id)
            self._registry.push(source_key, dismissed)


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


async def notify_source(
    source_key: str,
    type: str,
    *,
    group: str | None = None,
    mode: NotificationMode = NotificationMode.QUEUED,
    **payload,
) -> Notification:
    """Convenience: publish a notification to any source key."""
    n = Notification(type=type, payload=payload, group=group, mode=mode)
    await notifications.send(source_key, n)
    return n


async def subscribe_source(subscriber_key: str, source_key: str) -> None:
    """Add a persistent subscription."""
    await notifications.subscribe(subscriber_key, source_key)


async def unsubscribe_source(subscriber_key: str, source_key: str) -> None:
    """Remove a persistent subscription."""
    await notifications.unsubscribe(subscriber_key, source_key)


async def dismiss_session_group(nid: str, group: str) -> bool:
    """Dismiss the notification with *group* from the session queue."""
    return await notifications.dismiss(nid, None, group=group)


async def dismiss_user_group(user_id: str, group: str) -> bool:
    """Dismiss the notification with *group* from the user queue (all sessions)."""
    # Find any session subscribed to this user to anchor the dismiss event push.
    user_key = f"user:{user_id}"
    # Look for a session that subscribes to this user
    children = notifications._registry._subscribers.get(user_key, set())
    anchor_nid = ""
    for child in children:
        if child.startswith("session:"):
            anchor_nid = child.removeprefix("session:")
            break
    return await notifications.dismiss(anchor_nid, user_id, group=group)


def _ensure_nid(request) -> str:
    """Get or lazily create _nid in session."""
    nid = request.session.get("_nid")
    if not nid:
        nid = str(uuid4())
        request.session["_nid"] = nid
    return nid
