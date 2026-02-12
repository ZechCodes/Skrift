"""Notifications controller — SSE stream and dismiss endpoints."""

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from litestar import Controller, Request, delete, get
from litestar.response import Response
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage

from skrift.lib.notifications import _ensure_nid, notifications


class NotificationsController(Controller):
    path = "/notifications"

    @get("/stream")
    async def stream(self, request: Request) -> ServerSentEvent:
        """SSE endpoint that streams notifications to the client."""
        nid = _ensure_nid(request)
        user_id = request.session.get("user_id")

        async def generate() -> AsyncGenerator[ServerSentEventMessage, None]:
            q = notifications.register_connection(nid, user_id)
            try:
                # Flush phase: yield all queued notifications
                for n in notifications.get_queued(nid, user_id):
                    yield ServerSentEventMessage(
                        data=json.dumps(n.to_dict()), event="notification"
                    )

                # Sync marker
                yield ServerSentEventMessage(data="", event="sync")

                # Live phase
                while True:
                    try:
                        n = await asyncio.wait_for(q.get(), timeout=30.0)
                        yield ServerSentEventMessage(
                            data=json.dumps(n.to_dict()), event="notification"
                        )
                    except asyncio.TimeoutError:
                        yield ServerSentEventMessage(comment="keepalive")
            finally:
                notifications.unregister_connection(nid, q)

        return ServerSentEvent(generate())

    @delete("/{notification_id:uuid}", status_code=200)
    async def dismiss(
        self, request: Request, notification_id: UUID
    ) -> Response:
        """Dismiss a notification by ID."""
        nid = _ensure_nid(request)
        user_id = request.session.get("user_id")
        found = notifications.dismiss(nid, user_id, notification_id)
        if found:
            return Response(
                content={"dismissed": str(notification_id)}, status_code=200
            )
        return Response(
            content={"error": "not found"}, status_code=404
        )
