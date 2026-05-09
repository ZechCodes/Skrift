"""CLI commands for Skrift."""

import base64
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click


@click.group()
@click.version_option(package_name="skrift")
@click.option(
    "-f",
    "--config-file",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    default=None,
    help="Path to config file (overrides SKRIFT_ENV-based resolution).",
)
def cli(config_file):
    """Skrift - A lightweight async Python CMS."""
    if config_file:
        from skrift.config import set_config_path

        set_config_path(Path(config_file))


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
@click.option("--workers", default=1, type=int, help="Number of worker processes")
@click.option(
    "--log-level",
    default="info",
    type=click.Choice(["debug", "info", "warning", "error"]),
    help="Logging level",
)
@click.option(
    "--subdomain",
    default=None,
    help="Serve only this subdomain site (for local multi-site testing)",
)
def serve(host, port, reload, workers, log_level, subdomain):
    """Run the Skrift server."""
    import asyncio
    import signal

    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config

    if subdomain:
        os.environ["SKRIFT_SUBDOMAIN"] = subdomain
        click.echo(f"Serving subdomain '{subdomain}' on {host}:{port}")

    config = Config()
    config.application_path = "skrift.asgi:app"
    config.bind = [f"{host}:{port}"]
    config.workers = 1 if reload else workers
    config.loglevel = log_level.upper()
    config.include_server_header = False

    if reload:
        config.use_reloader = True
        from hypercorn.run import run
        run(config)
        return

    from skrift.asgi import app

    shutdown_event = asyncio.Event()

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    try:
        loop.run_until_complete(
            hypercorn_serve(app, config, shutdown_trigger=shutdown_event.wait)
        )
    finally:
        loop.close()


def _worker_memory_backends(backends) -> list[str]:
    from skrift.config import worker_memory_backends

    return worker_memory_backends(backends)


