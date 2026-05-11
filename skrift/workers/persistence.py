"""Background persistence services for worker hot-path backends."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Any

from skrift.workers.interfaces import Archive, EventLog, StateStore


class EventFlusher:
    """Copy EventLog records into Archive while tracking durable cursors."""

    def __init__(
        self,
        *,
        event_log: EventLog,
        archive: Archive,
        state_store: StateStore,
        streams: Iterable[str],
        stream_prefixes: Iterable[str] = (),
        batch_size: int = 100,
        interval: float = 1.0,
        cursor_prefix: str = "workers:persister:event_cursors",
    ) -> None:
        self.event_log = event_log
        self.archive = archive
        self.state_store = state_store
        self.streams = tuple(streams)
        self.stream_prefixes = tuple(stream_prefixes)
        self.batch_size = batch_size
        self.interval = interval
        self.cursor_prefix = cursor_prefix.rstrip(":")
        self._task: asyncio.Task | None = None

    async def flush_once(self, stream: str | None = None) -> int:
        """Flush one batch for one stream, or one batch for every configured stream."""
        if stream is None:
            counts = [await self.flush_once(item) for item in await self._expand_streams()]
            return sum(counts)

        cursor_key = self._cursor_key(stream)
        cursor = await self.state_store.get(cursor_key)
        from_position = int(cursor or 0)
        events = await self.event_log.read(
            stream,
            from_position=from_position,
            limit=self.batch_size,
        )
        if not events:
            return 0

        await self.archive.bulk_insert_events(
            [(stream, position, event) for position, event in events]
        )
        await self.state_store.set(cursor_key, events[-1][0] + 1)
        return len(events)

    async def start(self) -> None:
        """Start flushing in the background."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run_loop(),
                name="skrift-worker-event-flusher",
            )

    async def stop(self) -> None:
        """Stop the background flusher."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            await self.flush_once()
            await asyncio.sleep(self.interval)

    async def _expand_streams(self) -> list[str]:
        return await _expand_event_streams(self.event_log, self.streams, self.stream_prefixes)

    def _cursor_key(self, stream: str) -> str:
        return f"{self.cursor_prefix}:{stream}"


class StateSnapshotter:
    """Archive selected StateStore values as historical snapshots."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        archive: Archive,
        keys: Iterable[str] = (),
        prefixes: Iterable[str] = (),
        interval: float = 60.0,
    ) -> None:
        self.state_store = state_store
        self.archive = archive
        self.keys = tuple(keys)
        self.prefixes = tuple(prefixes)
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def snapshot_once(self) -> int:
        """Snapshot all configured keys and prefixes once."""
        keys = set(self.keys)
        for prefix in self.prefixes:
            keys.update(await self.state_store.keys(prefix))

        count = 0
        for key in sorted(keys):
            value = await self.state_store.get(key)
            if value is None:
                continue
            await self.archive.upsert_state_snapshot(key, value)
            count += 1
        return count

    async def start(self) -> None:
        """Start snapshotting in the background."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run_loop(),
                name="skrift-worker-state-snapshotter",
            )

    async def stop(self) -> None:
        """Stop the background snapshotter."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            await self.snapshot_once()
            await asyncio.sleep(self.interval)


class WorkerPruner:
    """Prune worker hot-path and archive data according to retention settings."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        event_log: EventLog,
        queue: Any,
        dead_letter_store: Any,
        archive: Archive,
        streams: Iterable[str],
        retention: Any,
        stream_prefixes: Iterable[str] = (),
        cursor_prefix: str = "workers:persister:event_cursors",
    ) -> None:
        self.state_store = state_store
        self.event_log = event_log
        self.queue = queue
        self.dead_letter_store = dead_letter_store
        self.archive = archive
        self.streams = tuple(streams)
        self.stream_prefixes = tuple(stream_prefixes)
        self.retention = retention
        self.cursor_prefix = cursor_prefix.rstrip(":")
        self._task: asyncio.Task | None = None

    async def prune_once(self) -> dict[str, int]:
        """Run one retention pass and return deletion counts by category."""
        counts = {
            "redis_events": 0,
            "terminal_job_states": 0,
            "dead_queue_markers": 0,
            "archive_events": 0,
            "archive_snapshots": 0,
            "dlq_resolved": 0,
        }

        prune_events = getattr(self.event_log, "prune_archived_events", None)
        if callable(prune_events):
            for stream in await self._expand_streams():
                cursor = await self.state_store.get(self._cursor_key(stream))
                counts["redis_events"] += await prune_events(
                    stream,
                    archived_position=int(cursor or 0),
                    max_age_seconds=self.retention.redis_event_ttl,
                    max_entries=self.retention.redis_event_max_entries,
                )

        prune_states = getattr(self.state_store, "prune_terminal_job_states", None)
        if callable(prune_states):
            counts["terminal_job_states"] = await prune_states(
                max_age_seconds=self.retention.terminal_job_state_ttl
            )

        prune_dead_markers = getattr(self.queue, "prune_dead_markers", None)
        if callable(prune_dead_markers):
            counts["dead_queue_markers"] = await prune_dead_markers(
                max_age_seconds=self.retention.dead_queue_marker_ttl
            )

        prune_archive = getattr(self.archive, "prune", None)
        if callable(prune_archive):
            archive_counts = await prune_archive(
                event_max_age_seconds=self.retention.archive_event_ttl,
                snapshot_max_age_seconds=self.retention.archive_snapshot_ttl,
            )
            counts["archive_events"] = int(archive_counts.get("events", 0))
            counts["archive_snapshots"] = int(archive_counts.get("snapshots", 0))

        prune_resolved = getattr(self.dead_letter_store, "prune_resolved", None)
        if callable(prune_resolved):
            counts["dlq_resolved"] = await prune_resolved(
                max_age_seconds=self.retention.dlq_resolved_ttl
            )

        return counts

    async def start(self) -> None:
        """Start pruning in the background."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run_loop(),
                name="skrift-worker-pruner",
            )

    async def stop(self) -> None:
        """Stop the background pruner."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            await self.prune_once()
            await asyncio.sleep(self.retention.prune_interval)

    async def _expand_streams(self) -> list[str]:
        return await _expand_event_streams(self.event_log, self.streams, self.stream_prefixes)

    def _cursor_key(self, stream: str) -> str:
        return f"{self.cursor_prefix}:{stream}"


async def _expand_event_streams(
    event_log: EventLog,
    streams: Iterable[str],
    stream_prefixes: Iterable[str],
) -> list[str]:
    expanded = list(streams)
    prefixes = tuple(stream_prefixes)
    if prefixes:
        list_streams = getattr(event_log, "list_streams", None)
        if not callable(list_streams):
            raise TypeError("Event log backend does not support stream prefix discovery")
        for prefix in prefixes:
            expanded.extend(await list_streams(prefix=prefix))
    return _dedupe_streams(expanded)


def _dedupe_streams(streams: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for stream in streams:
        if stream in seen:
            continue
        seen.add(stream)
        unique.append(stream)
    return unique
