"""Outbound webhook admin dashboard."""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Template as TemplateResponse
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.config import get_settings
from skrift.db.models.webhook import WebhookDelivery, WebhookDeliveryAttempt
from skrift.lib.flash import flash_error, flash_success, get_flash_messages
from skrift.webhooks import retry_delivery


WEBHOOK_DELIVERY_LIMIT = 100
WEBHOOK_ATTEMPT_LIMIT = 30
WEBHOOK_STREAM_INTERVAL_SECONDS = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _short(value: object, length: int = 8) -> str:
    return str(value)[:length]


def _profile_url(profile: str) -> str:
    settings = get_settings()
    configured = settings.webhooks.profiles.get(profile)
    return configured.url if configured is not None else ""


def _profile_domain(profile: str) -> str:
    parsed = urlparse(_profile_url(profile))
    return parsed.netloc or "unconfigured"


def _serialize_delivery(delivery: WebhookDelivery) -> dict:
    terminal_at = delivery.delivered_at or delivery.dead_at
    last_result = delivery.last_error or (
        f"HTTP {delivery.last_status_code}" if delivery.last_status_code else ""
    )
    return {
        "id": str(delivery.id),
        "short_id": _short(delivery.id),
        "profile": delivery.profile,
        "domain": _profile_domain(delivery.profile),
        "endpoint": _profile_url(delivery.profile),
        "event_type": delivery.event_type,
        "idempotency_key": delivery.idempotency_key,
        "status": delivery.status,
        "attempt_count": delivery.attempt_count,
        "next_attempt_at": _fmt(delivery.next_attempt_at),
        "created_at": _fmt(delivery.created_at),
        "updated_at": _fmt(delivery.updated_at),
        "delivered_at": _fmt(delivery.delivered_at),
        "dead_at": _fmt(delivery.dead_at),
        "terminal_at": _fmt(terminal_at),
        "last_status_code": delivery.last_status_code or "",
        "last_error": delivery.last_error or "",
        "last_result": last_result,
        "retryable": delivery.status in {"dead", "cancelled"},
    }


def _serialize_attempt(attempt: WebhookDeliveryAttempt, *, include_delivery: bool = False) -> dict:
    delivery = attempt.delivery if include_delivery else None
    payload = {
        "id": str(attempt.id),
        "short_id": _short(attempt.id),
        "attempt_number": attempt.attempt_number,
        "started_at": _fmt(attempt.started_at),
        "finished_at": _fmt(attempt.finished_at),
        "duration_seconds": attempt.duration_seconds,
        "status_code": attempt.status_code or "",
        "outcome": attempt.outcome,
        "error": attempt.error or "",
        "response_body_preview": attempt.response_body_preview or "",
    }
    if delivery is not None:
        payload.update(
            {
                "delivery_id": str(delivery.id),
                "delivery_short_id": _short(delivery.id),
                "profile": delivery.profile,
                "domain": _profile_domain(delivery.profile),
                "event_type": delivery.event_type,
                "delivery_status": delivery.status,
            }
        )
    return payload


def _bucket_start(value: datetime, *, hours: int) -> datetime:
    value = value.astimezone(timezone.utc)
    if hours <= 24:
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _build_series(
    deliveries: list[WebhookDelivery],
    *,
    hours: int,
) -> tuple[list[dict], list[dict]]:
    domain_counts: dict[tuple[datetime, str], int] = defaultdict(int)
    endpoint_counts: dict[tuple[datetime, str], int] = defaultdict(int)

    for delivery in deliveries:
        if delivery.status not in {"dead", "retrying"}:
            continue
        timestamp = delivery.dead_at or delivery.updated_at or delivery.created_at
        bucket = _bucket_start(timestamp, hours=hours)
        domain_counts[(bucket, _profile_domain(delivery.profile))] += 1
        endpoint_counts[(bucket, delivery.profile)] += 1

    domain_series = [
        {"bucket": _fmt(bucket), "domain": domain, "failures": count}
        for (bucket, domain), count in sorted(domain_counts.items())
    ]
    endpoint_series = [
        {"bucket": _fmt(bucket), "profile": profile, "failures": count}
        for (bucket, profile), count in sorted(endpoint_counts.items())
    ]
    return domain_series, endpoint_series


