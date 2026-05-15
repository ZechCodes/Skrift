"""Built-in worker handlers for outbound webhooks."""

from __future__ import annotations

import hmac
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import Field

import skrift
from skrift.config import WebhookProfileConfig
from skrift.db.models.webhook import WebhookDelivery, WebhookDeliveryAttempt
from skrift.webhooks.service import (
    _identity,
    delivery_job_id,
    get_profile,
    prune_retained_deliveries,
    recover_expired_delivery_locks,
    submit_delivery,
    submit_due_deliveries,
)
from skrift.workers import PermanentFailure


class DeliverWebhook(skrift.Job):
    """Worker payload for one webhook delivery attempt."""

    delivery_id: str
    attempt_number: int = Field(ge=1)


class ReconcileWebhooks(skrift.Job):
    """Worker payload for submitting due webhook deliveries."""

    limit: int = Field(default=100, ge=1)
    reschedule: bool = True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _response_preview(response: httpx.Response | None, *, limit: int = 4096) -> str | None:
    if response is None:
        return None
    text = response.text
    return text[:limit]


def _next_delay(profile: WebhookProfileConfig, attempt_number: int) -> float:
    backoff = profile.backoff
    delay = min(
        backoff.max_seconds,
        backoff.initial_seconds * (backoff.factor ** max(0, attempt_number - 1)),
    )
    if backoff.jitter_seconds:
        delay += random.uniform(0, backoff.jitter_seconds)
    return delay


def _deadline_exceeded(delivery: WebhookDelivery, profile: WebhookProfileConfig) -> bool:
    if profile.dead_letter_after_seconds is None:
        return False
    created_at = _coerce_utc(delivery.created_at)
    if created_at is None:
        return False
    return _utcnow() >= created_at + timedelta(seconds=profile.dead_letter_after_seconds)


