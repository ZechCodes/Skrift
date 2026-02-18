"""Notification webhook controller — HTTP endpoint for external notification delivery."""

import hmac
import time
from typing import Annotated, Literal

from litestar import Controller, Request, post
from litestar.response import Response
from pydantic import BaseModel, Field

from skrift.lib import notifications as _notifications_mod
from skrift.lib.hooks import hooks, WEBHOOK_NOTIFICATION_RECEIVED
from skrift.lib.notifications import Notification, NotificationMode


class _FailedAuthLimiter:
    """Per-IP sliding window that tracks failed auth attempts.

    Only records *failed* attempts; successful requests don't touch it.
    """

    def __init__(self, max_failures: int = 1, window: float = 60.0) -> None:
        self.max_failures = max_failures
        self.window = window
        self._buckets: dict[str, list[float]] = {}
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 60.0

    def _cleanup_stale(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - self.window
        stale_keys = []
        for key, timestamps in self._buckets.items():
            self._buckets[key] = [t for t in timestamps if t > cutoff]
            if not self._buckets[key]:
                stale_keys.append(key)
        for key in stale_keys:
            del self._buckets[key]

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        self._cleanup_stale(now)
        self._buckets.setdefault(ip, []).append(now)

    def is_blocked(self, ip: str) -> bool:
        now = time.monotonic()
        self._cleanup_stale(now)
        cutoff = now - self.window
        timestamps = self._buckets.get(ip)
        if not timestamps:
            return False
        self._buckets[ip] = [t for t in timestamps if t > cutoff]
        return len(self._buckets[ip]) >= self.max_failures


_failed_auth_limiter = _FailedAuthLimiter()


def _get_client_ip(request: Request) -> str:
    """Extract client IP, checking x-forwarded-for first."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.scope.get("client")
    if client:
        return client[0]
    return "unknown"


# --- Request models ---


class _SessionTarget(BaseModel):
    target: Literal["session"]
    session_id: str
    type: str
    group: str | None = None
    mode: str = "queued"
    payload: dict = Field(default_factory=dict)


class _UserTarget(BaseModel):
    target: Literal["user"]
    user_id: str
    type: str
    group: str | None = None
    mode: str = "queued"
    payload: dict = Field(default_factory=dict)


class _BroadcastTarget(BaseModel):
    target: Literal["broadcast"]
    type: str
    group: str | None = None
    mode: str = "queued"
    payload: dict = Field(default_factory=dict)


WebhookRequest = Annotated[
    _SessionTarget | _UserTarget | _BroadcastTarget,
    Field(discriminator="target"),
]


class NotificationsWebhookController(Controller):
    path = "/notifications/webhook"

    @post("/")
    async def handle(self, request: Request) -> Response:
        # 1. Extract client IP
        ip = _get_client_ip(request)

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
        if isinstance(req, _SessionTarget):
            target_type, scope_id = "session", req.session_id
            await svc.send_to_session(req.session_id, notification)
        elif isinstance(req, _UserTarget):
            target_type, scope_id = "user", req.user_id
            await svc.send_to_user(req.user_id, notification)
        else:
            target_type, scope_id = "broadcast", None
            await svc.broadcast(notification)

        await hooks.do_action(WEBHOOK_NOTIFICATION_RECEIVED, notification, target_type, scope_id)

        return Response(
            content={"id": str(notification.id), "type": notification.type},
            status_code=202,
        )
