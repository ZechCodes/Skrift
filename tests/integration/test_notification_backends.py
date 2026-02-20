"""Cross-replica integration tests for Redis and PgNotify notification backends.

Each test creates two independent NotificationService+backend pairs (simulating
two replicas) connected to the same shared infrastructure, then verifies pub/sub
fanout and DB storage work correctly across the pair.

Requires running PostgreSQL and Redis — see compose.yml.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from skrift.lib.notifications import NotDismissibleError, Notification, NotificationMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def drain_queue(q: asyncio.Queue, *, timeout: float = 1.0) -> list:
    """Collect all items from an asyncio.Queue within a timeout window."""
    items = []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            item = await asyncio.wait_for(q.get(), timeout=remaining)
            items.append(item)
        except asyncio.TimeoutError:
            break
    return items


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestNotificationBackends:
    """Cross-replica notification backend tests (parametrized over Redis + PgNotify)."""

    async def test_db_storage_session_scoped(self, backend_pair):
        """Send on A → get_queued_multi from B's backend confirms DB storage."""
        (svc_a, _), (_, backend_b) = backend_pair

        n = Notification(type="generic", payload={"title": "Hello"})
        await svc_a.send_to_session("sess-1", n)

        queued = await backend_b.get_queued_multi(["session:sess-1"])
        assert len(queued) == 1
        assert queued[0].id == n.id
        assert queued[0].payload == {"title": "Hello"}

    async def test_db_storage_user_scoped(self, backend_pair):
        """send_to_user on A → get_queued_multi from B confirms user-scoped DB storage."""
        (svc_a, _), (_, backend_b) = backend_pair

        n = Notification(type="generic", payload={"title": "User msg"})
        await svc_a.send_to_user("user-42", n)

        queued = await backend_b.get_queued_multi(["user:user-42"])
        assert len(queued) == 1
        assert queued[0].id == n.id
        assert queued[0].payload == {"title": "User msg"}

    async def test_cross_replica_session_notification(self, backend_pair):
        """Register connection on B for session → send on A → B's queue receives it."""
        (svc_a, _), (svc_b, _) = backend_pair

        q = await svc_b.register_connection("sess-1", None)

        n = Notification(type="generic", payload={"title": "Cross-replica"})
        await svc_a.send_to_session("sess-1", n)

        items = await drain_queue(q)
        assert len(items) == 1
        assert items[0].id == n.id
        assert items[0].type == "generic"

    async def test_cross_replica_user_notification(self, backend_pair):
        """Register connection on B for user-42 → send_to_user on A → B receives it."""
        (svc_a, _), (svc_b, _) = backend_pair

        q = await svc_b.register_connection("sess-b", "user-42")

        n = Notification(type="generic", payload={"title": "User cross-replica"})
        await svc_a.send_to_user("user-42", n)

        items = await drain_queue(q)
        assert len(items) == 1
        assert items[0].id == n.id

    async def test_cross_replica_broadcast(self, backend_pair):
        """Register two connections on B → broadcast on A → both B queues receive it."""
        (svc_a, _), (svc_b, _) = backend_pair

        q1 = await svc_b.register_connection("sess-b1", None)
        q2 = await svc_b.register_connection("sess-b2", "user-1")

        n = Notification(type="alert", payload={"msg": "broadcast"})
        await svc_a.broadcast(n)

        items1 = await drain_queue(q1)
        items2 = await drain_queue(q2)
        assert len(items1) == 1
        assert len(items2) == 1
        assert items1[0].id == n.id
        assert items2[0].id == n.id

    async def test_cross_replica_dismiss(self, backend_pair):
        """Store on A → register on B → dismiss on A → B receives dismissed + DB cleared."""
        (svc_a, backend_a), (svc_b, _) = backend_pair

        n = Notification(type="generic", payload={"title": "Will dismiss"})
        await svc_a.send_to_session("sess-1", n)

        # Register on B and drain any pub/sub messages from the send
        q = await svc_b.register_connection("sess-1", None)
        await drain_queue(q, timeout=0.3)

        # Dismiss on A
        result = await svc_a.dismiss("sess-1", None, n.id)
        assert result is True

        # B should receive the dismissed event via pub/sub
        items = await drain_queue(q)
        assert len(items) == 1
        assert items[0].type == "dismissed"
        assert items[0].id == n.id

        # DB should be empty
        queued = await backend_a.get_queued_multi(["session:sess-1"])
        assert len(queued) == 0

    async def test_group_replacement_across_replicas(self, backend_pair):
        """Send n1 on A → replace with n2 (same group) on A → B sees n2, DB has only n2."""
        (svc_a, backend_a), (svc_b, _) = backend_pair

        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc_a.send_to_session("sess-1", n1)

        # Register on B and drain any messages from n1
        q = await svc_b.register_connection("sess-1", None)
        await drain_queue(q, timeout=0.3)

        # Replace with n2 (same group)
        n2 = Notification(type="generic", group="deploy", payload={"step": "2"})
        await svc_a.send_to_session("sess-1", n2)

        # B should receive n2 via pub/sub
        items = await drain_queue(q)
        received_ids = {item.id for item in items}
        assert n2.id in received_ids

        # DB should have only n2
        queued = await backend_a.get_queued_multi(["session:sess-1"])
        assert len(queued) == 1
        assert queued[0].id == n2.id

    async def test_cleanup_doesnt_error(self, backend_pair):
        """_delete_old_notifications() is a no-op on fresh data but must not raise."""
        (_, backend_a), _ = backend_pair
        await backend_a._delete_old_notifications()

    async def test_timeseries_db_storage_and_get_since(self, backend_pair):
        """Timeseries notifications are stored and returned by get_since_multi."""
        (svc_a, _), (_, backend_b) = backend_pair

        before = time.time() - 1
        n = Notification(
            type="generic", mode=NotificationMode.TIMESERIES,
            payload={"msg": "activity"},
        )
        await svc_a.send_to_session("sess-1", n)

        since = await backend_b.get_since_multi(["session:sess-1"], before)
        assert len(since) == 1
        assert since[0].id == n.id
        assert since[0].mode == NotificationMode.TIMESERIES

    async def test_get_queued_excludes_timeseries(self, backend_pair):
        """get_queued_multi should not return timeseries notifications."""
        (svc_a, _), (_, backend_b) = backend_pair

        n = Notification(
            type="generic", mode=NotificationMode.TIMESERIES,
            payload={"msg": "ts"},
        )
        await svc_a.send_to_session("sess-1", n)

        queued = await backend_b.get_queued_multi(["session:sess-1"])
        assert len(queued) == 0

    async def test_ephemeral_not_stored_in_db(self, backend_pair):
        """Ephemeral notifications should not appear in DB storage."""
        (svc_a, _), (_, backend_b) = backend_pair

        n = Notification(
            type="generic", mode=NotificationMode.EPHEMERAL,
            payload={"msg": "gone"},
        )
        await svc_a.send_to_session("sess-1", n)

        queued = await backend_b.get_queued_multi(["session:sess-1"])
        since = await backend_b.get_since_multi(["session:sess-1"], 0.0)
        assert len(queued) == 0
        assert len(since) == 0

    async def test_timeseries_dismiss_raises_in_db(self, backend_pair):
        """Dismissing a timeseries notification via DB backend raises NotDismissibleError."""
        (svc_a, _), _ = backend_pair

        n = Notification(
            type="generic", mode=NotificationMode.TIMESERIES,
            payload={"msg": "permanent"},
        )
        await svc_a.send_to_session("sess-1", n)

        with pytest.raises(NotDismissibleError):
            await svc_a.dismiss("sess-1", None, n.id)

    async def test_persistent_subscription_crud(self, backend_pair):
        """Test add/get/remove persistent subscriptions in DB."""
        (_, backend_a), _ = backend_pair

        # Initially empty
        subs = await backend_a.get_persistent_subscriptions("user:alice")
        assert subs == []

        # Add
        await backend_a.add_subscription("user:alice", "blog:tech")
        subs = await backend_a.get_persistent_subscriptions("user:alice")
        assert subs == ["blog:tech"]

        # Idempotent add
        await backend_a.add_subscription("user:alice", "blog:tech")
        subs = await backend_a.get_persistent_subscriptions("user:alice")
        assert subs == ["blog:tech"]

        # Add another
        await backend_a.add_subscription("user:alice", "blog:news")
        subs = await backend_a.get_persistent_subscriptions("user:alice")
        assert set(subs) == {"blog:tech", "blog:news"}

        # Remove
        await backend_a.remove_subscription("user:alice", "blog:tech")
        subs = await backend_a.get_persistent_subscriptions("user:alice")
        assert subs == ["blog:news"]

    async def test_cross_replica_source_subscription(self, backend_pair):
        """Subscribe user to source on A, publish on A, verify B receives via graph."""
        (svc_a, _), (svc_b, _) = backend_pair

        # Register connection on B for user-alice
        q = await svc_b.register_connection("sess-b", "alice")

        # Subscribe user:alice to blog:tech on B (persistent + local graph)
        await svc_b.subscribe("user:alice", "blog:tech")

        # Publish to blog:tech on A (ephemeral so it only goes via pub/sub)
        n = Notification(type="new_post", mode=NotificationMode.EPHEMERAL, payload={"title": "Hello"})
        await svc_a.send("blog:tech", n)

        # B should receive it via cross-replica fanout + graph resolution
        items = await drain_queue(q)
        assert len(items) == 1
        assert items[0].id == n.id

    async def test_self_echo_prevention(self, backend_pair):
        """Messages from the same publisher_id should not be delivered to local listeners."""
        (svc_a, _), _ = backend_pair

        q = await svc_a.register_connection("sess-1", None)

        # Send a notification — it should be delivered locally but remote
        # handler should skip self-echo
        n = Notification(type="test", mode=NotificationMode.EPHEMERAL)
        await svc_a.send_to_session("sess-1", n)

        items = await drain_queue(q, timeout=0.5)
        # Should get exactly 1 (from local push), not 2 (local + remote echo)
        assert len(items) == 1
        assert items[0].id == n.id
