"""Tests for ClientIPMiddleware."""

import pytest

from skrift.config import TrustedProxyConfig
from skrift.lib.trusted_proxy import TrustedProxyManager
from skrift.middleware.client_ip import ClientIPMiddleware


async def _app(scope, receive, send):
    # Echo the resolved IP and source back so we can assert on them.
    state = scope.get("state", {})
    ip = state.get("client_ip", "")
    source = state.get("client_ip_source", "")
    body = f"{ip}|{source}".encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _scope(peer="10.0.0.5", headers=None):
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers or [],
        "client": (peer, 0),
    }


class TestClientIPMiddleware:
    @pytest.mark.asyncio
    async def test_sets_resolved_state(self):
        mgr = TrustedProxyManager(
            TrustedProxyConfig(explicit=True, trusted=["10.0.0.0/8"])
        )
        mw = ClientIPMiddleware(_app, manager=mgr)

        messages = []

        async def send(msg):
            messages.append(msg)

        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"203.0.113.7")],
        )
        await mw(scope, None, send)

        assert scope["state"]["client_ip"] == "203.0.113.7"
        assert scope["state"]["client_ip_source"] == "xff"
        # App wrote body with "<ip>|<source>" — confirm it saw the state
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        assert body == b"203.0.113.7|xff"

    @pytest.mark.asyncio
    async def test_strict_mode_returns_400(self):
        mgr = TrustedProxyManager(
            TrustedProxyConfig(
                explicit=True,
                trusted=["10.0.0.0/8"],
                strict=True,
            )
        )
        mw = ClientIPMiddleware(_app, manager=mgr)

        messages = []

        async def send(msg):
            messages.append(msg)

        # Trusted peer, no forwarding header → strict failure
        scope = _scope(peer="10.0.0.5", headers=[])
        await mw(scope, None, send)

        assert messages[0]["status"] == 400

    @pytest.mark.asyncio
    async def test_untrusted_peer_uses_socket(self):
        mgr = TrustedProxyManager(TrustedProxyConfig(explicit=True, trusted=[]))
        mw = ClientIPMiddleware(_app, manager=mgr)

        messages = []

        async def send(msg):
            messages.append(msg)

        scope = _scope(
            peer="203.0.113.7",
            headers=[(b"x-forwarded-for", b"1.2.3.4")],
        )
        await mw(scope, None, send)

        assert scope["state"]["client_ip"] == "203.0.113.7"
        assert scope["state"]["client_ip_source"] == "socket"

    @pytest.mark.asyncio
    async def test_lifespan_passthrough(self):
        called = False

        async def lifespan_app(scope, receive, send):
            nonlocal called
            called = True

        async def noop_send(_msg):
            return None

        mgr = TrustedProxyManager(TrustedProxyConfig(explicit=True))
        mw = ClientIPMiddleware(lifespan_app, manager=mgr)

        await mw({"type": "lifespan"}, None, noop_send)
        assert called