def _validate_worker_process_backends(
    settings,
    *,
    allow_memory_backends: bool,
    context: str = "worker",
) -> None:
    from skrift.config import validate_worker_runtime_config

    try:
        validate_worker_runtime_config(
            settings.workers,
            context=context,
            allow_memory_backends=allow_memory_backends,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _build_db_config(settings):
    from advanced_alchemy.config import EngineConfig
    from advanced_alchemy.extensions.litestar import AsyncSessionConfig, SQLAlchemyAsyncConfig

    return SQLAlchemyAsyncConfig(
        connection_string=settings.db.url,
        session_config=AsyncSessionConfig(expire_on_commit=False),
        engine_config=EngineConfig(echo=settings.db.echo),
    )


def _import_worker_modules(settings, extra_imports: tuple[str, ...] = ()) -> None:
    import importlib

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    for spec in [*settings.controllers, *settings.workers.imports, *extra_imports]:
        module_path = spec.split(":", 1)[0]
        importlib.import_module(module_path)


def _configure_worker_runtime(settings, *, session_maker, queues, concurrency, mode=None):
    from skrift.workers import configure_workers

    return configure_workers(
        mode=mode or "in_process",
        queues=tuple(queues),
        concurrency=concurrency,
        poll_interval=settings.workers.poll_interval,
        visibility_timeout=settings.workers.visibility_timeout,
        max_reclaims=settings.workers.max_reclaims,
        backend_imports=settings.workers.backends,
        settings=settings,
        session_maker=session_maker,
    )


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return "0s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes}m {int(seconds % 60)}s"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h {minutes}m"


def _echo_table(headers: list[str], rows: list[list[object]]) -> None:
    values = [[str(value) for value in row] for row in rows]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in values)) if values else len(header)
        for index, header in enumerate(headers)
    ]
    click.echo("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    click.echo("  ".join("-" * width for width in widths))
    for row in values:
        click.echo("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _job_state_dict(state) -> dict:
    return state.model_dump(mode="json")


def _dlq_entry_dict(entry) -> dict:
    return entry.model_dump(mode="json")


_DLQ_DURATION_RE = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[smhdw])$")


def _parse_dlq_time(value: str | None, *, option_name: str) -> datetime | None:
    if not value:
        return None
    match = _DLQ_DURATION_RE.match(value.strip())
    if match:
        amount = float(match.group("amount"))
        unit = match.group("unit")
        seconds = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
            "d": 24 * 60 * 60,
            "w": 7 * 24 * 60 * 60,
        }[unit]
        return datetime.now(timezone.utc) - timedelta(seconds=amount * seconds)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise click.ClickException(
            f"Invalid {option_name} value {value!r}; use an ISO datetime or a duration "
            "like 15m, 1h, or 2d."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dlq_filters(
    *,
    queue: str | None = None,
    job_type: str | None = None,
    cause: str | None = None,
    state: str | None = None,
    exception_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    return {
        "queue": queue or None,
        "job_type": job_type or None,
        "cause": cause or None,
        "state": state or None,
        "exception_type": exception_type or None,
        "created_after": _parse_dlq_time(since, option_name="--since"),
        "created_before": _parse_dlq_time(until, option_name="--until"),
    }


def _has_dlq_filters(**values) -> bool:
    return any(value not in (None, "") for value in values.values())


async def _select_dlq_entry_ids(runtime, entry_ids: tuple[str, ...], filters: dict) -> list[str]:
    if entry_ids:
        return list(entry_ids)
    entries = await runtime.inspect_dlq(**filters)
    return [entry.id for entry in entries]


@cli.group("workers")
def workers_group():
    """Manage Skrift worker processes and worker operations."""


@workers_group.command("run")
@click.option("--queue", "queues", multiple=True, help="Queue to drain. Defaults to config.")
@click.option("--concurrency", type=int, default=None, help="Worker concurrency override.")
@click.option("--poll-interval", type=float, default=None, help="Queue poll interval override.")
@click.option("--visibility-timeout", type=float, default=None, help="Claim visibility timeout override.")
@click.option("--max-reclaims", type=int, default=None, help="Max reclaim count override.")
@click.option("--import", "imports", multiple=True, help="Additional handler module to import.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_run(
    queues,
    concurrency,
    poll_interval,
    visibility_timeout,
    max_reclaims,
    imports,
    allow_memory_backends,
):
    """Run a standalone worker process."""
    import asyncio
    import signal

    from skrift.config import get_settings

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="worker",
    )
    _import_worker_modules(settings, imports)
    if poll_interval is not None:
        settings.workers.poll_interval = poll_interval
    if visibility_timeout is not None:
        settings.workers.visibility_timeout = visibility_timeout
    if max_reclaims is not None:
        settings.workers.max_reclaims = max_reclaims

    async def _run():
        db_config = _build_db_config(settings)
        selected_queues = queues or tuple(settings.workers.queues)
        runtime = _configure_worker_runtime(
            settings,
            session_maker=db_config.get_session,
            queues=selected_queues,
            concurrency=concurrency or settings.workers.concurrency,
        )
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_event.set)
        await runtime.start()
        click.echo(
            "Worker process draining "
            f"{', '.join(selected_queues)} with concurrency {runtime.config.concurrency}"
        )
        try:
            await shutdown_event.wait()
        finally:
            await runtime.stop()
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_group.command("persister")
@click.option("--stream", "streams", multiple=True, help="Event stream to flush.")
@click.option("--batch-size", type=int, default=None, help="Flush batch size override.")
@click.option("--flush-interval", type=float, default=None, help="Flush interval override.")
@click.option("--snapshot-key", "snapshot_keys", multiple=True, help="State key to snapshot.")
@click.option("--snapshot-prefix", "snapshot_prefixes", multiple=True, help="State key prefix to snapshot.")
@click.option("--snapshot-interval", type=float, default=None, help="Snapshot interval override.")
@click.option("--once", is_flag=True, help="Run one flush/snapshot pass and exit.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def persister(
    streams,
    batch_size,
    flush_interval,
    snapshot_keys,
    snapshot_prefixes,
    snapshot_interval,
    once,
    allow_memory_backends,
):
    """Run worker event/state persistence services."""
    import asyncio
    import signal

    from skrift.config import get_settings
    from skrift.workers import EventFlusher, StateSnapshotter, WorkerPruner
    from skrift.workers.runtime import _coerce_backend_config, _instantiate_backend

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="persister",
    )

    async def _run():
        db_config = _build_db_config(settings)
        backends = _coerce_backend_config(settings.workers.backends)
        kwargs = {"settings": settings, "session_maker": db_config.get_session}
        state_store = _instantiate_backend(backends.state_store, kind="state_store", **kwargs)
        event_log = _instantiate_backend(backends.event_log, kind="event_log", **kwargs)
        archive = _instantiate_backend(backends.archive, kind="archive", **kwargs)
        persistence = settings.workers.persistence
        flusher = EventFlusher(
            event_log=event_log,
            archive=archive,
            state_store=state_store,
            streams=streams or tuple(persistence.streams),
            batch_size=batch_size or persistence.batch_size,
            interval=flush_interval or persistence.flush_interval,
        )
        snapshotter = StateSnapshotter(
            state_store=state_store,
            archive=archive,
            keys=snapshot_keys or tuple(persistence.snapshot_keys),
            prefixes=snapshot_prefixes or tuple(persistence.snapshot_prefixes),
            interval=snapshot_interval or persistence.snapshot_interval,
        )
        pruner = None
        if settings.workers.retention.enabled:
            queue = _instantiate_backend(backends.queue, kind="queue", **kwargs)
            dead_letter_store = _instantiate_backend(
                backends.dead_letter_store,
                kind="dead_letter_store",
                **kwargs,
            )
            pruner = WorkerPruner(
                state_store=state_store,
                event_log=event_log,
                queue=queue,
                dead_letter_store=dead_letter_store,
                archive=archive,
                streams=streams or tuple(persistence.streams),
                retention=settings.workers.retention,
            )
        if once:
            flushed = await flusher.flush_once()
            snapshotted = await snapshotter.snapshot_once()
            pruned = await pruner.prune_once() if pruner is not None else {}
            if pruned:
                click.echo(
                    f"Flushed {flushed} event(s); snapshotted {snapshotted} state key(s); "
                    f"pruned {sum(pruned.values())} item(s)."
                )
            else:
                click.echo(f"Flushed {flushed} event(s); snapshotted {snapshotted} state key(s).")
            await db_config.get_engine().dispose()
            return

        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_event.set)
        await flusher.start()
        await snapshotter.start()
        if pruner is not None:
            await pruner.start()
        click.echo("Worker persister running.")
        try:
            await shutdown_event.wait()
        finally:
            if pruner is not None:
                await pruner.stop()
            await flusher.stop()
            await snapshotter.stop()
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_group.command("prune")
@click.option("--stream", "streams", multiple=True, help="Event stream to prune.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def workers_prune(streams, allow_memory_backends, as_json):
    """Run one worker retention/pruning pass."""
    import asyncio

    from skrift.config import get_settings
    from skrift.workers import WorkerPruner
    from skrift.workers.runtime import _coerce_backend_config, _instantiate_backend

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="persister",
    )

    async def _run():
        db_config = _build_db_config(settings)
        backends = _coerce_backend_config(settings.workers.backends)
        kwargs = {"settings": settings, "session_maker": db_config.get_session}
        pruner = WorkerPruner(
            state_store=_instantiate_backend(backends.state_store, kind="state_store", **kwargs),
            event_log=_instantiate_backend(backends.event_log, kind="event_log", **kwargs),
            queue=_instantiate_backend(backends.queue, kind="queue", **kwargs),
            dead_letter_store=_instantiate_backend(
                backends.dead_letter_store,
                kind="dead_letter_store",
                **kwargs,
            ),
            archive=_instantiate_backend(backends.archive, kind="archive", **kwargs),
            streams=streams or tuple(settings.workers.persistence.streams),
            retention=settings.workers.retention,
        )
        try:
            counts = await pruner.prune_once()
        finally:
            await db_config.get_engine().dispose()
        if as_json:
            click.echo(json.dumps(counts, indent=2, sort_keys=True))
            return
        rows = [[name, count] for name, count in counts.items()]
        _echo_table(["Category", "Pruned"], rows)

    asyncio.run(_run())


