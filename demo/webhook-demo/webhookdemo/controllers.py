"""Demo Skrift site that periodically enqueues outbound webhooks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from itertools import count
from typing import Annotated
from uuid import uuid4

import httpx
import skrift
from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Response, Template as TemplateResponse

from skrift.lib.hooks import AFTER_USER_CREATED, APP_SHUTDOWN, APP_STARTUP, action
from skrift.workers.models import utcnow


logger = logging.getLogger(__name__)
_generator_task: asyncio.Task | None = None
_sequence = count(1)
_receiver_internal_url = "http://receiver:8090"


def _receiver_url(request: Request) -> str:
    """Build a receiver URL that works from the current client machine."""

    host_header = request.headers.get("host", "localhost")
    if host_header.startswith("["):
        host = host_header.split("]", 1)[0] + "]"
    else:
        host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return f"{request.url.scheme}://{host}:8085"


def _rewrite_receiver_html(html: str) -> str:
    return (
        html.replace('action="/controls"', 'action="/receiver/controls"')
        .replace('action="/clear"', 'action="/receiver/clear"')
        .replace('href="http://localhost:8084"', 'href="/"')
        .replace('href="http://192.168.68.61:8084"', 'href="/"')
        .replace('href="http://receiver:8084"', 'href="/"')
    )


async def _receiver_request(
    method: str,
    path: str,
    *,
    data: dict | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        return await client.request(method, f"{_receiver_internal_url}{path}", data=data)


def _event_payload(sequence: int, *, manual: bool = False) -> dict:
    timestamp = utcnow().isoformat()
    return {
        "sequence": sequence,
        "kind": "manual" if manual else "periodic",
        "message": f"Skrift webhook demo event {sequence}",
        "created_at": timestamp,
    }


async def _enqueue_demo_webhook(*, manual: bool = False) -> str:
    sequence = next(_sequence)
    await skrift.enqueue_webhook_standalone(
        profile="receiver",
        event_type="demo.manual" if manual else "demo.periodic",
        idempotency_key=f"webhook-demo:{uuid4().hex}",
        payload=_event_payload(sequence, manual=manual),
    )
    logger.info("Enqueued webhook demo delivery %s", sequence)
    return str(sequence)


async def _periodic_webhooks() -> None:
    while True:
        try:
            await _enqueue_demo_webhook()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Webhook demo failed to enqueue a periodic delivery")
        await asyncio.sleep(8)


@action(APP_STARTUP)
async def start_webhook_demo(_app) -> None:
    """Start the periodic sender."""

    global _generator_task
    if _generator_task is None or _generator_task.done():
        _generator_task = asyncio.create_task(
            _periodic_webhooks(),
            name="webhook-demo-periodic-sender",
        )
        logger.info("Webhook demo periodic sender started")


@action(APP_SHUTDOWN)
async def stop_webhook_demo(_app) -> None:
    """Stop the periodic sender."""

    global _generator_task
    if _generator_task is not None:
        _generator_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _generator_task
        _generator_task = None


@action(AFTER_USER_CREATED)
async def make_demo_users_admin(login_result, request) -> None:
    """Let a fresh dummy-login user open webhook and worker admin pages."""

    from skrift.auth.services import assign_role_to_user

    session_maker = request.app.state.session_maker_class
    async with session_maker() as session:
        await assign_role_to_user(session, login_result.user.id, "admin")


class WebhookDemoController(Controller):
    """Landing page for the webhook sender site."""

    path = "/"

    @get("/")
    async def index(self, request: Request) -> TemplateResponse:
        return TemplateResponse(
            "webhook-demo/index.html",
            context={
                "user": request.session.get("user_id"),
                "receiver_url": "/receiver",
                "direct_receiver_url": _receiver_url(request),
            },
        )

    @post("/webhooks/send")
    async def send_now(self) -> Redirect:
        await _enqueue_demo_webhook(manual=True)
        return Redirect(path="/admin/webhooks")

    @get("/receiver")
    async def receiver_proxy(self) -> Response:
        response = await _receiver_request("GET", "/")
        return Response(
            _rewrite_receiver_html(response.text),
            media_type="text/html",
            status_code=response.status_code,
        )

    @post("/receiver/controls")
    async def receiver_controls(
        self,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        await _receiver_request("POST", "/controls", data=dict(data))
        return Redirect(path="/receiver")

    @post("/receiver/clear")
    async def receiver_clear(self) -> Redirect:
        await _receiver_request("POST", "/clear")
        return Redirect(path="/receiver")
