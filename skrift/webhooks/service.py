"""Public service API for Skrift outbound webhooks."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from skrift.config import WebhookProfileConfig, WebhooksConfig
from skrift.db.models.webhook import WebhookDelivery

logger = logging.getLogger(__name__)

_settings: WebhooksConfig | None = None
_session_maker: Any | None = None
_SESSION_INFO_KEY = "skrift_webhook_delivery_ids"


class WebhookConfigurationError(RuntimeError):
    """Raised when the webhook framework has not been configured."""


class WebhookIdempotencyConflict(ValueError):
    """Raised when an idempotency key is reused with a different payload."""


def configure_webhooks(settings: WebhooksConfig, *, session_maker: Any) -> None:
    """Configure process-local webhook settings and database access."""

    global _settings, _session_maker
    _settings = settings
    _session_maker = session_maker


def _require_settings() -> WebhooksConfig:
    if _settings is None:
        raise WebhookConfigurationError("Webhook framework is not configured")
    return _settings


def _require_session_maker() -> Any:
    if _session_maker is None:
        raise WebhookConfigurationError("Webhook framework has no session maker configured")
    return _session_maker


def get_profile(name: str) -> WebhookProfileConfig:
    """Return a configured webhook profile by name."""

    settings = _require_settings()
    try:
        profile = settings.profiles[name]
    except KeyError as exc:
        raise WebhookConfigurationError(f"Unknown webhook profile {name!r}") from exc
    if not profile.enabled:
        raise WebhookConfigurationError(f"Webhook profile {name!r} is disabled")
    return profile


def ensure_handlers_registered() -> None:
    """Ensure built-in webhook worker handlers are present in the global registry."""

    from skrift.workers.registry import registry

    try:
        registry.get("webhooks.deliver")
        registry.get("webhooks.reconcile")
        return
    except KeyError:
        pass

    import skrift.webhooks.jobs as jobs

    importlib.reload(jobs)


def delivery_job_id(delivery_id: str | UUID, attempt_number: int) -> str:
    """Return the deterministic worker job id for a delivery attempt."""

    normalized = str(delivery_id).replace("-", "")
    return f"webhook:{normalized}:{attempt_number}"


def _identity(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def payload_hash(payload: dict[str, Any]) -> str:
    """Return a stable hash for idempotency conflict detection."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def enqueue(
    db_session: AsyncSession,
    *,
    profile: str,
    idempotency_key: str,
    payload: dict[str, Any],
    event_type: str = "",
    scheduled_for: datetime | None = None,
    submit_after_commit: bool = True,
) -> WebhookDelivery:
    """Insert a durable webhook delivery using the caller's transaction.

    This function never commits. The caller controls transaction boundaries, so
    domain writes and webhook enqueue either commit together or roll back
    together.
    """

    get_profile(profile)
    digest = payload_hash(payload)
    result = await db_session.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.profile == profile,
            WebhookDelivery.idempotency_key == idempotency_key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if existing.payload_hash != digest:
            raise WebhookIdempotencyConflict(
                f"Webhook idempotency key {idempotency_key!r} for profile {profile!r} "
                "already exists with a different payload"
            )
        if submit_after_commit and existing.status in {"queued", "retrying"}:
            _remember_after_commit(db_session, str(existing.id))
        return existing

    delivery = WebhookDelivery(
        profile=profile,
        idempotency_key=idempotency_key,
        event_type=event_type,
        payload=payload,
        payload_hash=digest,
        status="queued",
        attempt_count=0,
        next_attempt_at=_coerce_utc(scheduled_for) or _utcnow(),
    )
    db_session.add(delivery)
    await db_session.flush()
    if submit_after_commit:
        _remember_after_commit(db_session, str(delivery.id))
    return delivery


async def enqueue_standalone(
    *,
    profile: str,
    idempotency_key: str,
    payload: dict[str, Any],
    event_type: str = "",
    scheduled_for: datetime | None = None,
) -> WebhookDelivery:
    """Open a session, enqueue one delivery, commit it, and nudge workers."""

    session_maker = _require_session_maker()
    async with session_maker() as db_session:
        delivery = await enqueue(
            db_session,
            profile=profile,
            idempotency_key=idempotency_key,
            payload=payload,
            event_type=event_type,
            scheduled_for=scheduled_for,
            submit_after_commit=False,
        )
        delivery_id = str(delivery.id)
        await db_session.commit()
    await submit_delivery(delivery_id)
    return delivery


def _remember_after_commit(db_session: AsyncSession, delivery_id: str) -> None:
    ids = db_session.sync_session.info.setdefault(_SESSION_INFO_KEY, set())
    ids.add(delivery_id)


@event.listens_for(Session, "after_commit")
def _submit_webhooks_after_commit(session: Session) -> None:
    delivery_ids = session.info.pop(_SESSION_INFO_KEY, None)
    if not delivery_ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for delivery_id in delivery_ids:
        loop.create_task(_submit_after_commit(delivery_id))


