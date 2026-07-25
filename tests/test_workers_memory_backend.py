"""Memory-pressure regression tests for the in-memory worker backends."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from skrift.workers import (
    InMemoryEventLog,
    InMemoryQueue,
    InMemoryStateStore,
    Job,
    JobStatus,
    RetryPolicy,
    WorkerConfig,
    WorkerRuntime,
)
from skrift.workers.memory import DEFAULT_EVENT_STREAM_MAX_EVENTS
from skrift.workers.models import EventIdConflict, JobEnvelope, JobState, utcnow
from skrift.workers.registry import HandlerRegistry
from skrift.workers.runtime import TERMINAL_JOB_STATE_TTL_SECONDS


class Greeting(Job):
    name: str


async def _claim_with_expired_visibility(queue: InMemoryQueue) -> JobEnvelope:
    job = JobEnvelope(type="stuck", queue="default")
    await queue.submit(job)
    claimed = await queue.claim(["default"], visibility_timeout=0.01)
    assert claimed is not None
    await asyncio.sleep(0.02)
    return job


def _entry(queue: InMemoryQueue, job: JobEnvelope):
    return queue._entries[job.queue][job.id]


async def test_release_expired_claims_takes_now_like_the_other_queue_backends():
    queue = InMemoryQueue()
    job = await _claim_with_expired_visibility(queue)

    await queue._release_expired_claims(utcnow())

    entry = _entry(queue, job)
    assert entry.claim_token is None
    assert entry.job.reclaim_count == 1


async def test_release_expired_claims_uses_the_supplied_now():
    queue = InMemoryQueue()
    job = JobEnvelope(type="held", queue="default")
    await queue.submit(job)
    claimed = await queue.claim(["default"], visibility_timeout=60)
    assert claimed is not None

    await queue._release_expired_claims(utcnow())
    assert _entry(queue, job).claim_token is not None

    await queue._release_expired_claims(utcnow() + timedelta(seconds=120))
    assert _entry(queue, job).claim_token is None
    assert _entry(queue, job).job.reclaim_count == 1


async def test_claim_and_stats_still_reclaim_expired_claims():
    queue = InMemoryQueue()
    job = await _claim_with_expired_visibility(queue)

    stats = await queue.stats("default")
    assert stats.ready == 1
    assert stats.claimed == 0

    reclaimed = await queue.claim(["default"], visibility_timeout=1)
    assert reclaimed is not None
    assert reclaimed.job.id == job.id
    assert reclaimed.job.reclaim_count == 1


async def test_runtime_reaper_reclaims_memory_queue_claims_and_sweeps_state():
    queue = InMemoryQueue()
    state_store = InMemoryStateStore()
    job = await _claim_with_expired_visibility(queue)
    await state_store.set("workers:jobs:expired", "gone", ttl=0.01)
    await state_store.set("workers:jobs:live", "kept")
    await asyncio.sleep(0.02)

    runtime = WorkerRuntime(
        config=WorkerConfig(mode="in_process", queues=("idle",), reaper_interval=0.02),
        queue=queue,
        state_store=state_store,
        handler_registry=HandlerRegistry(),
    )
    await runtime.start()
    try:
        for _ in range(100):
            if _entry(queue, job).claim_token is None and "workers:jobs:expired" not in (
                state_store._values
            ):
                break
            await asyncio.sleep(0.02)
    finally:
        await runtime.stop()

    assert _entry(queue, job).claim_token is None
    assert _entry(queue, job).job.reclaim_count == 1
    assert "workers:jobs:expired" not in state_store._values
    assert "workers:jobs:live" in state_store._values


async def test_event_log_bounds_streams_and_keeps_positions_monotonic():
    log = InMemoryEventLog(max_events_per_stream=3)

    positions = [await log.append("s", {"n": index}) for index in range(6)]

    assert positions == [0, 1, 2, 3, 4, 5]
    assert await log.read("s") == [(3, {"n": 3}), (4, {"n": 4}), (5, {"n": 5})]
    assert await log.append("s", {"n": 6}) == 6


async def test_event_log_reads_ignore_positions_that_were_pruned():
    log = InMemoryEventLog(max_events_per_stream=2)
    for index in range(5):
        await log.append("s", {"n": index, "job_id": "job-1" if index % 2 else "job-0"})

    assert await log.read("s", from_position=0) == [
        (3, {"n": 3, "job_id": "job-1"}),
        (4, {"n": 4, "job_id": "job-0"}),
    ]
    assert await log.read("s", from_position=4) == [(4, {"n": 4, "job_id": "job-0"})]
    assert await log.read("s", from_position=9) == []
    assert await log.read("s", from_position=0, limit=1) == [(3, {"n": 3, "job_id": "job-1"})]
    assert await log.read_filtered("s", filters={"job_id": "job-1"}) == [
        (3, {"n": 3, "job_id": "job-1"})
    ]
    assert await log.read_tail("s", limit=1) == [(4, {"n": 4, "job_id": "job-0"})]
    assert await log.read_tail("s", limit=0) == []


async def test_event_log_dedupe_index_is_bounded_with_the_stream():
    log = InMemoryEventLog(max_events_per_stream=2)
    assert await log.append("s", {"event_id": "evt-0", "n": 0}) == 0
    assert await log.append("s", {"event_id": "evt-1", "n": 1}) == 1

    assert await log.append("s", {"event_id": "evt-1", "n": 1}) == 1
    with pytest.raises(EventIdConflict):
        await log.append("s", {"event_id": "evt-1", "n": 99})

    assert await log.append("s", {"event_id": "evt-2", "n": 2}) == 2
    assert log.stream_event_id_count("s") == 2
    # evt-0 fell out of the retained window, so its id is no longer indexed.
    assert await log.append("s", {"event_id": "evt-0", "n": 3}) == 3
    assert log.stream_event_id_count("s") == 2


async def test_event_log_subscribe_tails_a_bounded_stream():
    log = InMemoryEventLog(max_events_per_stream=2)
    await log.append("s", {"n": 0})
    await log.append("s", {"n": 1})

    subscription = log.subscribe("s", from_position=2)
    next_event = asyncio.create_task(anext(subscription))
    await log.append("s", {"n": 2})
    assert await asyncio.wait_for(next_event, timeout=1) == (2, {"n": 2})
    await subscription.aclose()


async def test_event_log_subscriber_skips_events_pruned_before_it_read_them():
    log = InMemoryEventLog(max_events_per_stream=2)
    subscription = log.subscribe("s", from_position=0)
    for index in range(4):
        await log.append("s", {"n": index})

    assert await asyncio.wait_for(anext(subscription), timeout=1) == (2, {"n": 2})
    assert await asyncio.wait_for(anext(subscription), timeout=1) == (3, {"n": 3})
    await subscription.aclose()


async def test_event_log_defaults_to_a_bounded_stream():
    log = InMemoryEventLog()

    assert log.max_events_per_stream == DEFAULT_EVENT_STREAM_MAX_EVENTS
    assert DEFAULT_EVENT_STREAM_MAX_EVENTS > 0


async def test_event_log_delete_resets_a_stream():
    log = InMemoryEventLog(max_events_per_stream=2)
    await log.append("s", {"n": 0})
    await log.delete("s")

    assert await log.read("s") == []
    assert await log.append("s", {"n": 1}) == 0
    assert await log.list_streams() == ["s"]


async def test_state_store_sweep_expired_reclaims_entries():
    store = InMemoryStateStore()
    await store.set("workers:jobs:expired", "gone", ttl=0.01)
    await store.set("workers:jobs:live", "kept")
    await asyncio.sleep(0.02)

    removed = await store.sweep_expired()

    assert removed == 1
    assert list(store._values) == ["workers:jobs:live"]
    assert await store.sweep_expired() == 0


async def test_state_store_reports_worker_job_states_with_total():
    store = InMemoryStateStore()
    older = JobState(job=JobEnvelope(type="older"))
    older.updated_at = utcnow() - timedelta(minutes=5)
    newer = JobState(job=JobEnvelope(type="newer"))
    newer.updated_at = utcnow()
    await store.set(f"workers:jobs:{older.job.id}", older)
    await store.set(f"workers:jobs:{newer.job.id}", newer)
    await store.set("workers:other", "ignored")

    states, total = await store.worker_job_states(limit=1)

    assert total == 2
    assert [state.job.type for state in states] == ["newer"]


async def test_runtime_expires_terminal_job_state_but_keeps_active_state():
    state_store = InMemoryStateStore()
    registry = HandlerRegistry()
    runtime = WorkerRuntime(
        config=WorkerConfig(mode="inline"),
        state_store=state_store,
        handler_registry=registry,
    )

    async def greet(job: Greeting) -> str:
        return job.name.upper()

    registry.register("greet", greet)

    completed = await runtime.submit("greet", {"name": "Ada"})
    state = await runtime.get_job_state(completed.id)
    assert state.status == JobStatus.COMPLETED

    stored = state_store._values[f"workers:jobs:{completed.id}"]
    assert stored.expires_at is not None
    remaining = (stored.expires_at - utcnow()).total_seconds()
    assert TERMINAL_JOB_STATE_TTL_SECONDS - 60 < remaining <= TERMINAL_JOB_STATE_TTL_SECONDS

    queued_runtime = WorkerRuntime(
        config=WorkerConfig(mode="in_process"),
        state_store=state_store,
        handler_registry=registry,
    )
    submitted = await queued_runtime.submit("greet", {"name": "Grace"})
    assert (await queued_runtime.get_job_state(submitted.id)).status == JobStatus.SUBMITTED
    assert state_store._values[f"workers:jobs:{submitted.id}"].expires_at is None


async def test_runtime_expires_dead_lettered_job_state():
    state_store = InMemoryStateStore()
    registry = HandlerRegistry()
    runtime = WorkerRuntime(
        config=WorkerConfig(mode="inline"),
        state_store=state_store,
        handler_registry=registry,
    )

    async def boom(job: Greeting) -> None:
        raise RuntimeError("nope")

    registry.register("boom", boom, retry_policy=RetryPolicy(max_attempts=1))

    handle = await runtime.submit("boom", {"name": "Ada"})
    state = await runtime.get_job_state(handle.id)
    assert state.status == JobStatus.DEAD_LETTERED
    assert state_store._values[f"workers:jobs:{handle.id}"].expires_at is not None


async def test_runtime_expires_cancelled_job_state():
    state_store = InMemoryStateStore()
    registry = HandlerRegistry()
    runtime = WorkerRuntime(
        config=WorkerConfig(mode="in_process"),
        state_store=state_store,
        handler_registry=registry,
    )

    async def greet(job: Greeting) -> str:
        return job.name.upper()

    registry.register("greet", greet)

    handle = await runtime.submit("greet", {"name": "Ada"})
    assert await runtime.cancel(handle.id) is True
    assert state_store._values[f"workers:jobs:{handle.id}"].expires_at is not None
