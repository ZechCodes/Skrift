"""Tests for durable outbound webhooks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift.webhooks.jobs  # noqa: F401 - register built-in handlers
from skrift.config import WebhookProfileConfig, WebhooksConfig
from skrift.db.base import Base
from skrift.db.models.webhook import WebhookDelivery, WebhookDeliveryAttempt
from skrift.webhooks import (
    WebhookIdempotencyConflict,
    configure_webhooks,
    enqueue,
    submit_delivery,
)
from skrift.workers import configure_workers


@pytest.fixture
async def webhooks_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'webhooks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_runtime():
    import skrift.workers.runtime as worker_runtime

    previous = worker_runtime._runtime
    worker_runtime._runtime = None
    yield
    worker_runtime._runtime = previous


def _settings(**profile_overrides):
    return WebhooksConfig(
        enabled=True,
        profiles={
            "test": WebhookProfileConfig(
                url="https://example.com/webhook",
                signing_secret="secret",
                **profile_overrides,
            )
        },
    )


async def test_enqueue_participates_in_caller_transaction(webhooks_session_maker):
    configure_webhooks(_settings(), session_maker=webhooks_session_maker)

    async with webhooks_session_maker() as session:
        await enqueue(
            session,
            profile="test",
            event_type="page.published",
            idempotency_key="page:1:v1",
            payload={"page_id": "1"},
            submit_after_commit=False,
        )
        await session.rollback()

    async with webhooks_session_maker() as session:
        count = await session.scalar(select(func.count()).select_from(WebhookDelivery))

    assert count == 0


async def test_enqueue_is_idempotent_and_rejects_payload_conflicts(webhooks_session_maker):
    configure_webhooks(_settings(), session_maker=webhooks_session_maker)

    async with webhooks_session_maker() as session:
        created = await enqueue(
            session,
            profile="test",
            idempotency_key="stable-key",
            payload={"n": 1},
            submit_after_commit=False,
        )
        await session.commit()

    async with webhooks_session_maker() as session:
        existing = await enqueue(
            session,
            profile="test",
            idempotency_key="stable-key",
            payload={"n": 1},
            submit_after_commit=False,
        )
        assert existing.id == created.id

        with pytest.raises(WebhookIdempotencyConflict):
            await enqueue(
                session,
                profile="test",
                idempotency_key="stable-key",
                payload={"n": 2},
                submit_after_commit=False,
            )


async def test_submit_delivery_sends_signed_webhook(monkeypatch, webhooks_session_maker):
    configure_webhooks(_settings(), session_maker=webhooks_session_maker)
    configure_workers(mode="inline", queues=("webhooks",))
    sent = SimpleNamespace(headers=None, content=None)

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, *, content, headers):
            sent.method = method
            sent.url = url
            sent.content = content
            sent.headers = headers
            return httpx.Response(204, request=httpx.Request(method, url))

    monkeypatch.setattr(skrift.webhooks.jobs.httpx, "AsyncClient", FakeClient)

    async with webhooks_session_maker() as session:
        delivery = await enqueue(
            session,
            profile="test",
            event_type="page.published",
            idempotency_key="page:1:v1",
            payload={"page_id": "1"},
            submit_after_commit=False,
        )
        delivery_id = str(delivery.id)
        await session.commit()

    await submit_delivery(delivery_id)

    async with webhooks_session_maker() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        attempts = await session.scalar(select(func.count()).select_from(WebhookDeliveryAttempt))

    assert delivery.status == "succeeded"
    assert attempts == 1
    assert sent.method == "POST"
    assert sent.url == "https://example.com/webhook"
    assert sent.headers["Idempotency-Key"] == "page:1:v1"
    assert sent.headers["X-Skrift-Delivery-Id"] == delivery_id
    assert sent.headers["X-Skrift-Signature"].startswith("v1=")


async def test_enqueue_nudges_worker_after_commit(monkeypatch, webhooks_session_maker):
    configure_webhooks(_settings(), session_maker=webhooks_session_maker)
    configure_workers(mode="inline", queues=("webhooks",))

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, *, content, headers):
            return httpx.Response(204, request=httpx.Request(method, url))

    monkeypatch.setattr(skrift.webhooks.jobs.httpx, "AsyncClient", FakeClient)

    async with webhooks_session_maker() as session:
        delivery = await enqueue(
            session,
            profile="test",
            idempotency_key="after-commit",
            payload={"ok": True},
        )
        delivery_id = str(delivery.id)
        await session.commit()

    for _ in range(20):
        async with webhooks_session_maker() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            if delivery.status == "succeeded":
                break
        await asyncio.sleep(0.01)

    assert delivery.status == "succeeded"
