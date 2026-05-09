"""Worker observer admin controller."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import timedelta
from typing import Annotated, Any

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Redirect, Response
from litestar.response import Template as TemplateResponse
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.lib.flash import flash_error, flash_success, get_flash_messages
from skrift.workers import (
    LIFECYCLE_STREAM,
    DeadLetterCause,
    DeadLetterState,
    get_runtime,
)
from skrift.workers.models import utcnow


WORKER_DASHBOARD_JOB_LIMIT = 100
WORKER_DASHBOARD_EVENT_LIMIT = 25


def _serialize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert a worker inspection snapshot to JSON-safe admin data."""
    return {
        "mode": snapshot["mode"],
        "concurrency": snapshot["concurrency"],
        "queues": [
            {
                "queue": queue.queue,
                "ready": queue.ready,
                "delayed": queue.delayed,
                "claimed": queue.claimed,
                "dead_lettered": queue.dead_lettered,
                "oldest_ready_age_seconds": round(queue.oldest_ready_age_seconds, 3),
            }
            for queue in snapshot["queues"]
        ],
        "queue_trend_history": snapshot.get("queue_trend_history", []),
        "queue_wait_history": snapshot["queue_wait_history"],
        "queue_wait_bucket_seconds": snapshot.get("queue_wait_bucket_seconds", 900),
        "completed_history": snapshot.get("completed_history", []),
        "dlq": _serialize_dlq_summary(snapshot["dlq"]),
        "jobs_total": snapshot.get("jobs_total", len(snapshot["jobs"])),
        "jobs_active_total": snapshot.get("jobs_active_total", 0),
        "jobs_limit": snapshot.get("jobs_limit"),
        "jobs": [
            {
                "id": state.job.id,
                "short_id": state.job.id[:12],
                "type": state.job.type,
                "queue": state.job.queue,
                "status": state.status.value,
                "attempt": state.attempt,
                "max_attempts": state.job.max_attempts,
                "updated_at": state.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
                "error": state.error or state.last_error or "",
            }
            for state in snapshot["jobs"]
        ],
        "handlers": [
            {
                "job_type": handler.job_type,
                "payload": handler.payload_model.__name__,
                "queue": handler.queue,
                "max_attempts": handler.retry_policy.max_attempts,
                "visibility_timeout": int(handler.visibility_timeout),
            }
            for handler in snapshot["handlers"]
        ],
        "events": [
            _serialize_event(position, event)
            for position, event in snapshot["events"]
        ],
    }


def _serialize_dlq_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "open": summary.get("open", 0),
        "last_hour": summary.get("last_hour", 0),
        "top_cause": summary.get("top_cause", ""),
        "top_cause_count": summary.get("top_cause_count", 0),
    }


def _serialize_dlq_entry(entry) -> dict[str, Any]:
    latest_attempt = entry.attempts[-1] if entry.attempts else None
    return {
        "id": entry.id,
        "short_id": entry.id[:12],
        "job_id": entry.job.id,
        "short_job_id": entry.job.id[:12],
        "queue": entry.queue,
        "job_type": entry.job_type,
        "cause": entry.cause.value,
        "state": entry.state.value,
        "attempts": len(entry.attempts),
        "created_at": entry.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": entry.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        "latest_error": entry.latest_error,
        "exception_type": latest_attempt.exception_type if latest_attempt else "",
        "replayed_to_job_id": entry.replayed_to_job_id or "",
        "payload": json.dumps(entry.job.payload, indent=2, sort_keys=True),
        "attempt_history": [
            {
                "attempt": attempt.attempt,
                "started_at": (
                    attempt.started_at.strftime("%Y-%m-%d %H:%M:%S")
                    if attempt.started_at else ""
                ),
                "finished_at": attempt.finished_at.strftime("%Y-%m-%d %H:%M:%S"),
                "worker_id": attempt.worker_id or "",
                "duration_seconds": attempt.duration_seconds,
                "exception_type": attempt.exception_type,
                "error": attempt.error,
                "traceback": attempt.traceback,
            }
            for attempt in entry.attempts
        ],
    }


