"""Tests for bounded memory usage in the notification subsystem.

Covers the in-memory backend's TTL-based expiry, the bounded SSE delivery
queue, and full teardown of registry edges when a user's last connection
disconnects.
"""

import asyncio
import time

import pytest

from skrift.lib.notification_backends import (
    CLEANUP_INTERVAL_SECONDS,
    QUEUED_TTL_HOURS,
    TIMESERIES_TTL_DAYS,
    InMemoryBackend,
)
from skrift.notifications import (
    SSE_QUEUE_MAXSIZE,
    Notification,
    NotificationMode,
    NotificationService,
    SourceRegistry,
)


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def svc():
    return NotificationService()


def _aged(mode: NotificationMode, age_seconds: float, **payload) -> Notification:
    """Build a notification whose created_at is *age_seconds* in the past."""
    return Notification(
        type="aged",
        created_at=time.time() - age_seconds,
        mode=mode,
        payload=payload,
    )


# ===========================================================================
# InMemoryBackend TTL expiry
# ===========================================================================


class TestInMemoryBackendExpiry:
    @pytest.mark.asyncio
    async def test_expires_old_queued_notifications(self, backend):
        stale = _aged(NotificationMode.QUEUED, QUEUED_TTL_HOURS * 3600 + 60)
        fresh = _aged(NotificationMode.QUEUED, 60)
        await backend.store("session:s1", stale)
        await backend.store("session:s1", fresh)

        await backend._delete_old_notifications()

        remaining = await backend.get_queued_multi(["session:s1"])
        assert [n.id for n in remaining] == [fresh.id]

    @pytest.mark.asyncio
    async def test_expires_old_timeseries_notifications(self, backend):
        stale = _aged(NotificationMode.TIMESERIES, TIMESERIES_TTL_DAYS * 86400 + 60)
        fresh = _aged(NotificationMode.TIMESERIES, 60)
        await backend.store("user:alice", stale)
        await backend.store("user:alice", fresh)

        await backend._delete_old_notifications()

        remaining = await backend.get_since_multi(["user:alice"], 0)
        assert [n.id for n in remaining] == [fresh.id]

    @pytest.mark.asyncio
    async def test_keeps_timeseries_younger_than_queued_ttl(self, backend):
        """Timeseries records live longer than queued ones."""
        older_than_queued_ttl = _aged(
            NotificationMode.TIMESERIES, QUEUED_TTL_HOURS * 3600 + 60
        )
        await backend.store("user:alice", older_than_queued_ttl)

        await backend._delete_old_notifications()

        remaining = await backend.get_since_multi(["user:alice"], 0)
        assert [n.id for n in remaining] == [older_than_queued_ttl.id]

    @pytest.mark.asyncio
    async def test_empty_source_keys_are_removed(self, backend):
        stale = _aged(NotificationMode.QUEUED, QUEUED_TTL_HOURS * 3600 + 60)
        await backend.store("session:s1", stale)

        await backend._delete_old_notifications()

        assert backend._queues == {}

    @pytest.mark.asyncio
    async def test_dismissed_records_for_expired_notifications_are_dropped(self, backend):
        stale = _aged(NotificationMode.QUEUED, QUEUED_TTL_HOURS * 3600 + 60)
        await backend.store("session:s1", stale)
        assert await backend.dismiss_for_subscriber("user:alice", stale.id) == "session:s1"

        await backend._delete_old_notifications()
        await backend.cleanup_dismissed()

        assert backend._dismissed == {}

    @pytest.mark.asyncio
    async def test_start_schedules_cleanup_and_stop_cancels_it(self, backend, monkeypatch):
        monkeypatch.setattr(InMemoryBackend, "_cleanup_interval_seconds", 0.01)
        stale = _aged(NotificationMode.QUEUED, QUEUED_TTL_HOURS * 3600 + 60)
        await backend.store("session:s1", stale)

        await backend.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not backend._queues:
                    break
            assert backend._queues == {}
        finally:
            await backend.stop()

        assert backend._cleanup_task is None or backend._cleanup_task.done()

    @pytest.mark.asyncio
    async def test_cleanup_loop_survives_backend_errors(self, backend, monkeypatch):
        """A failing sweep is logged and the loop keeps running."""
        monkeypatch.setattr(InMemoryBackend, "_cleanup_interval_seconds", 0.01)
        sweeps = []

        async def failing_sweep():
            sweeps.append(1)
            raise RuntimeError("boom")

        monkeypatch.setattr(backend, "_delete_old_notifications", failing_sweep)

        await backend.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(sweeps) >= 2:
                    break
            assert len(sweeps) >= 2
        finally:
            await backend.stop()

    def test_cleanup_interval_default(self, backend):
        assert backend._cleanup_interval_seconds == CLEANUP_INTERVAL_SECONDS