@workers_group.group("queues")
def workers_queues():
    """Inspect worker queues."""


@workers_queues.command("list")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_queues_list(allow_memory_backends):
    """List queue depth and state."""
    import asyncio

    from skrift.config import get_settings

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            snapshot = await runtime.inspect(job_limit=0, event_limit=0)
            rows = []
            for stats in snapshot["queues"]:
                total = stats.ready + stats.delayed + stats.claimed + stats.dead_lettered
                rows.append([
                    stats.queue,
                    stats.ready,
                    stats.delayed,
                    stats.claimed,
                    stats.dead_lettered,
                    total,
                    _format_duration(stats.oldest_ready_age_seconds),
                ])
            _echo_table(
                ["Queue", "Ready", "Delayed", "Claimed", "Dead", "Total", "Oldest ready"],
                rows,
            )
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_group.group("jobs")
def workers_jobs():
    """Inspect worker jobs."""


@workers_jobs.command("inspect")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_jobs_inspect(job_id, as_json, allow_memory_backends):
    """Inspect one worker job."""
    import asyncio

    from skrift.config import get_settings
    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            state = await runtime.get_job_state(job_id)
            if state is None:
                raise click.ClickException(f"Unknown worker job id {job_id!r}")
            events = [
                {"position": position, **event}
                for position, event in await runtime.lifecycle_events_for_job(job_id)
            ]
            payload = {"job": _job_state_dict(state), "events": events}
            if as_json:
                click.echo(json.dumps(payload, indent=2, sort_keys=True))
                return
            click.echo(f"Job: {state.job.id}")
            click.echo(f"Type: {state.job.type}")
            click.echo(f"Queue: {state.job.queue}")
            click.echo(f"Status: {state.status.value}")
            click.echo(f"Attempt: {state.attempt} / {state.job.max_attempts}")
            click.echo(f"Updated: {state.updated_at.isoformat()}")
            if state.error or state.last_error:
                click.echo(f"Error: {state.error or state.last_error}")
            if state.result is not None:
                click.echo(f"Result: {json.dumps(state.result, sort_keys=True)}")
            click.echo("Payload:")
            click.echo(json.dumps(state.job.payload, indent=2, sort_keys=True))
            if events:
                click.echo("Lifecycle:")
                _echo_table(
                    ["Position", "Type", "Attempt", "Timestamp", "Error"],
                    [
                        [
                            event["position"],
                            event.get("type", ""),
                            event.get("attempt", 0),
                            event.get("timestamp", ""),
                            event.get("error") or "",
                        ]
                        for event in events
                    ],
                )
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_group.group("dlq")
def workers_dlq():
    """Inspect and operate on worker dead-letter entries."""