def _signature(
    *,
    secret: str,
    timestamp: str,
    delivery_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> str:
    body = b".".join(
        [
            timestamp.encode(),
            delivery_id.encode(),
            idempotency_key.encode(),
            _payload_bytes(payload),
        ]
    )
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _headers(
    delivery: WebhookDelivery,
    profile: WebhookProfileConfig,
    *,
    attempt_number: int,
) -> dict[str, str]:
    timestamp = str(int(_utcnow().timestamp()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Skrift-Webhooks/1",
        "Idempotency-Key": delivery.idempotency_key,
        "X-Skrift-Delivery-Id": str(delivery.id),
        "X-Skrift-Profile": delivery.profile,
        "X-Skrift-Event-Type": delivery.event_type,
        "X-Skrift-Attempt": str(attempt_number),
        "X-Skrift-Timestamp": timestamp,
    }
    headers.update(profile.headers)
    if profile.signing_secret:
        headers["X-Skrift-Signature"] = _signature(
            secret=profile.signing_secret,
            timestamp=timestamp,
            delivery_id=str(delivery.id),
            idempotency_key=delivery.idempotency_key,
            payload=delivery.payload,
        )
    return headers


@dataclass(frozen=True)
class _AttemptResult:
    outcome: str
    status_code: int | None = None
    error: str | None = None
    response_body_preview: str | None = None


async def _send(
    delivery: WebhookDelivery,
    profile: WebhookProfileConfig,
    attempt_number: int,
) -> _AttemptResult:
    try:
        async with httpx.AsyncClient(timeout=profile.timeout_seconds) as client:
            response = await client.request(
                profile.method,
                profile.url,
                content=_payload_bytes(delivery.payload),
                headers=_headers(delivery, profile, attempt_number=attempt_number),
            )
    except httpx.HTTPError as exc:
        return _AttemptResult(outcome="retry", error=f"{type(exc).__name__}: {exc}")

    if 200 <= response.status_code < 300:
        outcome = "success"
    elif response.status_code in profile.permanent_failure_statuses:
        outcome = "dead"
    elif response.status_code in profile.retry_statuses or response.status_code >= 500:
        outcome = "retry"
    else:
        outcome = "dead"
    return _AttemptResult(
        outcome=outcome,
        status_code=response.status_code,
        response_body_preview=_response_preview(response),
    )


@skrift.handler("webhooks.deliver", queue="webhooks", max_attempts=3, visibility_timeout=30.0)
async def deliver_webhook(job: DeliverWebhook, context) -> dict[str, Any]:
    """Deliver one outbound webhook attempt."""

    from skrift.webhooks.service import _require_session_maker

    session_maker = _require_session_maker()
    started_at = _utcnow()
    worker_job_id = context.job.id

    async with session_maker() as db_session:
        delivery = await db_session.get(WebhookDelivery, _identity(job.delivery_id))
        if delivery is None:
            raise PermanentFailure(f"Unknown webhook delivery {job.delivery_id!r}")
        if delivery.status in {"succeeded", "dead", "cancelled"}:
            return {"status": delivery.status, "duplicate": True}
        if job.attempt_number <= delivery.attempt_count:
            return {"status": delivery.status, "duplicate": True}
        profile = get_profile(delivery.profile)
        now = _utcnow()
        next_attempt_at = _coerce_utc(delivery.next_attempt_at)
        if next_attempt_at is not None and next_attempt_at > now:
            return skrift.Pause(resume_at=next_attempt_at)
        delivery.status = "sending"
        delivery.locked_by = worker_job_id
        delivery.locked_until = now + timedelta(seconds=context.job.visibility_timeout)
        delivery.worker_job_id = worker_job_id
        if delivery.first_attempt_at is None:
            delivery.first_attempt_at = now
        await db_session.commit()

    result = await _send(delivery, profile, job.attempt_number)
    finished_at = _utcnow()

    async with session_maker() as db_session:
        current = await db_session.get(WebhookDelivery, _identity(job.delivery_id))
        if current is None:
            raise PermanentFailure(f"Unknown webhook delivery {job.delivery_id!r}")
        if current.locked_by != worker_job_id:
            return {"status": current.status, "stale": True}

        current.attempt_count = job.attempt_number
        current.last_status_code = result.status_code
        current.last_error = result.error
        current.locked_by = None
        current.locked_until = None

        attempt = WebhookDeliveryAttempt(
            delivery_id=current.id,
            attempt_number=job.attempt_number,
            worker_job_id=worker_job_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=(finished_at - started_at).total_seconds(),
            status_code=result.status_code,
            outcome=result.outcome,
            error=result.error,
            response_body_preview=result.response_body_preview,
        )
        db_session.add(attempt)

        if result.outcome == "success":
            current.status = "succeeded"
            current.delivered_at = finished_at
            current.retention_until = finished_at + timedelta(
                seconds=profile.retention.succeeded_seconds
            )
            await db_session.commit()
            return {"status": "succeeded", "attempt": job.attempt_number}

        exhausted = (
            job.attempt_number >= profile.max_attempts
            or _deadline_exceeded(current, profile)
            or result.outcome == "dead"
        )
        if exhausted:
            current.status = "dead"
            current.dead_at = finished_at
            current.retention_until = finished_at + timedelta(
                seconds=profile.retention.dead_seconds
            )
            if current.last_error is None and result.status_code is not None:
                current.last_error = f"HTTP {result.status_code}"
            await db_session.commit()
            return {"status": "dead", "attempt": job.attempt_number}

        next_attempt_at = finished_at + timedelta(seconds=_next_delay(profile, job.attempt_number))
        current.status = "retrying"
        current.next_attempt_at = next_attempt_at
        next_attempt_number = job.attempt_number + 1
        current.worker_job_id = delivery_job_id(current.id, next_attempt_number)
        await db_session.commit()

    await submit_delivery(job.delivery_id)
    return {"status": "retrying", "attempt": job.attempt_number}


@skrift.handler("webhooks.reconcile", queue="webhooks", max_attempts=3)
async def reconcile_webhooks(job: ReconcileWebhooks) -> dict[str, Any]:
    """Submit worker jobs for due queued/retrying webhook deliveries."""

    from skrift.webhooks.service import _require_settings
    from skrift.workers import JobIdConflict, get_runtime

    pruned = await prune_retained_deliveries()
    recovered = await recover_expired_delivery_locks(limit=job.limit)
    submitted = await submit_due_deliveries(limit=job.limit)
    next_job_id = None
    if job.reschedule:
        settings = _require_settings()
        next_at = _utcnow() + timedelta(seconds=settings.reconcile_interval_seconds)
        next_job_id = f"webhooks:reconcile:{int(next_at.timestamp())}"
        try:
            await get_runtime().submit(
                ReconcileWebhooks(limit=job.limit, reschedule=True),
                queue="webhooks",
                scheduled_for=next_at,
                job_id=next_job_id,
            )
        except JobIdConflict:
            pass
    return {
        "recovered": recovered,
        "pruned": pruned,
        "submitted": len(submitted),
        "job_ids": submitted,
        "next_job_id": next_job_id,
    }