# ===========================================================================
# Bounded SSE queue
# ===========================================================================


class TestBoundedSSEQueue:
    @pytest.mark.asyncio
    async def test_register_connection_queue_is_bounded(self, svc):
        q = await svc.register_connection("s1", None)
        assert q.maxsize == SSE_QUEUE_MAXSIZE
        assert SSE_QUEUE_MAXSIZE > 0

    def test_push_drops_oldest_when_queue_is_full(self):
        registry = SourceRegistry()
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        registry.add_listener("session:s1", q)

        first = Notification(type="first")
        second = Notification(type="second")
        third = Notification(type="third")
        for notification in (first, second, third):
            registry.push("session:s1", notification)

        assert q.qsize() == 2
        assert [q.get_nowait().id for _ in range(2)] == [second.id, third.id]

    @pytest.mark.asyncio
    async def test_stalled_client_queue_does_not_grow(self, svc):
        q = await svc.register_connection("s1", None)

        for index in range(SSE_QUEUE_MAXSIZE * 3):
            await svc.send_to_session(
                "s1", Notification(type="spam", mode=NotificationMode.EPHEMERAL, payload={"i": index})
            )

        assert q.qsize() == SSE_QUEUE_MAXSIZE

    def test_slow_listener_does_not_block_other_listeners(self):
        registry = SourceRegistry()
        full: asyncio.Queue = asyncio.Queue(maxsize=1)
        drained: asyncio.Queue = asyncio.Queue(maxsize=8)
        registry.add_listener("session:s1", full)
        registry.add_listener("session:s1", drained)

        registry.push("session:s1", Notification(type="one"))
        latest = Notification(type="two")
        registry.push("session:s1", latest)

        assert full.qsize() == 1
        assert full.get_nowait().id == latest.id
        assert drained.qsize() == 2


# ===========================================================================
# Registry teardown on disconnect
# ===========================================================================


def _registry_footprint(svc: NotificationService) -> tuple[int, ...]:
    """Sizes of every per-connection structure the service keeps."""
    registry = svc._registry
    return (
        len(registry._listeners),
        len(registry._subscriptions),
        len(registry._subscribers),
        len(svc._loaded_user_subs),
        len(svc._session_users),
    )


class TestRegistryTeardown:
    @pytest.mark.asyncio
    async def test_anonymous_connect_disconnect_returns_to_baseline(self, svc):
        baseline = _registry_footprint(svc)

        q = await svc.register_connection("s1", None)
        assert _registry_footprint(svc) != baseline
        svc.unregister_connection("s1", q)

        assert _registry_footprint(svc) == baseline

    @pytest.mark.asyncio
    async def test_user_connect_disconnect_returns_to_baseline(self, svc):
        baseline = _registry_footprint(svc)

        for index in range(5):
            q = await svc.register_connection(f"s{index}", "alice")
            svc.unregister_connection(f"s{index}", q)

        assert _registry_footprint(svc) == baseline
        assert "user:alice" not in svc._registry._subscribers
        assert "user:alice" not in svc._registry._subscriptions

    @pytest.mark.asyncio
    async def test_user_edges_survive_while_another_session_is_live(self, svc):
        first = await svc.register_connection("s1", "alice")
        second = await svc.register_connection("s2", "alice")

        svc.unregister_connection("s1", first)

        assert "user:alice" in svc._registry._subscriptions
        assert svc._registry._subscribers["user:alice"] == {"session:s2"}
        assert "user:alice" in svc._loaded_user_subs

        # The still-live session keeps receiving user broadcasts
        notification = Notification(type="ping", mode=NotificationMode.EPHEMERAL)
        await svc.send_to_user("alice", notification)
        assert second.get_nowait().id == notification.id

        svc.unregister_connection("s2", second)
        assert _registry_footprint(svc) == (0, 0, 0, 0, 0)

    @pytest.mark.asyncio
    async def test_persistent_subscriptions_reload_after_full_disconnect(self, svc):
        backend = svc._get_backend()
        await backend.add_subscription("user:alice", "blog:tech")

        first = await svc.register_connection("s1", "alice")
        svc.unregister_connection("s1", first)
        assert _registry_footprint(svc) == (0, 0, 0, 0, 0)

        second = await svc.register_connection("s2", "alice")
        notification = Notification(type="post", mode=NotificationMode.EPHEMERAL)
        await svc.send("blog:tech", notification)

        assert second.get_nowait().id == notification.id

    @pytest.mark.asyncio
    async def test_global_broadcast_still_reaches_reconnected_session(self, svc):
        first = await svc.register_connection("s1", "alice")
        svc.unregister_connection("s1", first)

        second = await svc.register_connection("s1", "alice")
        notification = Notification(type="announce", mode=NotificationMode.EPHEMERAL)
        await svc.broadcast(notification)

        assert second.get_nowait().id == notification.id
