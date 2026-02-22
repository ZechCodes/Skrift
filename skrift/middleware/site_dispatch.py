"""ASGI middleware for subdomain-based site dispatching.

Routes incoming requests to the appropriate Litestar app based on the
subdomain extracted from the Host header. Falls back to the primary app
when no subdomain matches.
"""

import asyncio
import logging

from litestar.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


def _extract_host(scope: Scope) -> str:
    """Extract the host from ASGI scope headers, stripping port."""
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"host":
            host = header_value.decode("latin-1")
            # Strip port if present
            if ":" in host:
                host = host.rsplit(":", 1)[0]
            return host.lower()
    return ""


def _get_subdomain(host: str, domain: str) -> str:
    """Extract the subdomain prefix from a host given the primary domain.

    Returns empty string if the host IS the primary domain or doesn't
    match the domain suffix.
    """
    host = host.lower()
    domain = domain.lower()
    if host == domain:
        return ""
    suffix = f".{domain}"
    if host.endswith(suffix):
        return host[: -len(suffix)]
    return ""


class SiteDispatcher:
    """ASGI dispatcher that routes requests to subdomain-specific Litestar apps.

    Each subdomain gets its own lightweight Litestar app with its own
    controllers and theme, while sharing the same database engine and
    session configuration with the primary app.

    Lifespan events are forwarded to all apps (primary + sites).
    """

    def __init__(
        self,
        primary_app: ASGIApp,
        site_apps: dict[str, ASGIApp],
        domain: str,
        force_subdomain: str = "",
    ) -> None:
        self.primary_app = primary_app
        self.site_apps = site_apps
        self.domain = domain.lower()
        self.force_subdomain = force_subdomain

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.primary_app(scope, receive, send)
            return

        if self.force_subdomain:
            subdomain = self.force_subdomain
        else:
            host = _extract_host(scope)
            subdomain = _get_subdomain(host, self.domain)

        scope.setdefault("state", {})

        if subdomain and subdomain in self.site_apps:
            scope["state"]["site_name"] = subdomain
            await self.site_apps[subdomain](scope, receive, send)
        else:
            scope["state"]["site_name"] = ""
            await self.primary_app(scope, receive, send)

    async def _handle_lifespan(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Forward lifespan startup/shutdown to primary app and all site apps."""
        all_apps = [self.primary_app, *self.site_apps.values()]
        startup_complete = asyncio.Event()
        shutdown_complete = asyncio.Event()
        failed = False

        # Per-app lifespan channels
        app_queues: list[asyncio.Queue] = [asyncio.Queue() for _ in all_apps]
        app_events: list[asyncio.Event] = [asyncio.Event() for _ in all_apps]

        async def app_receive(idx: int):
            return await app_queues[idx].get()

        async def app_send(idx: int, message: dict):
            nonlocal failed
            msg_type = message.get("type", "")
            if msg_type in (
                "lifespan.startup.complete",
                "lifespan.shutdown.complete",
            ):
                app_events[idx].set()
            elif msg_type in (
                "lifespan.startup.failed",
                "lifespan.shutdown.failed",
            ):
                failed = True
                app_events[idx].set()

        tasks = []
        for i, app in enumerate(all_apps):
            idx = i

            async def run(a=app, j=idx):
                try:
                    await a(
                        scope,
                        lambda _j=j: app_receive(_j),
                        lambda msg, _j=j: app_send(_j, msg),
                    )
                except Exception:
                    logger.warning(
                        "Lifespan error for app %d", j, exc_info=True
                    )

            tasks.append(asyncio.create_task(run()))

        # Wait for the outer lifespan startup message
        message = await receive()
        if message["type"] == "lifespan.startup":
            # Send startup to all apps
            for q in app_queues:
                await q.put({"type": "lifespan.startup"})
            # Wait for all to report
            await asyncio.gather(*(e.wait() for e in app_events))

            if failed:
                await send({"type": "lifespan.startup.failed", "message": "Site app startup failed"})
                return

            await send({"type": "lifespan.startup.complete"})
            startup_complete.set()

        # Wait for shutdown
        for e in app_events:
            e.clear()

        message = await receive()
        if message["type"] == "lifespan.shutdown":
            for q in app_queues:
                await q.put({"type": "lifespan.shutdown"})
            await asyncio.gather(*(e.wait() for e in app_events))
            await send({"type": "lifespan.shutdown.complete"})
            shutdown_complete.set()

        # Clean up tasks
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