@workers_dlq.command("list")
@click.option("--queue", default=None, help="Filter by queue.")
@click.option("--job-type", default=None, help="Filter by job type.")
@click.option("--cause", default=None, help="Filter by cause.")
@click.option("--state", "dlq_state", default="open", help="Filter by DLQ state.")
@click.option("--exception-type", default=None, help="Filter by exception type.")
@click.option("--since", default=None, help="Filter entries created after an ISO time or duration.")
@click.option("--until", default=None, help="Filter entries created before an ISO time or duration.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_dlq_list(
    queue,
    job_type,
    cause,
    dlq_state,
    exception_type,
    since,
    until,
    as_json,
    allow_memory_backends,
):
    """List dead-letter entries."""
    import asyncio

    from skrift.config import get_settings

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            filters = _dlq_filters(
                queue=queue,
                job_type=job_type,
                cause=cause,
                state=dlq_state,
                exception_type=exception_type,
                since=since,
                until=until,
            )
            entries = await runtime.inspect_dlq(**filters)
            if as_json:
                click.echo(json.dumps([_dlq_entry_dict(entry) for entry in entries], indent=2, sort_keys=True))
                return
            _echo_table(
                ["Entry", "Job", "Queue", "Type", "Cause", "State", "Attempts", "Updated"],
                [
                    [
                        entry.id[:12],
                        entry.job.id[:12],
                        entry.queue,
                        entry.job_type,
                        entry.cause.value,
                        entry.state.value,
                        len(entry.attempts),
                        entry.updated_at.isoformat(),
                    ]
                    for entry in entries
                ],
            )
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_dlq.command("inspect")
@click.argument("entry_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_dlq_inspect(entry_id, as_json, allow_memory_backends):
    """Inspect one dead-letter entry."""
    import asyncio

    from skrift.config import get_settings

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            entry = await runtime.get_dlq_entry(entry_id)
            if entry is None:
                raise click.ClickException(f"Unknown DLQ entry id {entry_id!r}")
            payload = _dlq_entry_dict(entry)
            if as_json:
                click.echo(json.dumps(payload, indent=2, sort_keys=True))
                return
            click.echo(f"Entry: {entry.id}")
            click.echo(f"Job: {entry.job.id}")
            click.echo(f"Queue: {entry.queue}")
            click.echo(f"Type: {entry.job_type}")
            click.echo(f"Cause: {entry.cause.value}")
            click.echo(f"State: {entry.state.value}")
            click.echo(f"Latest error: {entry.latest_error}")
            click.echo("Payload:")
            click.echo(json.dumps(entry.job.payload, indent=2, sort_keys=True))
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_dlq.command("retry")
@click.argument("entry_ids", nargs=-1)
@click.option("--queue", default=None, help="Filter by queue.")
@click.option("--job-type", default=None, help="Filter by job type.")
@click.option("--cause", default=None, help="Filter by cause.")
@click.option("--state", "dlq_state", default=None, help="Filter by DLQ state.")
@click.option("--exception-type", default=None, help="Filter by exception type.")
@click.option("--since", default=None, help="Filter entries created after an ISO time or duration.")
@click.option("--until", default=None, help="Filter entries created before an ISO time or duration.")
@click.option("--force", is_flag=True, help="Replay permanent/poison failures.")
@click.option("--dry-run", is_flag=True, help="Show matched entries without replaying them.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_dlq_retry(
    entry_ids,
    queue,
    job_type,
    cause,
    dlq_state,
    exception_type,
    since,
    until,
    force,
    dry_run,
    as_json,
    allow_memory_backends,
):
    """Replay dead-letter entries."""
    import asyncio

    from skrift.config import get_settings

    has_filters = _has_dlq_filters(
        queue=queue,
        job_type=job_type,
        cause=cause,
        state=dlq_state,
        exception_type=exception_type,
        since=since,
        until=until,
    )
    if entry_ids and has_filters:
        raise click.ClickException("Pass either ENTRY_ID values or filters, not both.")
    if not entry_ids and not has_filters:
        raise click.ClickException("Pass at least one ENTRY_ID or a DLQ filter.")
    filters = _dlq_filters(
        queue=queue,
        job_type=job_type,
        cause=cause,
        state=dlq_state or "open",
        exception_type=exception_type,
        since=since,
        until=until,
    )

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            selected = await _select_dlq_entry_ids(runtime, entry_ids, filters)
            summary = {
                "matched": selected,
                "changed": [],
                "skipped": selected if dry_run else [],
                "errors": [],
            }
            if not dry_run:
                for entry_id in selected:
                    try:
                        handle = await runtime.retry_dlq_entry(entry_id, force=force)
                    except Exception as exc:  # noqa: BLE001
                        summary["errors"].append({
                            "entry_id": entry_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                    else:
                        summary["changed"].append({
                            "entry_id": entry_id,
                            "job_id": handle.id,
                        })
            if as_json:
                click.echo(json.dumps(summary, indent=2, sort_keys=True))
                if summary["errors"]:
                    click.get_current_context().exit(1)
                return
            if dry_run:
                click.echo(f"Matched {len(selected)} dead-letter entr{'y' if len(selected) == 1 else 'ies'}.")
                for entry_id in selected:
                    click.echo(entry_id)
                return
            for item in summary["changed"]:
                click.echo(f"Replayed {item['entry_id']} to job {item['job_id']}")
            for item in summary["errors"]:
                click.echo(f"Failed {item['entry_id']}: {item['error']}", err=True)
            if summary["errors"]:
                click.get_current_context().exit(1)
            if not summary["changed"] and not summary["errors"]:
                click.echo("No dead-letter entries matched.")
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_dlq.command("discard")
@click.argument("entry_ids", nargs=-1)
@click.option("--queue", default=None, help="Filter by queue.")
@click.option("--job-type", default=None, help="Filter by job type.")
@click.option("--cause", default=None, help="Filter by cause.")
@click.option("--state", "dlq_state", default=None, help="Filter by DLQ state.")
@click.option("--exception-type", default=None, help="Filter by exception type.")
@click.option("--since", default=None, help="Filter entries created after an ISO time or duration.")
@click.option("--until", default=None, help="Filter entries created before an ISO time or duration.")
@click.option("--reason", default=None, help="Discard reason.")
@click.option("--dry-run", is_flag=True, help="Show matched entries without discarding them.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_dlq_discard(
    entry_ids,
    queue,
    job_type,
    cause,
    dlq_state,
    exception_type,
    since,
    until,
    reason,
    dry_run,
    as_json,
    allow_memory_backends,
):
    """Discard dead-letter entries."""
    import asyncio

    from skrift.config import get_settings

    has_filters = _has_dlq_filters(
        queue=queue,
        job_type=job_type,
        cause=cause,
        state=dlq_state,
        exception_type=exception_type,
        since=since,
        until=until,
    )
    if entry_ids and has_filters:
        raise click.ClickException("Pass either ENTRY_ID values or filters, not both.")
    if not entry_ids and not has_filters:
        raise click.ClickException("Pass at least one ENTRY_ID or a DLQ filter.")
    filters = _dlq_filters(
        queue=queue,
        job_type=job_type,
        cause=cause,
        state=dlq_state or "open",
        exception_type=exception_type,
        since=since,
        until=until,
    )

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            selected = await _select_dlq_entry_ids(runtime, entry_ids, filters)
            summary = {
                "matched": selected,
                "changed": [],
                "skipped": selected if dry_run else [],
                "errors": [],
            }
            if not dry_run:
                for entry_id in selected:
                    try:
                        entry = await runtime.discard_dlq_entry(entry_id, reason=reason)
                    except Exception as exc:  # noqa: BLE001
                        summary["errors"].append({
                            "entry_id": entry_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                    else:
                        summary["changed"].append({"entry_id": entry.id})
            if as_json:
                click.echo(json.dumps(summary, indent=2, sort_keys=True))
                if summary["errors"]:
                    click.get_current_context().exit(1)
                return
            if dry_run:
                click.echo(f"Matched {len(selected)} dead-letter entr{'y' if len(selected) == 1 else 'ies'}.")
                for entry_id in selected:
                    click.echo(entry_id)
                return
            for item in summary["changed"]:
                click.echo(f"Discarded {item['entry_id']}")
            for item in summary["errors"]:
                click.echo(f"Failed {item['entry_id']}: {item['error']}", err=True)
            if summary["errors"]:
                click.get_current_context().exit(1)
            if not summary["changed"] and not summary["errors"]:
                click.echo("No dead-letter entries matched.")
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@workers_dlq.command("export")
@click.option("--queue", default=None, help="Filter by queue.")
@click.option("--job-type", default=None, help="Filter by job type.")
@click.option("--cause", default=None, help="Filter by cause.")
@click.option("--state", "dlq_state", default=None, help="Filter by DLQ state.")
@click.option("--exception-type", default=None, help="Filter by exception type.")
@click.option("--since", default=None, help="Filter entries created after an ISO time or duration.")
@click.option("--until", default=None, help="Filter entries created before an ISO time or duration.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def workers_dlq_export(
    queue,
    job_type,
    cause,
    dlq_state,
    exception_type,
    since,
    until,
    allow_memory_backends,
):
    """Export dead-letter entries as JSON."""
    import asyncio

    from skrift.config import get_settings

    settings = get_settings()
    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)

    async def _run():
        db_config = _build_db_config(settings)
        try:
            runtime = _configure_worker_runtime(
                settings,
                session_maker=db_config.get_session,
                queues=tuple(settings.workers.queues),
                concurrency=settings.workers.concurrency,
                mode=settings.workers.execution,
            )
            filters = _dlq_filters(
                queue=queue or None,
                job_type=job_type or None,
                cause=cause or None,
                state=dlq_state or None,
                exception_type=exception_type,
                since=since,
                until=until,
            )
            entries = await runtime.inspect_dlq(**filters)
            click.echo(json.dumps([_dlq_entry_dict(entry) for entry in entries], indent=2, sort_keys=True))
        finally:
            await db_config.get_engine().dispose()

    asyncio.run(_run())


@cli.command()
@click.option(
    "--write",
    type=click.Path(),
    default=None,
    help="Write SECRET_KEY to a .env file",
)
@click.option(
    "--format",
    "fmt",
    default="urlsafe",
    type=click.Choice(["urlsafe", "hex", "base64"]),
    help="Output format for the secret key",
)
@click.option("--length", default=32, type=int, help="Number of random bytes")
def secret(write, fmt, length):
    """Generate a secure secret key."""
    # Generate key based on format
    if fmt == "urlsafe":
        key = secrets.token_urlsafe(length)
    elif fmt == "hex":
        key = secrets.token_hex(length)
    else:  # base64
        key = base64.b64encode(secrets.token_bytes(length)).decode("ascii")

    if write:
        env_path = Path(write)
        env_content = ""

        # Read existing content if file exists
        if env_path.exists():
            env_content = env_path.read_text()

        # Update or add SECRET_KEY
        secret_key_pattern = re.compile(r"^SECRET_KEY=.*$", re.MULTILINE)
        new_line = f"SECRET_KEY={key}"

        if secret_key_pattern.search(env_content):
            # Replace existing SECRET_KEY
            env_content = secret_key_pattern.sub(new_line, env_content)
        else:
            # Add SECRET_KEY at the end
            if env_content and not env_content.endswith("\n"):
                env_content += "\n"
            env_content += new_line + "\n"

        env_path.write_text(env_content)
        click.echo(f"SECRET_KEY written to {env_path}")
    else:
        click.echo(key)


def _db_init(project_root: Path) -> None:
    """Initialize a user migrations directory with versions/ and script.py.mako."""
    import shutil

    migrations_dir = project_root / "migrations" / "versions"
    migrations_dir.mkdir(parents=True, exist_ok=True)

    # Copy the Mako template from the Skrift package
    skrift_dir = Path(__file__).parent
    src_mako = skrift_dir / "alembic" / "script.py.mako"
    dst_mako = project_root / "migrations" / "script.py.mako"

    if not dst_mako.exists() and src_mako.exists():
        shutil.copy2(src_mako, dst_mako)

    click.echo(f"Initialized migrations directory at {migrations_dir}")
    click.echo(f"Template at {dst_mako}")


def _run_alembic(project_root: Path, args: list[str]) -> None:
    """Build an Alembic Config programmatically and run the given command."""
    from alembic.config import Config, CommandLine

    skrift_dir = Path(__file__).parent

    # Find alembic.ini
    alembic_ini = project_root / "alembic.ini"
    if not alembic_ini.exists():
        alembic_ini = skrift_dir / "alembic.ini"
        if not alembic_ini.exists():
            click.echo("Error: Could not find alembic.ini", err=True)
            sys.exit(1)

    # Build version_locations: user dir first (if it exists), then Skrift's
    skrift_versions = str(skrift_dir / "alembic" / "versions")
    user_versions = project_root / "migrations" / "versions"
    if user_versions.is_dir():
        version_locations = os.pathsep.join([str(user_versions), skrift_versions])
    else:
        version_locations = skrift_versions

    # Rewrite "upgrade head" → "upgrade heads" when multiple locations exist
    if user_versions.is_dir() and len(args) >= 2:
        if args[0] == "upgrade" and args[1] == "head":
            args = list(args)
            args[1] = "heads"
        elif args[0] == "downgrade" and args[1] == "base":
            args = list(args)
            args[1] = "base"

    # Build Config and inject version_locations
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("version_path_separator", "os")
    cfg.set_main_option("version_locations", version_locations)

    # Parse and run through CommandLine for proper subcommand dispatch
    cmd = CommandLine()
    options = cmd.parser.parse_args(args)
    if not hasattr(options, "cmd"):
        cmd.parser.error("too few arguments")
    else:
        cfg.cmd_opts = options
        fn, positional, kwarg = options.cmd
        fn(
            cfg,
            *[getattr(options, k, None) for k in positional],
            **{k: getattr(options, k, None) for k in kwarg},
        )


@cli.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.pass_context
def db(ctx):
    """Run database migrations via Alembic.

    \b
    Examples:
        skrift db init             # Initialize user migrations directory
        skrift db upgrade heads    # Apply all migrations
        skrift db downgrade -1     # Rollback one migration
        skrift db current          # Show current revision
        skrift db history          # Show migration history
        skrift db revision -m "description" --autogenerate  # Create new migration
    """
    # Always run from the project root (where app.yaml and .env are)
    project_root = Path.cwd()
    if not (project_root / "app.yaml").exists():
        project_root = Path(__file__).parent.parent
    os.chdir(project_root)

    args = ctx.args

    # Intercept "init" as a custom subcommand
    if args and args[0] == "init":
        _db_init(project_root)
        return

    if not args:
        click.echo(ctx.get_help())
        return

    _run_alembic(project_root, args)


def _configure_agents_cli_runtime(settings, *, allow_memory_backends: bool):
    from skrift.agents.config import configure_agent_runtime

    _validate_worker_process_backends(
        settings,
        allow_memory_backends=allow_memory_backends,
        context="inspect",
    )
    _import_worker_modules(settings)
    configure_agent_runtime(settings.agents)
    db_config = _build_db_config(settings)
    return _configure_worker_runtime(
        settings,
        session_maker=db_config.get_session,
        queues=tuple(settings.workers.queues),
        concurrency=settings.workers.concurrency,
        mode=settings.workers.execution,
    )


@cli.group("agents")
def agents_group():
    """Inspect and operate durable agent sessions."""
    pass


@agents_group.command("list")
def agents_list():
    """List registered agents."""
    from skrift.config import get_settings
    from skrift.agents.registry import registry as agent_registry

    settings = get_settings()
    _import_worker_modules(settings)
    rows = [[item.name, len(item.tool_policies)] for item in agent_registry.list()]
    if rows:
        _echo_table(["Agent", "Tools"], rows)
    else:
        click.echo("No agents registered.")


@agents_group.command("trace")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def agents_trace(session_id, as_json, allow_memory_backends):
    """Show agent session events."""
    import asyncio
    from skrift.config import get_settings
    from skrift.agents.state import stream_name

    async def _run():
        runtime = _configure_agents_cli_runtime(
            get_settings(),
            allow_memory_backends=allow_memory_backends,
        )
        rows = await runtime.event_log.read(stream_name(session_id))
        payload = [{"position": position, **event} for position, event in rows]
        if as_json:
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        _echo_table(
            ["Pos", "Seq", "Type", "Timestamp"],
            [
                [item["position"], item.get("seq", ""), item.get("type", ""), item.get("ts", "")]
                for item in payload
            ],
        )

    asyncio.run(_run())


@agents_group.command("replay")
@click.argument("session_id")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def agents_replay(session_id, allow_memory_backends):
    """Replay an agent session event stream."""
    import asyncio
    from skrift.config import get_settings
    from skrift.agents import replay

    async def _run():
        _configure_agents_cli_runtime(get_settings(), allow_memory_backends=allow_memory_backends)
        click.echo(json.dumps(await replay(session_id), indent=2, sort_keys=True))

    asyncio.run(_run())


@agents_group.group("audit")
def agents_audit():
    """Export agent audit trails."""
    pass


@agents_audit.command("export")
@click.argument("session_id")
@click.option("--lineage/--no-lineage", default=True, help="Include lineage metadata.")
@click.option("--format", "output_format", type=click.Choice(["flat", "nested"]), default="flat")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, resolve_path=True))
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def agents_audit_export(session_id, lineage, output_format, out_path, allow_memory_backends):
    """Export a full agent audit trail."""
    import asyncio
    from skrift.config import get_settings
    from skrift.agents import audit_export

    async def _run():
        _configure_agents_cli_runtime(get_settings(), allow_memory_backends=allow_memory_backends)
        payload = (await audit_export(session_id, include_lineage=lineage, format=output_format)).model_dump(mode="json")
        text = json.dumps(payload, indent=2, sort_keys=True)
        if out_path:
            Path(out_path).write_text(text)
        else:
            click.echo(text)

    asyncio.run(_run())


@agents_group.group("sessions")
def agents_sessions():
    """Inspect and mutate agent sessions."""
    pass


@agents_sessions.command("inspect")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def agents_sessions_inspect(session_id, as_json, allow_memory_backends):
    """Inspect an agent session RunState."""
    import asyncio
    from skrift.config import get_settings
    from skrift.agents.state import load_runstate

    async def _run():
        _configure_agents_cli_runtime(get_settings(), allow_memory_backends=allow_memory_backends)
        state = await load_runstate(session_id)
        if state is None:
            raise click.ClickException(f"Unknown agent session {session_id!r}")
        payload = state.model_dump(mode="json")
        if as_json:
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        click.echo(f"Session: {state.session_id}")
        click.echo(f"Agent: {state.agent_name}")
        click.echo(f"Status: {state.status}")
        click.echo(f"Version: {state.version}")
        click.echo(f"Current job: {state.current_run_job_id or ''}")

    asyncio.run(_run())


def _cli_actor(value: str | None):
    return {"kind": "service", "id": value or f"cli:{os.environ.get('USER', 'unknown')}"}


def _session_mutation(command_name: str, session_id: str, actor: str | None, message: str | None, allow_memory_backends: bool):
    import asyncio
    from skrift.config import get_settings
    from skrift.agents import session as get_session

    async def _run():
        _configure_agents_cli_runtime(get_settings(), allow_memory_backends=allow_memory_backends)
        handle = await get_session(session_id)
        if command_name == "cancel":
            await handle.cancel(actor=_cli_actor(actor))
        elif command_name == "pause":
            await handle.pause(actor=_cli_actor(actor))
        elif command_name == "resume":
            await handle.resume(actor=_cli_actor(actor))
        elif command_name == "steer":
            await handle.steer(message or "", actor=_cli_actor(actor))
        click.echo(f"{command_name} recorded for {session_id}")

    asyncio.run(_run())


@agents_sessions.command("cancel")
@click.argument("session_id")
@click.option("--actor", default=None)
@click.option("--allow-memory-backends", is_flag=True)
def agents_sessions_cancel(session_id, actor, allow_memory_backends):
    """Cancel an agent session."""
    _session_mutation("cancel", session_id, actor, None, allow_memory_backends)


@agents_sessions.command("pause")
@click.argument("session_id")
@click.option("--actor", default=None)
@click.option("--allow-memory-backends", is_flag=True)
def agents_sessions_pause(session_id, actor, allow_memory_backends):
    """Pause an agent session."""
    _session_mutation("pause", session_id, actor, None, allow_memory_backends)


@agents_sessions.command("resume")
@click.argument("session_id")
@click.option("--actor", default=None)
@click.option("--allow-memory-backends", is_flag=True)
def agents_sessions_resume(session_id, actor, allow_memory_backends):
    """Resume an agent session."""
    _session_mutation("resume", session_id, actor, None, allow_memory_backends)


@agents_sessions.command("steer")
@click.argument("session_id")
@click.option("--message", required=True)
@click.option("--actor", default=None)
@click.option("--allow-memory-backends", is_flag=True)
def agents_sessions_steer(session_id, message, actor, allow_memory_backends):
    """Inject steering text into an agent session."""
    _session_mutation("steer", session_id, actor, message, allow_memory_backends)


@agents_group.command("drain")
@click.argument("session_id", required=False)
@click.option("--allow-memory-backends", is_flag=True, help="Allow process-local backends.")
def agents_drain(session_id, allow_memory_backends):
    """Drain one agent session outbox, or all pending outboxes."""
    import asyncio
    from skrift.config import get_settings
    from skrift.agents.state import drain_outbox, drain_pending_outboxes

    async def _run():
        _configure_agents_cli_runtime(get_settings(), allow_memory_backends=allow_memory_backends)
        if session_id:
            await drain_outbox(session_id)
            click.echo(f"Drained {session_id}")
            return
        drained = await drain_pending_outboxes()
        click.echo(f"Drained {len(drained)} session(s)")

    asyncio.run(_run())


@cli.group()
def claude():
    """Manage Claude Code skills for Skrift development."""
    pass


cli.add_command(claude)


def _get_skill_names():
    """Discover skill subdirectories from the package."""
    import importlib.resources

    package_files = importlib.resources.files("skrift.claude_skill")
    names = []
    for item in package_files.iterdir():
        if item.is_dir() and item.joinpath("SKILL.md").is_file():
            names.append(item.name)
    names.sort()
    return names


def _find_installed_skills(skills_base: Path, skill_names: list[str]) -> list[str]:
    """Return skill names that already exist on disk."""
    return [n for n in skill_names if (skills_base / n).exists()]


def _install_skills(skills_base: Path, skill_names: list[str]) -> list[str]:
    """Copy skill files from the package to disk. Returns installed names."""
    import importlib.resources

    package_files = importlib.resources.files("skrift.claude_skill")
    installed = []
    for skill_name in skill_names:
        skill_dir = skills_base / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        source = package_files.joinpath(skill_name, "SKILL.md")
        dest = skill_dir / "SKILL.md"

        content = source.read_text()
        dest.write_text(content)
        click.echo(f"  {dest.relative_to(Path.cwd())}")
        installed.append(skill_name)
    return installed


def _remove_skills(skills_base: Path, skill_names: list[str]) -> list[str]:
    """Remove installed skill directories. Returns removed names."""
    import shutil

    removed = []
    for skill_name in skill_names:
        skill_dir = skills_base / skill_name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            click.echo(f"  Removed {skill_dir.relative_to(Path.cwd())}")
            removed.append(skill_name)
    return removed


@claude.command()
def install():
    """Install Skrift skills for Claude Code.

    Fails if any Skrift skills are already installed.
    Use `skrift claude update` to replace existing skills.
    """
    skills_base = Path.cwd() / ".claude" / "skills"
    skill_names = _get_skill_names()

    if not skill_names:
        click.echo("Error: No skill directories found in package.", err=True)
        sys.exit(1)

    existing = _find_installed_skills(skills_base, skill_names)
    if existing:
        click.echo(f"Skrift skills already installed: {', '.join(existing)}", err=True)
        click.echo("Use `skrift claude update` to replace them.", err=True)
        sys.exit(1)

    installed = _install_skills(skills_base, skill_names)
    click.echo(f"\nInstalled {len(installed)} skills. Use /skrift to activate.")


@claude.command()
def remove():
    """Remove all installed Skrift skills."""
    skills_base = Path.cwd() / ".claude" / "skills"
    skill_names = _get_skill_names()

    existing = _find_installed_skills(skills_base, skill_names)
    if not existing:
        click.echo("No Skrift skills found to remove.")
        return

    removed = _remove_skills(skills_base, existing)
    click.echo(f"\nRemoved {len(removed)} skills.")


@claude.command()
def update():
    """Update Skrift skills to the latest version.

    Removes existing Skrift skills and installs fresh copies.
    """
    skills_base = Path.cwd() / ".claude" / "skills"
    skill_names = _get_skill_names()

    if not skill_names:
        click.echo("Error: No skill directories found in package.", err=True)
        sys.exit(1)

    existing = _find_installed_skills(skills_base, skill_names)
    if existing:
        _remove_skills(skills_base, existing)

    installed = _install_skills(skills_base, skill_names)
    click.echo(f"\nUpdated {len(installed)} skills. Use /skrift to activate.")


@cli.group("storage")
def storage_group():
    """Manage asset storage backends."""
    pass


@storage_group.command("stores")
def storage_stores():
    """List configured storage stores."""
    from skrift.config import get_settings

    settings = get_settings()
    default = settings.storage.default

    for name, cfg in settings.storage.stores.items():
        marker = " (default)" if name == default else ""
        if cfg.backend == "local":
            detail = f"path={cfg.local_path}"
        elif cfg.backend == "s3":
            detail = f"bucket={cfg.s3.bucket} region={cfg.s3.region}"
        else:
            detail = f"backend={cfg.backend}"
        max_mb = cfg.max_upload_size / 1_048_576
        click.echo(f"  {name}: {cfg.backend} ({detail}, max={max_mb:.0f}MB){marker}")


@storage_group.command("ls")
@click.option("--store", default=None, help="Store name (default: all stores)")
@click.option("--prefix", default=None, help="Filter by folder prefix")
@click.option("--limit", default=100, type=int, help="Max results")
def storage_ls(store, prefix, limit):
    """List assets in the database."""
    import asyncio

    from skrift.config import get_settings
    from skrift.db.services.asset_service import list_assets

    settings = get_settings()

    async def _run():
        from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, AsyncSessionConfig
        from advanced_alchemy.config import EngineConfig

        db_config = SQLAlchemyAsyncConfig(
            connection_string=settings.db.url,
            session_config=AsyncSessionConfig(expire_on_commit=False),
            engine_config=EngineConfig(echo=False),
        )
        async with db_config.get_session() as session:
            assets = await list_assets(
                session,
                store=store,
                folder=prefix,
                limit=limit,
            )
            if not assets:
                click.echo("No assets found.")
                return
            for a in assets:
                size_kb = a.size / 1024
                click.echo(f"  {a.key}  {a.filename}  {size_kb:.1f}KB  {a.content_type}  store={a.store}")

    asyncio.run(_run())


@storage_group.command("orphans")
@click.option("--store", default=None, help="Store name (default: default store)")
@click.option("--delete", "do_delete", is_flag=True, help="Delete orphaned files from backend")
def storage_orphans(store, do_delete):
    """Find files in backends that have no matching Asset row."""
    import asyncio

    from skrift.config import get_settings
    from skrift.lib.storage import StorageManager

    settings = get_settings()

    async def _run():
        from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, AsyncSessionConfig
        from advanced_alchemy.config import EngineConfig
        from sqlalchemy import select
        from skrift.db.models.asset import Asset

        manager = StorageManager(settings.storage)
        store_name = store or manager.default_store

        db_config = SQLAlchemyAsyncConfig(
            connection_string=settings.db.url,
            session_config=AsyncSessionConfig(expire_on_commit=False),
            engine_config=EngineConfig(echo=False),
        )

        backend = await manager.get(store_name)

        async with db_config.get_session() as session:
            result = await session.execute(
                select(Asset.key).where(Asset.store == store_name)
            )
            db_keys = {row[0] for row in result.all()}

        orphans = []
        async for key in backend.list_keys():
            if key not in db_keys:
                orphans.append(key)

        if not orphans:
            click.echo("No orphaned files found.")
            return

        click.echo(f"Found {len(orphans)} orphaned file(s) in store '{store_name}':")
        for key in orphans:
            click.echo(f"  {key}")

        if do_delete:
            for key in orphans:
                await backend.delete(key)
            click.echo(f"Deleted {len(orphans)} orphaned file(s).")

        await manager.close()

    asyncio.run(_run())


@storage_group.command("sync")
@click.option("--source", required=True, type=click.Path(exists=True), help="Source directory")
@click.option("--store", default=None, help="Target store name")
@click.option("--dry-run", is_flag=True, help="Show what would be uploaded")
@click.option("--delete", "do_delete", is_flag=True, help="Remove orphaned files from backend")
def storage_sync(source, store, dry_run, do_delete):
    """Sync a local directory to a storage backend."""
    import asyncio
    import hashlib

    from skrift.config import get_settings
    from skrift.lib.storage import StorageManager

    settings = get_settings()
    source_path = Path(source)

    async def _run():
        from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, AsyncSessionConfig
        from advanced_alchemy.config import EngineConfig
        from sqlalchemy import select, and_
        from skrift.db.models.asset import Asset
        import mimetypes

        manager = StorageManager(settings.storage)
        store_name = store or manager.default_store
        backend = await manager.get(store_name)

        db_config = SQLAlchemyAsyncConfig(
            connection_string=settings.db.url,
            session_config=AsyncSessionConfig(expire_on_commit=False),
            engine_config=EngineConfig(echo=False),
        )

        files = [p for p in source_path.rglob("*") if p.is_file()]
        uploaded = 0

        async with db_config.get_session() as session:
            for file_path in files:
                data = file_path.read_bytes()
                content_hash = hashlib.sha256(data).hexdigest()

                # Check if already uploaded
                existing = await session.execute(
                    select(Asset).where(
                        and_(Asset.store == store_name, Asset.content_hash == content_hash)
                    ).limit(1)
                )
                if existing.scalar_one_or_none():
                    continue

                content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                relative = str(file_path.relative_to(source_path))

                if dry_run:
                    click.echo(f"  Would upload: {relative} ({len(data)} bytes)")
                    uploaded += 1
                    continue

                key = content_hash
                await backend.put(key, data, content_type)
                asset = Asset(
                    key=key,
                    store=store_name,
                    content_hash=content_hash,
                    filename=file_path.name,
                    content_type=content_type,
                    size=len(data),
                    folder=str(file_path.parent.relative_to(source_path)) if file_path.parent != source_path else "",
                )
                session.add(asset)
                uploaded += 1

            if not dry_run:
                await session.commit()

        action = "Would upload" if dry_run else "Uploaded"
        click.echo(f"{action} {uploaded} file(s) to store '{store_name}'.")
        await manager.close()

    asyncio.run(_run())


@storage_group.command("migrate")
@click.option("--from", "from_store", required=True, help="Source store name")
@click.option("--to", "to_store", required=True, help="Destination store name")
@click.option("--dry-run", is_flag=True, help="Show what would be migrated")
def storage_migrate(from_store, to_store, dry_run):
    """Migrate assets from one store to another."""
    import asyncio

    from skrift.config import get_settings
    from skrift.lib.storage import StorageManager

    settings = get_settings()

    async def _run():
        from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig, AsyncSessionConfig
        from advanced_alchemy.config import EngineConfig
        from sqlalchemy import select, and_
        from skrift.db.models.asset import Asset

        manager = StorageManager(settings.storage)
        source = await manager.get(from_store)
        dest = await manager.get(to_store)

        db_config = SQLAlchemyAsyncConfig(
            connection_string=settings.db.url,
            session_config=AsyncSessionConfig(expire_on_commit=False),
            engine_config=EngineConfig(echo=False),
        )

        migrated = 0
        async with db_config.get_session() as session:
            result = await session.execute(
                select(Asset).where(Asset.store == from_store)
            )
            assets = list(result.scalars().all())

            if not assets:
                click.echo(f"No assets found in store '{from_store}'.")
                return

            for asset in assets:
                # Check if content already exists in dest
                existing = await session.execute(
                    select(Asset).where(
                        and_(Asset.store == to_store, Asset.content_hash == asset.content_hash)
                    ).limit(1)
                )

                if dry_run:
                    click.echo(f"  Would migrate: {asset.filename} ({asset.size} bytes)")
                    migrated += 1
                    continue

                if not existing.scalar_one_or_none():
                    data = await source.get(asset.key)
                    await dest.put(asset.key, data, asset.content_type)

                asset.store = to_store
                migrated += 1

            if not dry_run:
                await session.commit()

        action = "Would migrate" if dry_run else "Migrated"
        click.echo(f"{action} {migrated} asset(s) from '{from_store}' to '{to_store}'.")
        await manager.close()

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
