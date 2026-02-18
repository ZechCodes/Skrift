"""Tests for the notification webhook controller."""

import pytest

from skrift.controllers.notification_webhook import (
    NotificationsWebhookController,
    _FailedAuthLimiter,
    _get_client_ip,
)
from skrift.lib.hooks import hooks, WEBHOOK_NOTIFICATION_RECEIVED
from skrift.lib.notifications import NotificationMode, NotificationService


# ---------------------------------------------------------------------------
# _FailedAuthLimiter
# ---------------------------------------------------------------------------


class TestFailedAuthLimiter:
    def test_not_blocked_initially(self):
        limiter = _FailedAuthLimiter(max_failures=2, window=60.0)
        assert limiter.is_blocked("1.2.3.4") is False

    def test_blocked_after_max_failures(self):
        limiter = _FailedAuthLimiter(max_failures=2, window=60.0)
        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("1.2.3.4") is False
        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("1.2.3.4") is True

    def test_different_ips_independent(self):
        limiter = _FailedAuthLimiter(max_failures=1, window=60.0)
        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("1.2.3.4") is True
        assert limiter.is_blocked("5.6.7.8") is False

    def test_failures_expire(self):
        import time

        limiter = _FailedAuthLimiter(max_failures=1, window=0.05)
        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("1.2.3.4") is True
        time.sleep(0.06)
        assert limiter.is_blocked("1.2.3.4") is False


# ---------------------------------------------------------------------------
# _get_client_ip
# ---------------------------------------------------------------------------


class TestGetClientIP:
    def _make_request(self, headers=None, client=None):
        """Create a minimal mock request for IP extraction."""

        class FakeRequest:
            def __init__(self, headers, scope):
                self.headers = headers or {}
                self.scope = scope

        scope = {}
        if client is not None:
            scope["client"] = client
        return FakeRequest(headers or {}, scope)

    def test_uses_x_forwarded_for_first_entry(self):
        req = self._make_request(
            headers={"x-forwarded-for": "10.0.0.1, 10.0.0.2"},
            client=("192.168.1.1", 12345),
        )
        assert _get_client_ip(req) == "10.0.0.1"

    def test_falls_back_to_client(self):
        req = self._make_request(client=("192.168.1.1", 12345))
        assert _get_client_ip(req) == "192.168.1.1"

    def test_returns_unknown_when_no_info(self):
        req = self._make_request()
        assert _get_client_ip(req) == "unknown"


# ---------------------------------------------------------------------------
# Webhook handler (end-to-end via Litestar test client)
# ---------------------------------------------------------------------------


@pytest.fixture
def notification_svc():
    """Create a fresh NotificationService for each test."""
    svc = NotificationService()
    from skrift.lib import notifications as mod

    original = mod.notifications
    mod.notifications = svc
    yield svc
    mod.notifications = original


@pytest.fixture(autouse=True)
def _reset_auth_limiter():
    """Reset the module-level auth limiter between tests."""
    from skrift.controllers import notification_webhook as mod

    original = mod._failed_auth_limiter
    mod._failed_auth_limiter = _FailedAuthLimiter()
    yield
    mod._failed_auth_limiter = original


@pytest.fixture
def webhook_client(notification_svc):
    """Create a Litestar test client with the webhook controller."""
    from litestar import Litestar
    from litestar.testing import TestClient

    app = Litestar(
        route_handlers=[NotificationsWebhookController],
        csrf_config=None,
    )
    app.state.webhook_secret = "test-secret-token"
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-secret-token"}


class TestWebhookAuth:
    def test_missing_auth_returns_401(self, webhook_client):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "broadcast", "type": "test"},
        )
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, webhook_client):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "broadcast", "type": "test"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_correct_token_accepted(self, webhook_client, auth_headers):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "broadcast", "type": "test"},
            headers=auth_headers,
        )
        assert resp.status_code == 202

    def test_no_secret_configured_returns_404(self, notification_svc):
        from litestar import Litestar
        from litestar.testing import TestClient

        app = Litestar(
            route_handlers=[NotificationsWebhookController],
            csrf_config=None,
        )
        app.state.webhook_secret = ""
        client = TestClient(app)
        resp = client.post(
            "/notifications/webhook",
            json={"target": "broadcast", "type": "test"},
            headers={"Authorization": "Bearer anything"},
        )
        assert resp.status_code == 404