def _dlq_filters(
    *,
    queue: str | None = None,
    job_type: str | None = None,
    cause: str | None = None,
    state: str | None = None,
    exception_type: str | None = None,
    hours: int | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "queue": queue or None,
        "job_type": job_type or None,
        "cause": cause or None,
        "state": state or None,
        "exception_type": exception_type or None,
    }
    if hours:
        filters["created_after"] = utcnow() - timedelta(hours=hours)
    return filters


def _bulk_message(verb: str, count: int) -> str:
    noun = "entry" if count == 1 else "entries"
    return f"{verb} {count} DLQ {noun}."


def _serialize_event(position: int, event: dict[str, Any]) -> dict[str, Any]:
    """Convert one lifecycle event log row to JSON-safe admin data."""
    return {
        "position": position,
        "type": event.get("type", ""),
        "job_id": event.get("job_id", ""),
        "short_job_id": event.get("job_id", "")[:12],
        "queue": event.get("queue", ""),
        "job_type": event.get("job_type", ""),
        "attempt": event.get("attempt", 0),
        "timestamp": event.get("timestamp", ""),
        "error": event.get("error") or "",
    }


class WorkersAdminController(Controller):
    """Read-only observer for the current local worker runtime."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/workers",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Workers", "icon": "activity", "order": 92},
    )
    async def workers(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show worker queues, jobs, handlers, and lifecycle events."""
        ctx = await get_admin_context(request, db_session)
        snapshot = await get_runtime().inspect(
            job_limit=WORKER_DASHBOARD_JOB_LIMIT,
            event_limit=WORKER_DASHBOARD_EVENT_LIMIT,
        )
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/workers.html",
            context={
                "flash_messages": flash_messages,
                "snapshot": snapshot,
                **ctx,
            },
        )

    @get(
        "/workers/dlq",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Worker DLQ", "icon": "alert-triangle", "order": 93},
    )
    async def dlq(
        self,
        request: Request,
        db_session: AsyncSession,
        queue: str | None = None,
        job_type: str | None = None,
        cause: str | None = None,
        dlq_state: Annotated[str | None, Parameter(query="state")] = "open",
        exception_type: str | None = None,
        hours: int | None = None,
    ) -> TemplateResponse:
        """Inspect and filter worker DLQ entries."""
        ctx = await get_admin_context(request, db_session)
        runtime = get_runtime()
        filters = _dlq_filters(
            queue=queue,
            job_type=job_type,
            cause=cause,
            state=dlq_state,
            exception_type=exception_type,
            hours=hours,
        )
        entries = await runtime.inspect_dlq(**filters)
        all_entries = await runtime.inspect_dlq()
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/workers_dlq.html",
            context={
                "flash_messages": flash_messages,
                "entries": [_serialize_dlq_entry(entry) for entry in entries],
                "filters": {
                    "queue": queue or "",
                    "job_type": job_type or "",
                    "cause": cause or "",
                    "state": dlq_state or "",
                    "exception_type": exception_type or "",
                    "hours": hours or "",
                },
                "queues": sorted({entry.queue for entry in all_entries}),
                "job_types": sorted({entry.job_type for entry in all_entries}),
                "exception_types": sorted(
                    {
                        attempt.exception_type
                        for entry in all_entries
                        for attempt in entry.attempts
                        if attempt.exception_type
                    }
                ),
                "causes": [cause.value for cause in DeadLetterCause],
                "states": [state.value for state in DeadLetterState],
                **ctx,
            },
        )

    @get(
        "/workers/dlq/export",
        guards=[auth_guard, Permission("administrator")],
    )
    async def dlq_export(
        self,
        queue: str | None = None,
        job_type: str | None = None,
        cause: str | None = None,
        dlq_state: Annotated[str | None, Parameter(query="state")] = "open",
        exception_type: str | None = None,
        hours: int | None = None,
    ) -> Response:
        """Export filtered DLQ entries as JSON."""
        filters = _dlq_filters(
            queue=queue,
            job_type=job_type,
            cause=cause,
            state=dlq_state,
            exception_type=exception_type,
            hours=hours,
        )
        entries = await get_runtime().inspect_dlq(**filters)
        return Response(
            content=[entry.model_dump(mode="json") for entry in entries],
            media_type="application/json",
        )

    @get(
        "/workers/dlq/{entry_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def dlq_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        entry_id: str,
    ) -> TemplateResponse:
        """Show one DLQ entry with full forensic detail."""
        ctx = await get_admin_context(request, db_session)
        entry = await get_runtime().get_dlq_entry(entry_id)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/workers_dlq_detail.html",
            context={
                "flash_messages": flash_messages,
                "entry": _serialize_dlq_entry(entry) if entry else None,
                **ctx,
            },
        )

    @post(
        "/workers/dlq/action",
        guards=[auth_guard, Permission("administrator")],
    )
    async def dlq_action(
        self,
        request: Request,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Retry, force retry, or discard selected DLQ entries."""
        runtime = get_runtime()
        action = str(data.get("action", ""))
        ids = [
            key.removeprefix("entry_")
            for key, value in data.items()
            if key.startswith("entry_") and value == "on"
        ]
        if not ids and data.get("entry_id"):
            ids = [str(data["entry_id"])]
        try:
            if action == "retry":
                await runtime.retry_dlq_entries(ids)
                flash_success(request, _bulk_message("Replayed", len(ids)))
            elif action == "force_retry":
                await runtime.retry_dlq_entries(ids, force=True)
                flash_success(request, _bulk_message("Force replayed", len(ids)))
            elif action == "discard":
                reason = str(data.get("reason", "")) or None
                await runtime.discard_dlq_entries(ids, reason=reason)
                flash_success(request, _bulk_message("Discarded", len(ids)))
            else:
                flash_error(request, "Choose a DLQ action.")
        except Exception as exc:  # noqa: BLE001
            flash_error(request, str(exc))
        return Redirect(path=str(data.get("next") or "/admin/workers/dlq"))

    @get(
        "/workers/stream",
        guards=[auth_guard, Permission("administrator")],
    )
    async def stream(self, request: Request) -> ServerSentEvent:
        """Stream worker lifecycle events and fresh observer snapshots."""
        runtime = get_runtime()

        async def snapshot_message() -> ServerSentEventMessage:
            snapshot = _serialize_snapshot(
                await runtime.inspect(
                    job_limit=WORKER_DASHBOARD_JOB_LIMIT,
                    event_limit=WORKER_DASHBOARD_EVENT_LIMIT,
                )
            )
            return ServerSentEventMessage(
                data=json.dumps(snapshot),
                event="workers_snapshot",
            )

        async def generate() -> AsyncGenerator[ServerSentEventMessage, None]:
            read_tail = getattr(runtime.event_log, "read_tail", None)
            if callable(read_tail):
                current_events = await read_tail(LIFECYCLE_STREAM, limit=1)
            else:
                current_events = await runtime.event_log.read(LIFECYCLE_STREAM)
            cursor = current_events[-1][0] + 1 if current_events else 0

            yield await snapshot_message()
            yield ServerSentEventMessage(data="", event="sync")

            subscription = runtime.event_log.subscribe(
                LIFECYCLE_STREAM,
                from_position=cursor,
            )
            next_event = asyncio.create_task(anext(subscription))
            try:
                while True:
                    done, _ = await asyncio.wait({next_event}, timeout=2.0)
                    if not done:
                        yield await snapshot_message()
                        continue

                    position, event = next_event.result()
                    next_event = asyncio.create_task(anext(subscription))
                    yield ServerSentEventMessage(
                        data=json.dumps(_serialize_event(position, event)),
                        event="worker_event",
                    )
                    yield await snapshot_message()
            finally:
                next_event.cancel()
                with suppress(asyncio.CancelledError):
                    await next_event
                await subscription.aclose()

        return ServerSentEvent(generate())
