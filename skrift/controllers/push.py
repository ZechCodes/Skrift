"""Web Push subscription management endpoints."""

from pathlib import Path

from litestar import Controller, Request, get, post
from litestar.response import File, Response
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.lib.push import get_vapid_public_key, remove_subscription, save_subscription

_SW_PATH = Path(__file__).parent.parent / "static" / "sw.js"


class PushController(Controller):
    """Endpoints for Web Push subscription management."""

    path = "/push"

    @get("/vapid-key")
    async def vapid_key(self, request: Request, db_session: AsyncSession) -> Response:
        """Return the VAPID public key for the frontend to subscribe."""
        public_key = await get_vapid_public_key(db_session)
        return Response(content={"publicKey": public_key})

    @post("/subscribe")
    async def subscribe(self, request: Request, db_session: AsyncSession) -> Response:
        """Register a push subscription for the current user."""
        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            return Response(content={"error": "unauthorized"}, status_code=401)

        body = await request.json()
        endpoint = body.get("endpoint", "")
        keys = body.get("keys", {})
        p256dh = keys.get("p256dh", "")
        auth = keys.get("auth", "")

        if not endpoint or not p256dh or not auth:
            return Response(
                content={"error": "endpoint and keys (p256dh, auth) required"},
                status_code=400,
            )

        await save_subscription(db_session, user_id, endpoint, p256dh, auth)
        return Response(content={"ok": True}, status_code=201)

    @post("/unsubscribe")
    async def unsubscribe(self, request: Request, db_session: AsyncSession) -> Response:
        """Remove a push subscription."""
        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            return Response(content={"error": "unauthorized"}, status_code=401)

        body = await request.json()
        endpoint = body.get("endpoint", "")
        if not endpoint:
            return Response(content={"error": "endpoint required"}, status_code=400)

        removed = await remove_subscription(db_session, endpoint)
        return Response(content={"ok": removed})


@get("/sw.js", media_type="application/javascript")
async def service_worker() -> File:
    """Serve the base service worker at root scope for push notifications."""
    return File(
        path=_SW_PATH,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )
