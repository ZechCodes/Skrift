"""Notifications controller — SSE stream and dismiss endpoints."""

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from litestar import Controller, Request, delete, get
from litestar.response import Response
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage

from skrift.lib.notifications import NotDismissibleError, _ensure_nid, notifications


class NotificationsController(Controller):
    path = "/notifications"

    @get("/stream")
    async def stream(self, request: Request) -> ServerSentEvent:
        """SSE endpoint that streams notifications to the client."""
        nid = _ensure_nid(request)
        user_id = request.session.get("user_id")

        since_raw = request.query_params.get("since")
        since: float | None = None
        if since_raw is not None:
            try:
                since = float(since_raw)
            except (ValueError, TypeError):
                pass

        async def generate() -> AsyncGenerator[ServerSentEventMessage, None]:
            q = notifications.register_connection(nid, user_id)
            try:
                # Flush phase: yield all queued notifications
                for n in await notifications.get_queued(nid, user_id):
                    yield ServerSentEventMessage(
                        data=json.dumps(n.to_dict()), event="notification"
                    )

                # Flush timeseries if since provided
                if since is not None:
                    for n in await notifications.get_since(nid, user_id, since):
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
        try:
            found = await notifications.dismiss(nid, user_id, notification_id)
        except NotDismissibleError:
            return Response(
                content={"error": "notification is not dismissible"}, status_code=409
            )
        if found:
            return Response(
                content={"dismissed": str(notification_id)}, status_code=200
            )
        return Response(
            content={"error": "not found"}, status_code=404
        )

    @delete("/group/{group:str}", status_code=200)
    async def dismiss_group(
        self, request: Request, group: str
    ) -> Response:
        """Dismiss a notification by group key."""
        nid = _ensure_nid(request)
        user_id = request.session.get("user_id")
        try:
            found = await notifications.dismiss(nid, user_id, group=group)
        except NotDismissibleError:
            return Response(
                content={"error": "notification is not dismissible"}, status_code=409
            )
        if found:
            return Response(
                content={"dismissed_group": group}, status_code=200
            )
        return Response(
            content={"error": "not found"}, status_code=404
        )