@event.listens_for(Session, "after_rollback")
def _clear_webhooks_after_rollback(session: Session) -> None:
    session.info.pop(_SESSION_INFO_KEY, None)


async def _submit_after_commit(delivery_id: str) -> None:
    try:
        await submit_delivery(delivery_id)
    except Exception:
        logger.debug("Webhook after-commit submit failed; reconciler will retry", exc_info=True)


async def submit_delivery(delivery_id: str | UUID) -> str | None:
    """Submit a worker job for the next attempt of one queued delivery."""

    from skrift.workers import JobIdConflict, get_runtime

    ensure_handlers_registered()
    from skrift.webhooks.jobs import DeliverWebhook

    session_maker = _require_session_maker()
    async with session_maker() as db_session:
        delivery = await db_session.get(WebhookDelivery, _identity(delivery_id))
        if delivery is None or delivery.status not in {"queued", "retrying"}:
            return None
        profile = get_profile(delivery.profile)
        attempt_number = delivery.attempt_count + 1
        job_id = delivery_job_id(delivery.id, attempt_number)
        scheduled_for = _coerce_utc(delivery.next_attempt_at)
        delivery.worker_job_id = job_id
        await db_session.commit()

    runtime = get_runtime()
    try:
        await runtime.submit(
            DeliverWebhook(delivery_id=str(delivery_id), attempt_number=attempt_number),
            queue=profile.queue,
            scheduled_for=scheduled_for,
            visibility_timeout=profile.visibility_timeout,
            job_id=job_id,
        )
    except JobIdConflict:
        logger.debug("Webhook delivery job already exists with different envelope: %s", job_id)
        raise
    return job_id


async def submit_due_deliveries(*, limit: int = 100) -> list[str]:
    """Submit worker jobs for due queued/retrying deliveries."""

    session_maker = _require_session_maker()
    now = _utcnow()
    async with session_maker() as db_session:
        result = await db_session.execute(
            select(WebhookDelivery.id)
            .where(
                WebhookDelivery.status.in_(["queued", "retrying"]),
                WebhookDelivery.next_attempt_at <= now,
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
        delivery_ids = [str(value) for value in result.scalars().all()]

    submitted: list[str] = []
    for delivery_id in delivery_ids:
        job_id = await submit_delivery(delivery_id)
        if job_id is not None:
            submitted.append(job_id)
    return submitted


async def recover_expired_delivery_locks(*, limit: int = 100) -> int:
    """Move stranded in-flight deliveries back to retry/dead state."""

    session_maker = _require_session_maker()
    now = _utcnow()
    async with session_maker() as db_session:
        result = await db_session.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == "sending",
                WebhookDelivery.locked_until.is_not(None),
                WebhookDelivery.locked_until <= now,
            )
            .order_by(WebhookDelivery.locked_until)
            .limit(limit)
        )
        deliveries = list(result.scalars().all())
        for delivery in deliveries:
            profile = get_profile(delivery.profile)
            failed_attempt = delivery.attempt_count + 1
            delivery.attempt_count = failed_attempt
            delivery.locked_by = None
            delivery.locked_until = None
            delivery.last_error = "Delivery lock expired before completion"
            deadline = None
            if profile.dead_letter_after_seconds is not None:
                deadline = _coerce_utc(delivery.created_at) + timedelta(
                    seconds=profile.dead_letter_after_seconds
                )
            if failed_attempt >= profile.max_attempts or (
                deadline is not None and now >= deadline
            ):
                delivery.status = "dead"
                delivery.dead_at = now
                delivery.retention_until = now + timedelta(
                    seconds=profile.retention.dead_seconds
                )
            else:
                delivery.status = "retrying"
                delivery.next_attempt_at = now
        await db_session.commit()
        return len(deliveries)


async def prune_retained_deliveries() -> int:
    """Delete terminal deliveries whose retention deadline has passed."""

    session_maker = _require_session_maker()
    async with session_maker() as db_session:
        result = await db_session.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.status.in_(["succeeded", "dead", "cancelled"]),
                WebhookDelivery.retention_until.is_not(None),
                WebhookDelivery.retention_until <= _utcnow(),
            )
        )
        await db_session.commit()
        return int(result.rowcount or 0)


async def retry_delivery(delivery_id: str | UUID) -> bool:
    """Reset a terminal webhook delivery and submit a fresh attempt."""

    session_maker = _require_session_maker()
    async with session_maker() as db_session:
        delivery = await db_session.get(WebhookDelivery, _identity(delivery_id))
        if delivery is None or delivery.status not in {"dead", "cancelled"}:
            return False
        delivery.status = "queued"
        delivery.attempt_count = 0
        delivery.next_attempt_at = _utcnow()
        delivery.dead_at = None
        delivery.delivered_at = None
        delivery.retention_until = None
        delivery.locked_by = None
        delivery.locked_until = None
        delivery.last_error = None
        delivery.last_status_code = None
        await db_session.commit()
    await submit_delivery(delivery_id)
    return True
