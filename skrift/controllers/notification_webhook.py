"""Notification webhook controller — HTTP endpoint for external notification delivery."""

import hmac
from typing import Annotated, Literal

from litestar import Controller, Request, post
from litestar.response import Response
from pydantic import BaseModel, Field

from skrift.lib import notifications as _notifications_mod
from skrift.lib.client_ip import get_client_ip
from skrift.lib.hooks import hooks, WEBHOOK_NOTIFICATION_RECEIVED
from skrift.lib.notifications import Notification, NotificationMode
from skrift.lib.sliding_window import SlidingWindowCounter


class _FailedAuthLimiter:
    """Per-IP sliding window that tracks failed auth attempts.

    Only records *failed* attempts; successful requests don't touch it.
    """

    def __init__(self, max_failures: int = 1, window: float = 60.0) -> None:
        self.max_failures = max_failures
        self._counter = SlidingWindowCounter(window=window)

    def record_failure(self, ip: str) -> None:
        self._counter.record(ip)

    def is_blocked(self, ip: str) -> bool:
        return self._counter.count(ip) >= self.max_failures


_failed_auth_limiter = _FailedAuthLimiter()


# --- Request models ---


class _BaseTarget(BaseModel):
    type: str
    group: str | None = None
    mode: str = "queued"
    payload: dict = Field(default_factory=dict)

    @property
    def scope(self) -> str:
        raise NotImplementedError

    @property
    def scope_id(self) -> str | None:
        raise NotImplementedError

    async def dispatch(self, svc: "_notifications_mod.NotificationService", notification: Notification) -> None:
        raise NotImplementedError


class _SessionTarget(_BaseTarget):
    target: Literal["session"]
    session_id: str

    @property
    def scope(self) -> str:
        return "session"

    @property
    def scope_id(self) -> str:
        return self.session_id

    async def dispatch(self, svc, notification):
        await svc.send_to_session(self.session_id, notification)


class _UserTarget(_BaseTarget):
    target: Literal["user"]
    user_id: str

    @property
    def scope(self) -> str:
        return "user"

    @property
    def scope_id(self) -> str:
        return self.user_id

    async def dispatch(self, svc, notification):
        await svc.send_to_user(self.user_id, notification)


class _BroadcastTarget(_BaseTarget):
    target: Literal["broadcast"]

    @property
    def scope(self) -> str:
        return "broadcast"

    @property
    def scope_id(self) -> None:
        return None

    async def dispatch(self, svc, notification):
        await svc.broadcast(notification)


WebhookRequest = Annotated[
    _SessionTarget | _UserTarget | _BroadcastTarget,
    Field(discriminator="target"),
]


class NotificationsWebhookController(Controller):
    path = "/notifications/webhook"

    @post("/")
    async def handle(self, request: Request) -> Response:
        # 1. Extract client IP
        ip = get_client_ip(request.scope)

        # 2. Rate limit check (failed auth attempts only)
        if _failed_auth_limiter.is_blocked(ip):
            return Response(
                content={"error": "Too many failed auth attempts"},
                status_code=429,
            )

        # 3. Auth check
        secret = getattr(request.app.state, "webhook_secret", "")
        if not secret:
            return Response(content={"error": "Webhook not configured"}, status_code=404)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            _failed_auth_limiter.record_failure(ip)
            return Response(content={"error": "Unauthorized"}, status_code=401)

        token = auth_header[7:]
        if not hmac.compare_digest(token, secret):
            _failed_auth_limiter.record_failure(ip)
            return Response(content={"error": "Unauthorized"}, status_code=401)

        # 4. Parse and validate body
        body = await request.json()
        target = body.get("target")
        if target == "session":
            req = _SessionTarget(**body)
        elif target == "user":
            req = _UserTarget(**body)
        elif target == "broadcast":
            req = _BroadcastTarget(**body)
        else:
            return Response(
                content={"error": f"Invalid target: {target!r}"}, status_code=422
            )

        # 5. Build notification and dispatch
        notification = Notification(
            type=req.type,
            group=req.group,
            mode=NotificationMode(req.mode),
            payload=req.payload,
        )

        svc = _notifications_mod.notifications
        await req.dispatch(svc, notification)

        await hooks.do_action(WEBHOOK_NOTIFICATION_RECEIVED, notification, req.scope, req.scope_id)

        return Response(
            content={"id": str(notification.id), "type": notification.type},
            status_code=202,
        )