class TestWebhookRateLimit:
    def test_rate_limited_after_failures(self, notification_svc):
        """Repeated bad auth triggers rate limiting."""
        from skrift.controllers import notification_webhook as mod
        from litestar import Litestar
        from litestar.testing import TestClient

        # Use a fresh limiter with max_failures=1 for test speed
        original_limiter = mod._failed_auth_limiter
        mod._failed_auth_limiter = _FailedAuthLimiter(max_failures=1, window=60.0)
        try:
            app = Litestar(
                route_handlers=[NotificationsWebhookController],
                csrf_config=None,
            )
            app.state.webhook_secret = "test-secret-token"
            client = TestClient(app)

            # First bad attempt -> 401
            resp = client.post(
                "/notifications/webhook",
                json={"target": "broadcast", "type": "test"},
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

            # Second attempt -> 429 (blocked)
            resp = client.post(
                "/notifications/webhook",
                json={"target": "broadcast", "type": "test"},
                headers={"Authorization": "Bearer correct-doesnt-matter"},
            )
            assert resp.status_code == 429
        finally:
            mod._failed_auth_limiter = original_limiter


class TestWebhookDispatch:
    @pytest.mark.asyncio
    async def test_session_target(self, webhook_client, auth_headers, notification_svc):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={
                "target": "session",
                "session_id": "sess-123",
                "type": "deploy",
                "payload": {"url": "https://example.com"},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["type"] == "deploy"
        assert "id" in data

        queued = await notification_svc.get_queued("sess-123", None)
        assert len(queued) == 1
        assert queued[0].type == "deploy"
        assert queued[0].payload == {"url": "https://example.com"}

    @pytest.mark.asyncio
    async def test_user_target(self, webhook_client, auth_headers, notification_svc):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={
                "target": "user",
                "user_id": "user-456",
                "type": "alert",
                "group": "ci",
                "payload": {"message": "Build passed"},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["type"] == "alert"

        queued = await notification_svc.get_queued("_none_", "user-456")
        assert len(queued) == 1
        assert queued[0].group == "ci"
        assert queued[0].payload == {"message": "Build passed"}

    @pytest.mark.asyncio
    async def test_broadcast_target(self, webhook_client, auth_headers, notification_svc):
        q = notification_svc.register_connection("s1", None)

        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "broadcast", "type": "maintenance"},
            headers=auth_headers,
        )
        assert resp.status_code == 202

        msg = q.get_nowait()
        assert msg.type == "maintenance"

    def test_invalid_target_returns_422(self, webhook_client, auth_headers):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "invalid", "type": "test"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_mode_forwarded(self, webhook_client, auth_headers, notification_svc):
        resp = webhook_client.post(
            "/notifications/webhook",
            json={
                "target": "session",
                "session_id": "sess-789",
                "type": "metric",
                "mode": "timeseries",
                "payload": {"value": 42},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202

        # Timeseries should not appear in queued
        queued = await notification_svc.get_queued("sess-789", None)
        assert len(queued) == 0

        # But should appear in get_since
        since = await notification_svc.get_since("sess-789", None, 0.0)
        assert len(since) == 1
        assert since[0].mode == NotificationMode.TIMESERIES

    @pytest.mark.asyncio
    async def test_group_replacement(self, webhook_client, auth_headers, notification_svc):
        """Sending two notifications with the same group replaces the first."""
        for step in ("1", "2"):
            webhook_client.post(
                "/notifications/webhook",
                json={
                    "target": "session",
                    "session_id": "sess-grp",
                    "type": "deploy",
                    "group": "pipeline",
                    "payload": {"step": step},
                },
                headers=auth_headers,
            )

        queued = await notification_svc.get_queued("sess-grp", None)
        assert len(queued) == 1
        assert queued[0].payload["step"] == "2"

    def test_missing_required_field_returns_error(self, webhook_client, auth_headers):
        """session target without session_id should fail."""
        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "session", "type": "test"},
            headers=auth_headers,
        )
        assert resp.status_code == 422 or resp.status_code == 500


class TestWebhookHook:
    """Test that WEBHOOK_NOTIFICATION_RECEIVED fires with correct args."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        hooks.clear()
        yield
        hooks.clear()

    def test_webhook_hook_fires_session(self, webhook_client, auth_headers):
        fired = []

        async def on_webhook(notification, target_type, scope_id):
            fired.append((notification.type, target_type, scope_id))

        hooks.add_action(WEBHOOK_NOTIFICATION_RECEIVED, on_webhook)

        resp = webhook_client.post(
            "/notifications/webhook",
            json={
                "target": "session",
                "session_id": "sess-hook",
                "type": "deploy",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert fired == [("deploy", "session", "sess-hook")]

    def test_webhook_hook_fires_user(self, webhook_client, auth_headers):
        fired = []

        async def on_webhook(notification, target_type, scope_id):
            fired.append((notification.type, target_type, scope_id))

        hooks.add_action(WEBHOOK_NOTIFICATION_RECEIVED, on_webhook)

        resp = webhook_client.post(
            "/notifications/webhook",
            json={
                "target": "user",
                "user_id": "user-hook",
                "type": "alert",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert fired == [("alert", "user", "user-hook")]

    def test_webhook_hook_fires_broadcast(self, webhook_client, auth_headers):
        fired = []

        async def on_webhook(notification, target_type, scope_id):
            fired.append((notification.type, target_type, scope_id))

        hooks.add_action(WEBHOOK_NOTIFICATION_RECEIVED, on_webhook)

        resp = webhook_client.post(
            "/notifications/webhook",
            json={"target": "broadcast", "type": "maintenance"},
            headers=auth_headers,
        )
        assert resp.status_code == 202
        assert fired == [("maintenance", "broadcast", None)]
