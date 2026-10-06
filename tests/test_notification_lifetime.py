"""Configurable notification lifetimes and on-demand clearing (#245).

Covers the ``notifications.queued_ttl_seconds`` / ``timeseries_ttl_seconds``
settings, the read bound every built-in backend applies, the sweep interval
cap, and ``clear_queued`` on the service, the backends, and a custom backend
written before the method existed.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from skrift.config import (
    MAX_NOTIFICATION_TTL_SECONDS,
    DatabaseConfig,
    NotificationsConfig,
    RedisConfig,
    Settings,
)
from skrift.lib.notification_backends import (
    CLEANUP_INTERVAL_SECONDS,
    MIN_CLEANUP_INTERVAL_SECONDS,
    QUEUED_TTL_HOURS,
    TIMESERIES_TTL_DAYS,
    InMemoryBackend,
    NotificationBackend,
    PgNotifyBackend,
    RedisBackend,
    _DatabaseStorageMixin,
)
from skrift.notifications import (
    RECENTLY_REMOVED_MAXSIZE,
    RECENTLY_REMOVED_SECONDS,
    Notification,
    NotificationMode,
    NotificationService,
    _RecentlyRemoved,
    clear_session_notifications,
    clear_source_notifications,
    clear_user_notifications,
)


def _settings(**notifications: Any) -> Settings:
    return Settings(
        secret_key="test-secret",
        db=DatabaseConfig(url="postgresql+asyncpg://u:p@localhost/db"),
        redis=RedisConfig(url="redis://localhost:6379"),
        notifications=NotificationsConfig(**notifications),
    )


def _aged(mode: NotificationMode, age_seconds: float, *, group: str | None = None) -> Notification:
    return Notification(
        type="aged", created_at=time.time() - age_seconds, mode=mode, group=group
    )


async def _drain(q: asyncio.Queue) -> list[Notification]:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ===========================================================================
# Settings
# ===========================================================================


class TestLifetimeSettings:
    def test_defaults_match_the_legacy_constants(self):
        config = NotificationsConfig()
        assert config.queued_ttl_seconds == QUEUED_TTL_HOURS * 3600 == 86400
        assert config.timeseries_ttl_seconds == TIMESERIES_TTL_DAYS * 86400 == 604800

    def test_existing_section_loads_unchanged(self):
        config = NotificationsConfig(**{"backend": "a.b:C", "webhook_secret": "s"})
        assert config.backend == "a.b:C"
        assert config.queued_ttl_seconds == 86400

    def test_accepts_sub_hour_lifetimes(self):
        config = NotificationsConfig(queued_ttl_seconds=0.5, timeseries_ttl_seconds=90)
        assert config.queued_ttl_seconds == 0.5
        assert config.timeseries_ttl_seconds == 90

    @pytest.mark.parametrize("field", ["queued_ttl_seconds", "timeseries_ttl_seconds"])
    @pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, "inf", 1e12, MAX_NOTIFICATION_TTL_SECONDS + 1])
    def test_rejects_non_positive_non_finite_or_unrepresentable(self, field, value):
        with pytest.raises(ValidationError):
            NotificationsConfig(**{field: value})

    def test_accepts_the_documented_maximum(self):
        config = NotificationsConfig(
            queued_ttl_seconds=MAX_NOTIFICATION_TTL_SECONDS,
            timeseries_ttl_seconds=MAX_NOTIFICATION_TTL_SECONDS,
        )
        assert config.queued_ttl_seconds == 100 * 365 * 86400


# ===========================================================================
# Backend construction
# ===========================================================================


class TestBackendsTakeLifetimesFromSettings:
    def test_in_memory_without_settings_uses_defaults(self):
        backend = InMemoryBackend()
        assert backend._queued_ttl_seconds == QUEUED_TTL_HOURS * 3600
        assert backend._timeseries_ttl_seconds == TIMESERIES_TTL_DAYS * 86400
        assert backend._sweep_interval_seconds() == CLEANUP_INTERVAL_SECONDS

    @pytest.mark.parametrize("backend_cls", [InMemoryBackend, RedisBackend, PgNotifyBackend])
    def test_built_in_backends_read_settings(self, backend_cls):
        backend = backend_cls(
            settings=_settings(queued_ttl_seconds=21600, timeseries_ttl_seconds=3600),
            session_maker=None,
        )
        assert backend._queued_ttl_seconds == 21600
        assert backend._timeseries_ttl_seconds == 3600

    @pytest.mark.parametrize(
        ("queued", "timeseries", "interval"),
        [
            (86400, 604800, CLEANUP_INTERVAL_SECONDS),
            (21600, 604800, CLEANUP_INTERVAL_SECONDS),
            (30, 604800, 30),
            (0.01, 604800, MIN_CLEANUP_INTERVAL_SECONDS),
            (86400, 45, 45),
            (86400, 0.1, MIN_CLEANUP_INTERVAL_SECONDS),
        ],
    )
    def test_sweep_interval_follows_the_shorter_lifetime(self, queued, timeseries, interval):
        backend = InMemoryBackend(
            settings=_settings(queued_ttl_seconds=queued, timeseries_ttl_seconds=timeseries)
        )
        assert backend._sweep_interval_seconds() == interval

    @pytest.mark.asyncio
    async def test_ensure_backend_started_passes_lifetimes(self):
        svc = NotificationService()
        settings = _settings(
            backend="skrift.lib.notification_backends:InMemoryBackend",
            queued_ttl_seconds=60,
        )
        assert await svc.ensure_backend_started(settings=settings) is True
        try:
            assert svc._backend._queued_ttl_seconds == 60
        finally:
            await svc.stop_backend()

    @pytest.mark.asyncio
    async def test_default_backend_honours_explicit_settings(self):
        svc = NotificationService()
        with patch("skrift.config.get_settings", return_value=_settings()):
            assert await svc.ensure_backend_started(settings=_settings(queued_ttl_seconds=1)) is False
            backend = svc._get_backend()
        assert backend._queued_ttl_seconds == 1

        await backend.store("session:s1", _aged(NotificationMode.QUEUED, 2))
        assert await svc.get_queued("s1", None) == []

    @pytest.mark.asyncio
    async def test_explicit_settings_reach_an_existing_lazy_backend(self):
        svc = NotificationService()
        with patch("skrift.config.get_settings", return_value=_settings()):
            backend = svc._get_backend()
            await svc.ensure_backend_started(settings=_settings(queued_ttl_seconds=1))
        assert svc._get_backend() is backend
        assert backend._queued_ttl_seconds == 1

    def test_lazy_fallback_reads_app_settings(self):
        with patch("skrift.config.get_settings", return_value=_settings(queued_ttl_seconds=60)):
            backend = NotificationService()._get_backend()
        assert backend._queued_ttl_seconds == 60

    def test_lazy_fallback_without_loadable_settings_uses_defaults(self):
        with patch("skrift.config.get_settings", side_effect=SystemExit("bad app.yaml")):
            backend = NotificationService()._get_backend()
        assert backend._queued_ttl_seconds == QUEUED_TTL_HOURS * 3600

    def test_asgi_default_backend_gets_settings(self):
        import inspect

        import skrift.asgi

        assert "InMemoryBackend(settings=settings)" in inspect.getsource(skrift.asgi)


# ===========================================================================
# Read bound and sweep: in-memory
# ===========================================================================


class TestInMemoryLifetime:
    @pytest.fixture
    def backend(self):
        return InMemoryBackend(
            settings=_settings(queued_ttl_seconds=60, timeseries_ttl_seconds=120)
        )

    @pytest.mark.asyncio
    async def test_queued_reads_exclude_expired_before_any_sweep(self, backend):
        stale = _aged(NotificationMode.QUEUED, 61)
        fresh = _aged(NotificationMode.QUEUED, 59)
        await backend.store("session:s1", stale)
        await backend.store("session:s1", fresh)

        assert [n.id for n in await backend.get_queued_multi(["session:s1"])] == [fresh.id]

    @pytest.mark.asyncio
    async def test_timeseries_reads_exclude_expired_before_any_sweep(self, backend):
        stale = _aged(NotificationMode.TIMESERIES, 121)
        fresh = _aged(NotificationMode.TIMESERIES, 119)
        await backend.store("session:s1", stale)
        await backend.store("session:s1", fresh)

        assert [n.id for n in await backend.get_since_multi(["session:s1"], 0)] == [fresh.id]

    @pytest.mark.asyncio
    async def test_sweep_deletes_by_configured_lifetime(self, backend):
        stale = _aged(NotificationMode.QUEUED, 61)
        fresh = _aged(NotificationMode.QUEUED, 59)
        ts_kept = _aged(NotificationMode.TIMESERIES, 61)
        await backend.store("session:s1", stale)
        await backend.store("session:s1", fresh)
        await backend.store("session:s1", ts_kept)

        await backend._delete_old_notifications()

        assert set(backend._queues["session:s1"]) == {fresh.id, ts_kept.id}

    @pytest.mark.asyncio
    async def test_service_replay_honours_lifetime(self, backend):
        svc = NotificationService()
        svc.set_backend(backend)
        await backend.store("session:s1", _aged(NotificationMode.QUEUED, 61))
        fresh = _aged(NotificationMode.QUEUED, 1)
        await backend.store("session:s1", fresh)

        assert [n.id for n in await svc.get_queued("s1", None)] == [fresh.id]


# ===========================================================================
# Read bound, sweep and clearing: database storage (SQLite stand-in)
# ===========================================================================


class _SqliteStorageBackend(_DatabaseStorageMixin):
    """The shared Redis/PgNotify storage layer without the fanout transport."""

    def __init__(self, *, settings: Settings | None, session_maker: Any) -> None:
        self._init_db(session_maker, settings)


@pytest.fixture
async def sqlite_session_maker():
    from skrift.db.base import Base
    from skrift.db.models.notification import StoredNotification

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[StoredNotification.__table__])
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def db_backend(sqlite_session_maker):
    return _SqliteStorageBackend(
        settings=_settings(queued_ttl_seconds=60, timeseries_ttl_seconds=120),
        session_maker=sqlite_session_maker,
    )


class TestDatabaseLifetime:
    @pytest.mark.asyncio
    async def test_default_lifetime_without_settings(self, sqlite_session_maker):
        backend = _SqliteStorageBackend(settings=None, session_maker=sqlite_session_maker)
        kept = _aged(NotificationMode.QUEUED, QUEUED_TTL_HOURS * 3600 - 60)
        await backend.store("session:s1", kept)
        await backend.store("session:s1", _aged(NotificationMode.QUEUED, QUEUED_TTL_HOURS * 3600 + 60))

        assert [n.id for n in await backend.get_queued_multi(["session:s1"])] == [kept.id]

    @pytest.mark.asyncio
    async def test_queued_reads_exclude_expired_before_any_sweep(self, db_backend):
        stale = _aged(NotificationMode.QUEUED, 61)
        fresh = _aged(NotificationMode.QUEUED, 59)
        await db_backend.store("session:s1", stale)
        await db_backend.store("session:s1", fresh)

        assert [n.id for n in await db_backend.get_queued_multi(["session:s1"])] == [fresh.id]

    @pytest.mark.asyncio
    async def test_timeseries_reads_exclude_expired_before_any_sweep(self, db_backend):
        stale = _aged(NotificationMode.TIMESERIES, 121)
        fresh = _aged(NotificationMode.TIMESERIES, 119)
        await db_backend.store("session:s1", stale)
        await db_backend.store("session:s1", fresh)

        assert [n.id for n in await db_backend.get_since_multi(["session:s1"], 0)] == [fresh.id]

    @pytest.mark.asyncio
    async def test_sweep_deletes_by_configured_lifetime(self, db_backend):
        from skrift.db.models.notification import StoredNotification
        from sqlalchemy import select

        stale = _aged(NotificationMode.QUEUED, 61)
        fresh = _aged(NotificationMode.QUEUED, 59)
        ts_kept = _aged(NotificationMode.TIMESERIES, 61)
        for n in (stale, fresh, ts_kept):
            await db_backend.store("session:s1", n)

        await db_backend._delete_old_notifications()

        async with db_backend._session_maker() as session:
            ids = set((await session.execute(select(StoredNotification.id))).scalars())
        assert ids == {fresh.id, ts_kept.id}

    @pytest.mark.asyncio
    async def test_maximum_lifetime_reads_and_sweeps(self, sqlite_session_maker):
        backend = _SqliteStorageBackend(
            settings=_settings(
                queued_ttl_seconds=MAX_NOTIFICATION_TTL_SECONDS,
                timeseries_ttl_seconds=MAX_NOTIFICATION_TTL_SECONDS,
            ),
            session_maker=sqlite_session_maker,
        )
        n = _aged(NotificationMode.QUEUED, 86400 * 365)
        await backend.store("session:s1", n)
        await backend._delete_old_notifications()
        assert [x.id for x in await backend.get_queued_multi(["session:s1"])] == [n.id]
        assert await backend.get_since_multi(["session:s1"], 0) == []

    @pytest.mark.asyncio
    async def test_clear_queued_by_source_and_group(self, db_backend):
        a1 = Notification(type="t", group="answer-1")
        a2 = Notification(type="t", group="answer-2")
        plain = Notification(type="t")
        ts = Notification(type="t", mode=NotificationMode.TIMESERIES)
        other = Notification(type="t", group="answer-1")
        for n in (a1, a2, plain, ts):
            await db_backend.store("user:alice", n)
        await db_backend.store("user:bob", other)

        assert await db_backend.clear_queued("user:alice", "answer-1") == [a1.id]
        assert {n.id for n in await db_backend.get_queued_multi(["user:alice"])} == {a2.id, plain.id}

        assert set(await db_backend.clear_queued("user:alice")) == {a2.id, plain.id}
        assert await db_backend.get_queued_multi(["user:alice"]) == []
        assert [n.id for n in await db_backend.get_since_multi(["user:alice"], 0)] == [ts.id]
        assert [n.id for n in await db_backend.get_queued_multi(["user:bob"])] == [other.id]
        assert await db_backend.clear_queued("user:alice") == []


class HeldSessions:
    """Session maker whose sessions pause after their first statement.

    Each session waits until *parties* sessions have run a statement, or
    *timeout* passes, before going on. Under select-then-delete code every
    party has SELECTed before anyone deletes; a single atomic statement holds
    its row lock instead, so the timeout lets it commit first.
    """

    def __init__(self, inner: Any, parties: int, timeout: float = 0.5) -> None:
        self._inner = inner
        self._parties = parties
        self._timeout = timeout
        self._ran = 0
        self._all_ran = asyncio.Event()

    @contextlib.asynccontextmanager
    async def __call__(self):
        async with self._inner() as session:
            yield _HeldSession(session, self)

    async def _after_first_statement(self) -> None:
        self._ran += 1
        if self._ran >= self._parties:
            self._all_ran.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._all_ran.wait(), self._timeout)


class _HeldSession:
    def __init__(self, session: Any, maker: HeldSessions) -> None:
        self._session = session
        self._maker = maker
        self._first = True

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.execute(*args, **kwargs)
        if self._first:
            self._first = False
            await self._maker._after_first_statement()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.fixture
async def sqlite_file_session_maker(tmp_path):
    from skrift.db.base import Base
    from skrift.db.models.notification import StoredNotification

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[StoredNotification.__table__])
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def assert_concurrent_removals_count_once(backend: Any, inner_session_maker: Any) -> None:
    """Shared by the SQLite test here and the Postgres integration test."""
    n = Notification(type="t", group="g")
    await backend.store("user:alice", n)
    backend._session_maker = HeldSessions(inner_session_maker, parties=2)
    first, second = await asyncio.gather(
        backend.clear_queued("user:alice"), backend.clear_queued("user:alice")
    )
    assert sorted(map(len, (first, second))) == [0, 1]

    backend._session_maker = inner_session_maker
    n2 = Notification(type="t", group="g")
    await backend.store("user:alice", n2)
    backend._session_maker = HeldSessions(inner_session_maker, parties=2)
    results = await asyncio.gather(
        backend.remove_by_group("user:alice", "g"), backend.remove_by_group("user:alice", "g")
    )
    assert sorted(results, key=lambda r: r is not None) == [None, n2.id]

    backend._session_maker = inner_session_maker
    n3 = Notification(type="t")
    await backend.store("user:alice", n3)
    backend._session_maker = HeldSessions(inner_session_maker, parties=2)
    results = await asyncio.gather(backend.remove(n3.id), backend.remove(n3.id))
    assert sorted(results, key=lambda r: r is not None) == [None, "user:alice"]
    backend._session_maker = inner_session_maker


class TestDatabaseRemovalsAreAtomic:
    @pytest.mark.asyncio
    async def test_concurrent_removals_count_each_row_once(self, sqlite_file_session_maker):
        backend = _SqliteStorageBackend(settings=None, session_maker=sqlite_file_session_maker)
        await assert_concurrent_removals_count_once(backend, sqlite_file_session_maker)


# ===========================================================================
# Clearing through the service
# ===========================================================================


class _LegacyBackend:
    """A custom backend written against the protocol before ``clear_queued``.

    Every protocol method is defined on the class (delegating to an in-memory
    store) so ``isinstance(..., NotificationBackend)`` holds as it would for a
    real third-party backend.
    """

    def __init__(self, *, settings: Any = None, session_maker: Any = None, **kwargs: Any) -> None:
        self._inner = InMemoryBackend()
        self.published: list[dict] = []

    async def publish(self, message: dict) -> None:
        self.published.append(message)


def _delegate(name: str):
    def method(self, *args, **kwargs):
        return getattr(self._inner, name)(*args, **kwargs)

    method.__name__ = name
    return method


for _name in (
    "start", "stop", "store", "remove", "remove_by_group", "get_mode",
    "get_queued_multi", "get_since_multi", "get_persistent_subscriptions",
    "add_subscription", "remove_subscription", "find_by_group",
    "dismiss_for_subscriber", "get_dismissed_ids", "cleanup_dismissed",
    "on_remote_message",
):
    setattr(_LegacyBackend, _name, _delegate(_name))


class _RecordingBackend(InMemoryBackend):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.published: list[dict] = []

    async def publish(self, message: dict) -> None:
        self.published.append(message)


@pytest.fixture(params=["built_in", "legacy"])
def backend(request):
    return _RecordingBackend() if request.param == "built_in" else _LegacyBackend()


@pytest.fixture
def svc(backend):
    service = NotificationService()
    service.set_backend(backend)
    return service


class TestClearQueued:
    def test_legacy_backend_still_satisfies_the_protocol(self):
        backend = _LegacyBackend()
        assert isinstance(backend, NotificationBackend)
        assert not hasattr(backend, "clear_queued")

    @pytest.mark.asyncio
    async def test_legacy_backend_loads_from_config_and_clears(self):
        svc = NotificationService()
        settings = _settings(backend="tests.test_notification_lifetime:_LegacyBackend")
        assert await svc.ensure_backend_started(settings=settings) is True
        try:
            await svc.send_to_user("alice", Notification(type="t"))
            assert await svc.clear_queued("user:alice") == 1
        finally:
            await svc.stop_backend()

    @pytest.mark.asyncio
    async def test_clears_a_user_and_leaves_everything_else(self, svc, backend):
        mine = [Notification(type="t", group=f"g{i}") for i in range(3)] + [Notification(type="t")]
        for n in mine:
            await svc.send_to_user("alice", n)
        ts = Notification(type="t", mode=NotificationMode.TIMESERIES)
        await svc.send_to_user("alice", ts)
        bob = Notification(type="t")
        await svc.send_to_user("bob", bob)
        session = Notification(type="t")
        await svc.send_to_session("s1", session)

        assert await svc.clear_queued("user:alice") == 4

        assert await backend.get_queued_multi(["user:alice"]) == []
        assert [n.id for n in await backend.get_since_multi(["user:alice"], 0)] == [ts.id]
        assert [n.id for n in await backend.get_queued_multi(["user:bob"])] == [bob.id]
        assert [n.id for n in await backend.get_queued_multi(["session:s1"])] == [session.id]
        assert await svc.clear_queued("user:alice") == 0

    @pytest.mark.asyncio
    async def test_clears_one_group(self, svc, backend):
        keep = Notification(type="t", group="answer-2")
        drop = Notification(type="t", group="answer-1")
        plain = Notification(type="t")
        for n in (keep, drop, plain):
            await svc.send("chat:42", n)

        assert await svc.clear_queued("chat:42", group="answer-1") == 1
        assert await svc.clear_queued("chat:42", group="missing") == 0
        assert {n.id for n in await backend.get_queued_multi(["chat:42"])} == {keep.id, plain.id}

    @pytest.mark.asyncio
    async def test_connected_clients_get_dismissed_events(self, svc):
        n1, n2 = Notification(type="t"), Notification(type="t")
        await svc.send_to_user("alice", n1)
        await svc.send_to_user("alice", n2)
        q = await svc.register_connection("s1", "alice")

        await svc.clear_queued("user:alice")

        events = await _drain(q)
        assert {e.type for e in events} == {"dismissed"}
        assert {e.payload["notification_id"] for e in events} == {str(n1.id), str(n2.id)}

    @pytest.mark.asyncio
    async def test_other_replicas_get_dismissed_events(self, svc, backend):
        n = Notification(type="t")
        await svc.send_to_user("alice", n)
        backend.published.clear()

        await svc.clear_queued("user:alice")

        assert len(backend.published) == 1
        message = backend.published[0]
        assert message["a"] == "s" and message["sk"] == "user:alice"

        other = NotificationService()
        q = await other.register_connection("s9", "alice")
        await other._handle_remote(message)
        [event] = await _drain(q)
        assert event.type == "dismissed"
        assert event.payload["notification_id"] == str(n.id)

    @pytest.mark.asyncio
    async def test_dismissed_events_are_not_stored(self, svc, backend):
        await svc.send_to_user("alice", Notification(type="t"))
        await svc.clear_queued("user:alice")
        assert await backend.get_since_multi(["user:alice"], 0) == []


class _ModeBlindLegacyBackend(_LegacyBackend):
    """A custom backend whose get_queued_multi also returns timeseries entries."""

    async def get_queued_multi(self, source_keys):
        return [n for key in source_keys for n in self._inner._queues.get(key, {}).values()]


class TestLegacyFallbackIsQueuedOnly:
    @pytest.mark.asyncio
    async def test_fallback_skips_non_queued_items(self):
        backend = _ModeBlindLegacyBackend()
        svc = NotificationService()
        svc.set_backend(backend)
        queued = Notification(type="t")
        ts = Notification(type="t", mode=NotificationMode.TIMESERIES)
        await svc.send_to_user("alice", queued)
        await svc.send_to_user("alice", ts)

        assert await svc.clear_queued("user:alice") == 1
        assert [n.id for n in await backend.get_since_multi(["user:alice"], 0)] == [ts.id]


def _wire_send(source_key: str, n: Notification, pid: str = "other-replica") -> dict:
    return {"a": "s", "sk": source_key, "pid": pid, "n": n.to_dict()}


class TestLateDeliveryAfterRemoval:
    @pytest.mark.asyncio
    async def test_remote_send_after_local_clear_is_dropped(self):
        backend = InMemoryBackend()
        svc = NotificationService()
        svc.set_backend(backend)
        n = Notification(type="t", payload={"title": "secret"})
        # Replica A stored it; its fanout has not reached this replica yet.
        await backend.store("user:alice", n)
        q = await svc.register_connection("s1", "alice")

        assert await svc.clear_queued("user:alice") == 1
        await svc._handle_remote(_wire_send("user:alice", n))

        assert [e.type for e in await _drain(q)] == ["dismissed"]

    @pytest.mark.asyncio
    async def test_remote_send_after_remote_clear_is_dropped(self):
        clearer = NotificationService()
        clearer.set_backend(_RecordingBackend())
        n = Notification(type="t")
        await clearer._backend.store("user:alice", n)
        await clearer.clear_queued("user:alice")
        [dismissal] = clearer._backend.published

        other = NotificationService()
        q = await other.register_connection("s9", "alice")
        await other._handle_remote(dismissal)
        await other._handle_remote(_wire_send("user:alice", n))

        events = await _drain(q)
        assert [e.type for e in events] == ["dismissed"]
        assert events[0].payload["notification_id"] == str(n.id)

    @pytest.mark.asyncio
    async def test_send_cleared_while_in_flight_is_not_pushed_or_published(self):
        svc = NotificationService()

        class ClearsDuringStore(_RecordingBackend):
            async def store(self, source_key, notification):
                old = await super().store(source_key, notification)
                if notification.type == "racy":
                    await svc.clear_queued(source_key)
                return old

        backend = ClearsDuringStore()
        svc.set_backend(backend)
        q = await svc.register_connection("s1", "alice")

        await svc.send_to_user("alice", Notification(type="racy"))

        assert [e.type for e in await _drain(q)] == ["dismissed"]
        assert [m["n"]["type"] for m in backend.published] == ["dismissed"]

    @pytest.mark.asyncio
    async def test_remote_send_of_a_group_replaced_notification_is_dropped(self):
        svc = NotificationService()
        svc.set_backend(InMemoryBackend())
        old = Notification(type="t", group="progress")
        await svc._backend.store("user:alice", old)
        await svc.send_to_user("alice", Notification(type="t", group="progress"))
        q = await svc.register_connection("s1", "alice")

        await svc._handle_remote(_wire_send("user:alice", old))

        assert await _drain(q) == []

    @pytest.mark.asyncio
    async def test_unrelated_remote_sends_still_arrive(self):
        svc = NotificationService()
        svc.set_backend(InMemoryBackend())
        await svc._backend.store("user:alice", Notification(type="t"))
        await svc.clear_queued("user:alice")
        q = await svc.register_connection("s1", "alice")
        fresh = Notification(type="t")

        await svc._handle_remote(_wire_send("user:alice", fresh))

        assert [e.id for e in await _drain(q)] == [fresh.id]

    def test_memory_is_bounded_in_size(self):
        from uuid import uuid4

        removed = _RecentlyRemoved(maxsize=3)
        ids = [uuid4() for _ in range(4)]
        for nid in ids:
            removed.add(nid)
        assert len(removed) == 3
        assert ids[0] not in removed
        assert all(nid in removed for nid in ids[1:])

    def test_memory_is_bounded_in_time(self, monkeypatch):
        from uuid import uuid4

        now = [1000.0]
        monkeypatch.setattr("skrift.notifications.time.monotonic", lambda: now[0])
        removed = _RecentlyRemoved(seconds=10)
        early, late = uuid4(), uuid4()
        removed.add(early)
        now[0] += 5
        removed.add(late)
        now[0] += 6
        assert early not in removed and late in removed
        removed.add(uuid4())
        assert len(removed) == 2  # the expired id was pruned

    def test_defaults(self):
        assert RECENTLY_REMOVED_MAXSIZE == 10_000
        assert RECENTLY_REMOVED_SECONDS == 600


class TestClearHelpers:
    @pytest.fixture(autouse=True)
    def isolated_service(self):
        svc = NotificationService()
        svc.set_backend(InMemoryBackend())
        with patch("skrift.notifications.notifications", svc):
            yield svc

    @pytest.mark.asyncio
    async def test_session_user_and_source_helpers(self, isolated_service):
        svc = isolated_service
        await svc.send_to_session("s1", Notification(type="t", group="g"))
        await svc.send_to_session("s1", Notification(type="t"))
        await svc.send_to_user("alice", Notification(type="t"))
        await svc.send("blog:tech", Notification(type="t"))

        assert await clear_session_notifications("s1", group="g") == 1
        assert await clear_session_notifications("s1") == 1
        assert await clear_user_notifications("alice") == 1
        assert await clear_source_notifications("blog:tech") == 1
        assert await clear_source_notifications("blog:tech") == 0