async def _dashboard_snapshot(
    db_session: AsyncSession,
    *,
    status: str | None = None,
    profile: str | None = None,
    hours: int = 24,
) -> dict:
    cutoff = _utcnow() - timedelta(hours=hours)

    base = select(WebhookDelivery).where(WebhookDelivery.created_at >= cutoff)
    if status:
        base = base.where(WebhookDelivery.status == status)
    if profile:
        base = base.where(WebhookDelivery.profile == profile)
    result = await db_session.execute(
        base.order_by(WebhookDelivery.updated_at.desc()).limit(WEBHOOK_DELIVERY_LIMIT)
    )
    deliveries = list(result.scalars().all())

    all_recent = list(
        (
            await db_session.execute(
                select(WebhookDelivery).where(WebhookDelivery.created_at >= cutoff)
            )
        )
        .scalars()
        .all()
    )
    status_counts = Counter(delivery.status for delivery in all_recent)
    profile_counts = Counter(delivery.profile for delivery in all_recent)
    domain_counts = Counter(
        _profile_domain(delivery.profile)
        for delivery in all_recent
        if delivery.status in {"dead", "retrying"}
    )
    domain_series, endpoint_series = _build_series(all_recent, hours=hours)
    attempts = await db_session.scalar(
        select(func.count())
        .select_from(WebhookDeliveryAttempt)
        .where(WebhookDeliveryAttempt.started_at >= cutoff)
    )
    recent_attempts = list(
        (
            await db_session.execute(
                select(WebhookDeliveryAttempt)
                .options(selectinload(WebhookDeliveryAttempt.delivery))
                .where(WebhookDeliveryAttempt.started_at >= cutoff)
                .order_by(WebhookDeliveryAttempt.finished_at.desc())
                .limit(WEBHOOK_ATTEMPT_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    return {
        "deliveries": [_serialize_delivery(delivery) for delivery in deliveries],
        "status_counts": dict(status_counts),
        "profile_counts": dict(profile_counts),
        "domain_counts": dict(domain_counts),
        "domain_series": domain_series,
        "endpoint_series": endpoint_series,
        "total_recent": len(all_recent),
        "attempts_total": int(attempts or 0),
        "recent_attempts": [
            _serialize_attempt(attempt, include_delivery=True)
            for attempt in recent_attempts
        ],
        "filters": {
            "status": status or "",
            "profile": profile or "",
            "hours": hours,
        },
        "updated_at": _fmt(_utcnow()),
    }


class WebhooksAdminController(Controller):
    """Dashboard and operations for outbound webhook deliveries."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/webhooks",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Webhooks", "icon": "send", "order": 94},
    )
    async def webhooks(
        self,
        request: Request,
        db_session: AsyncSession,
        status: str | None = None,
        profile: str | None = None,
        hours: int = 24,
    ) -> TemplateResponse:
        """Show outbound webhook delivery health and failure trends."""

        ctx = await get_admin_context(request, db_session)
        snapshot = await _dashboard_snapshot(
            db_session,
            status=status,
            profile=profile,
            hours=hours,
        )

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/webhooks.html",
            context={
                "flash_messages": flash_messages,
                **snapshot,
                "profiles": sorted(get_settings().webhooks.profiles),
                **ctx,
            },
        )

    @get(
        "/webhooks/stream",
        guards=[auth_guard, Permission("administrator")],
    )
    async def stream(
        self,
        request: Request,
        status: str | None = None,
        profile: str | None = None,
        hours: int = 24,
    ) -> ServerSentEvent:
        """Stream filtered webhook dashboard snapshots."""

        session_maker = request.app.state.session_maker_class

        async def snapshot_message() -> ServerSentEventMessage:
            async with session_maker() as session:
                snapshot = await _dashboard_snapshot(
                    session,
                    status=status,
                    profile=profile,
                    hours=hours,
                )
            return ServerSentEventMessage(
                data=json.dumps(snapshot),
                event="webhooks_snapshot",
            )

        async def generate() -> AsyncGenerator[ServerSentEventMessage, None]:
            yield await snapshot_message()
            yield ServerSentEventMessage(data="", event="sync")
            while True:
                await asyncio.sleep(WEBHOOK_STREAM_INTERVAL_SECONDS)
                yield await snapshot_message()

        return ServerSentEvent(generate())

    @get(
        "/webhooks/{delivery_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def webhook_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        delivery_id: str,
    ) -> TemplateResponse:
        """Show one outbound webhook delivery and its attempts."""

        ctx = await get_admin_context(request, db_session)
        try:
            delivery_uuid = UUID(delivery_id)
        except ValueError:
            flash_error(request, "Webhook delivery not found.")
            return Redirect("/admin/webhooks")

        delivery = await db_session.scalar(
            select(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_uuid)
            .options(selectinload(WebhookDelivery.attempts))
        )
        if delivery is None:
            flash_error(request, "Webhook delivery not found.")
            return Redirect("/admin/webhooks")

        return TemplateResponse(
            "admin/webhook_detail.html",
            context={
                "flash_messages": get_flash_messages(request),
                "delivery": _serialize_delivery(delivery),
                "payload_json": json.dumps(delivery.payload, indent=2, sort_keys=True),
                "attempts": [
                    _serialize_attempt(attempt)
                    for attempt in sorted(delivery.attempts, key=lambda item: item.attempt_number)
                ],
                **ctx,
            },
        )

    @post(
        "/webhooks/action",
        guards=[auth_guard, Permission("administrator")],
    )
    async def webhook_action(
        self,
        request: Request,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Run bulk webhook delivery actions."""

        action = data.get("action")
        delivery_ids = [
            key.removeprefix("delivery_")
            for key, value in data.items()
            if key.startswith("delivery_") and value
        ]
        if not delivery_ids:
            flash_error(request, "Select at least one webhook delivery.")
            return Redirect("/admin/webhooks")
        if action != "retry":
            flash_error(request, "Unknown webhook action.")
            return Redirect("/admin/webhooks")

        retried = 0
        for delivery_id in delivery_ids:
            if await retry_delivery(delivery_id):
                retried += 1
        flash_success(request, f"Retried {retried} webhook delivery(s).")
        return Redirect("/admin/webhooks")

    @post(
        "/webhooks/{delivery_id:str}/retry",
        guards=[auth_guard, Permission("administrator")],
    )
    async def retry_one(self, request: Request, delivery_id: str) -> Redirect:
        """Retry a single dead or cancelled webhook delivery."""

        if await retry_delivery(delivery_id):
            flash_success(request, "Webhook delivery queued for retry.")
        else:
            flash_error(request, "Webhook delivery cannot be retried.")
        return Redirect(f"/admin/webhooks/{delivery_id}")
