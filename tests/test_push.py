"""Tests for Web Push notifications — VAPID keys, subscriptions, send_push, and unified notify."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skrift.lib.notifications import NotificationService, SourceRegistry


# ===========================================================================
# VAPID key generation (Tier 1)
# ===========================================================================


class TestVapidKeyGeneration:
    """Test VAPID keypair generation and caching."""

    def test_generate_vapid_keys_returns_base64url_strings(self):
        from skrift.lib.push import _generate_vapid_keys

        private, public = _generate_vapid_keys()

        # Both should be non-empty base64url strings (no padding)
        assert private and isinstance(private, str)
        assert public and isinstance(public, str)
        assert "=" not in private
        assert "=" not in public

    def test_generate_vapid_keys_produces_valid_ec_key(self):
        from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, EllipticCurvePublicKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_private_key

        from skrift.lib.push import _generate_vapid_keys

        private_b64, public_b64 = _generate_vapid_keys()

        # Decode private key (PKCS8 DER)
        private_bytes = base64.urlsafe_b64decode(private_b64 + "==")
        private_key = load_der_private_key(private_bytes, password=None)
        assert isinstance(private_key.curve, SECP256R1)

        # Decode public key (uncompressed point, 65 bytes)
        public_bytes = base64.urlsafe_b64decode(public_b64 + "==")
        assert len(public_bytes) == 65
        assert public_bytes[0] == 0x04  # Uncompressed point marker

    def test_generate_vapid_keys_unique_each_time(self):
        from skrift.lib.push import _generate_vapid_keys

        key1 = _generate_vapid_keys()
        key2 = _generate_vapid_keys()
        assert key1 != key2

    @pytest.mark.asyncio
    async def test_ensure_vapid_keys_generates_on_first_use(self):
        import skrift.lib.push as push_mod

        # Reset cache
        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None

        db_session = AsyncMock()

        # Simulate no existing keys in DB
        with patch("skrift.db.services.setting_service.get_setting", new_callable=AsyncMock, return_value=None) as mock_get, \
             patch("skrift.db.services.setting_service.set_setting", new_callable=AsyncMock) as mock_set:
            private, public = await push_mod._ensure_vapid_keys(db_session)

        assert private and public
        assert mock_set.call_count == 2  # Called for both private and public keys

        # Cleanup
        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None

    @pytest.mark.asyncio
    async def test_ensure_vapid_keys_loads_from_db(self):
        import skrift.lib.push as push_mod

        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None

        db_session = AsyncMock()

        stored_private = "stored-private-key"
        stored_public = "stored-public-key"

        async def mock_get(session, key):
            if key == push_mod.VAPID_PRIVATE_KEY:
                return stored_private
            if key == push_mod.VAPID_PUBLIC_KEY:
                return stored_public
            return None

        with patch("skrift.db.services.setting_service.get_setting", side_effect=mock_get):
            private, public = await push_mod._ensure_vapid_keys(db_session)

        assert private == stored_private
        assert public == stored_public

        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None

    @pytest.mark.asyncio
    async def test_ensure_vapid_keys_uses_cache(self):
        import skrift.lib.push as push_mod

        push_mod._vapid_private_key = "cached-private"
        push_mod._vapid_public_key = "cached-public"

        db_session = AsyncMock()
        private, public = await push_mod._ensure_vapid_keys(db_session)

        assert private == "cached-private"
        assert public == "cached-public"

        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None


# ===========================================================================
# PushSubscription model (Tier 2)
# ===========================================================================


class TestPushSubscriptionModel:
    """Test the PushSubscription SQLAlchemy model."""

    def test_model_has_required_columns(self):
        from skrift.db.models.push_subscription import PushSubscription

        assert hasattr(PushSubscription, "user_id")
        assert hasattr(PushSubscription, "endpoint")
        assert hasattr(PushSubscription, "key_p256dh")
        assert hasattr(PushSubscription, "key_auth")
        assert hasattr(PushSubscription, "last_used_at")
        assert hasattr(PushSubscription, "created_at")
        assert hasattr(PushSubscription, "updated_at")

    def test_model_tablename(self):
        from skrift.db.models.push_subscription import PushSubscription

        assert PushSubscription.__tablename__ == "push_subscriptions"

    def test_model_exported_from_package(self):
        from skrift.db.models import PushSubscription

        assert PushSubscription.__tablename__ == "push_subscriptions"


# ===========================================================================
# Subscription management (Tier 2)
# ===========================================================================


class TestSubscriptionManagement:
    """Test save/remove subscription functions."""

    @pytest.mark.asyncio
    async def test_save_subscription_creates_new(self):
        from skrift.lib.push import save_subscription

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        await save_subscription(
            mock_session, "user-123", "https://push.example.com/sub1",
            "p256dh-key", "auth-key",
        )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_subscription_updates_existing(self):
        from skrift.db.models.push_subscription import PushSubscription
        from skrift.lib.push import save_subscription

        existing = MagicMock(spec=PushSubscription)
        existing.user_id = "old-user"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute = AsyncMock(return_value=mock_result)

        await save_subscription(
            mock_session, "new-user", "https://push.example.com/sub1",
            "new-p256dh", "new-auth",
        )

        assert existing.user_id == "new-user"
        assert existing.key_p256dh == "new-p256dh"
        assert existing.key_auth == "new-auth"
        mock_session.add.assert_not_called()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_subscription_found(self):
        from skrift.lib.push import remove_subscription

        mock_sub = MagicMock()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_sub
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await remove_subscription(mock_session, "https://push.example.com/sub1")

        assert result is True
        mock_session.delete.assert_awaited_once_with(mock_sub)
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_subscription_not_found(self):
        from skrift.lib.push import remove_subscription

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await remove_subscription(mock_session, "https://push.example.com/nonexistent")

        assert result is False
        mock_session.delete.assert_not_awaited()


# ===========================================================================
# send_push (Tier 3)
# ===========================================================================


try:
    import pywebpush  # noqa: F401
    _has_pywebpush = True
except ImportError:
    _has_pywebpush = False


@pytest.mark.skipif(not _has_pywebpush, reason="pywebpush not installed")
class TestSendPush:
    """Test the send_push utility."""

    @pytest.mark.asyncio
    async def test_send_push_no_subscriptions(self):
        import skrift.lib.push as push_mod

        push_mod._vapid_private_key = "fake-key"
        push_mod._vapid_public_key = "fake-pub"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        sent = await push_mod.send_push(mock_session, "user-123", "Test", "body")
        assert sent == 0

        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None

    @pytest.mark.asyncio
    async def test_send_push_success(self):
        import skrift.lib.push as push_mod

        # Generate real keys for the test
        private, public = push_mod._generate_vapid_keys()
        push_mod._vapid_private_key = private
        push_mod._vapid_public_key = public

        mock_sub = MagicMock()
        mock_sub.id = uuid4()
        mock_sub.endpoint = "https://push.example.com/sub1"
        mock_sub.key_p256dh = "test-p256dh"
        mock_sub.key_auth = "test-auth"
        mock_sub.last_used_at = None

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_sub]
        mock_session.execute = AsyncMock(return_value=mock_result)

        import pywebpush as _pw
        with patch.object(_pw, "webpush") as mock_webpush:
            sent = await push_mod.send_push(
                mock_session, "user-123", "Test Title", "Test Body",
                url="/test", tag="test-tag",
            )

        assert sent == 1
        assert mock_sub.last_used_at is not None

        # Verify webpush was called with correct args
        call_kwargs = mock_webpush.call_args
        sub_info = call_kwargs.kwargs["subscription_info"]
        assert sub_info["endpoint"] == "https://push.example.com/sub1"
        assert sub_info["keys"]["p256dh"] == "test-p256dh"
        assert sub_info["keys"]["auth"] == "test-auth"

        data = json.loads(call_kwargs.kwargs["data"])
        assert data["title"] == "Test Title"
        assert data["body"] == "Test Body"
        assert data["url"] == "/test"
        assert data["tag"] == "test-tag"

        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None

    @pytest.mark.asyncio
    async def test_send_push_cleans_expired_subscriptions(self):
        import skrift.lib.push as push_mod

        push_mod._vapid_private_key = "fake-key"
        push_mod._vapid_public_key = "fake-pub"

        sub_id = uuid4()
        mock_sub = MagicMock()
        mock_sub.id = sub_id
        mock_sub.endpoint = "https://push.example.com/expired"
        mock_sub.key_p256dh = "p256dh"
        mock_sub.key_auth = "auth"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_sub]
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Simulate 410 Gone response
        mock_response = MagicMock()
        mock_response.status_code = 410

        from pywebpush import WebPushException
        error = WebPushException("Gone")
        error.response = mock_response

        import pywebpush as _pw
        with patch.object(_pw, "webpush", side_effect=error):
            sent = await push_mod.send_push(mock_session, "user-123", "Test", "body")

        assert sent == 0
        # Should have called execute with a DELETE for expired subscriptions
        assert mock_session.execute.call_count >= 2  # initial SELECT + DELETE
        mock_session.commit.assert_awaited()

        push_mod._vapid_private_key = None
        push_mod._vapid_public_key = None


# ===========================================================================
# Unified notify (Tier 4)
# ===========================================================================


class TestUnifiedNotify:
    """Test the unified notify function — SSE + push fallback."""

    @pytest.mark.asyncio
    async def test_notify_sends_sse_always(self):
        from skrift.lib.push import notify

        mock_session = AsyncMock()

        with patch("skrift.lib.notifications.notify_user", new_callable=AsyncMock) as mock_notify, \
             patch("skrift.lib.notifications.notifications") as mock_ns:
            # Simulate active SSE connection
            mock_ns._registry.has_listeners.return_value = True

            await notify(
                mock_session, "user-123", "test_event",
                data={"title": "Hello", "body": "World"},
            )

        mock_notify.assert_awaited_once()
        call_kwargs = mock_notify.call_args
        assert call_kwargs.args[0] == "user-123"
        assert call_kwargs.args[1] == "test_event"

    @pytest.mark.asyncio
    async def test_notify_skips_push_when_sse_connected(self):
        from skrift.lib.push import notify

        mock_session = AsyncMock()

        with patch("skrift.lib.notifications.notify_user", new_callable=AsyncMock), \
             patch("skrift.lib.notifications.notifications") as mock_ns, \
             patch("skrift.lib.push.send_push", new_callable=AsyncMock, create=True) as mock_push:
            mock_ns._registry.has_listeners.return_value = True

            await notify(
                mock_session, "user-123", "test_event",
                data={"title": "Hello"},
                push_fallback=True,
            )

        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notify_sends_push_when_no_sse(self):
        from skrift.lib.push import notify

        mock_session = AsyncMock()

        with patch("skrift.lib.notifications.notify_user", new_callable=AsyncMock), \
             patch("skrift.lib.notifications.notifications") as mock_ns, \
             patch("skrift.lib.push.send_push", new_callable=AsyncMock, create=True) as mock_push:
            mock_ns._registry.has_listeners.return_value = False
            mock_ns._registry._subscribers.get.return_value = set()

            await notify(
                mock_session, "user-123", "test_event",
                data={"title": "Push Title", "body": "Push Body", "url": "/test"},
                push_fallback=True,
            )

        mock_push.assert_awaited_once_with(
            mock_session,
            user_id="user-123",
            title="Push Title",
            body="Push Body",
            url="/test",
            tag="test_event",
        )

    @pytest.mark.asyncio
    async def test_notify_no_push_when_fallback_disabled(self):
        from skrift.lib.push import notify

        mock_session = AsyncMock()

        with patch("skrift.lib.notifications.notify_user", new_callable=AsyncMock), \
             patch("skrift.lib.notifications.notifications") as mock_ns, \
             patch("skrift.lib.push.send_push", new_callable=AsyncMock, create=True) as mock_push:
            mock_ns._registry.has_listeners.return_value = False

            await notify(
                mock_session, "user-123", "test_event",
                push_fallback=False,
            )

        mock_push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notify_checks_downstream_sessions(self):
        from skrift.lib.push import notify

        mock_session = AsyncMock()

        with patch("skrift.lib.notifications.notify_user", new_callable=AsyncMock), \
             patch("skrift.lib.notifications.notifications") as mock_ns, \
             patch("skrift.lib.push.send_push", new_callable=AsyncMock, create=True) as mock_push:
            # No direct listeners on user key
            def has_listeners(key):
                return key == "session:abc"

            mock_ns._registry.has_listeners.side_effect = has_listeners
            mock_ns._registry._subscribers.get.return_value = {"session:abc"}

            await notify(
                mock_session, "user-123", "test_event",
                data={"title": "Hello"},
                push_fallback=True,
            )

        # Should NOT send push because session:abc has listeners
        mock_push.assert_not_awaited()


# ===========================================================================
# Push hook (Tier 4 — auto-registration)
# ===========================================================================


class TestPushHook:
    """Test the NOTIFICATION_SENT hook for push fallback."""

    def test_setup_push_hook_registers_action(self):
        from skrift.lib.hooks import NOTIFICATION_SENT, hooks
        from skrift.lib.push import setup_push_hook

        hooks.clear()
        mock_session_maker = MagicMock()
        setup_push_hook(mock_session_maker)

        assert hooks.has_action(NOTIFICATION_SENT)
        hooks.clear()

    @pytest.mark.asyncio
    async def test_push_hook_fires_for_disconnected_user(self):
        from skrift.lib.hooks import NOTIFICATION_SENT, hooks
        from skrift.lib.notifications import Notification, NotificationMode
        from skrift.lib.push import setup_push_hook

        hooks.clear()

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=None)

        setup_push_hook(mock_session_maker)

        notification = Notification(
            type="test",
            payload={"title": "Test", "body": "Hello"},
            mode=NotificationMode.TIMESERIES,
        )

        with patch("skrift.lib.notifications.notifications") as mock_ns, \
             patch("skrift.lib.push.send_push", new_callable=AsyncMock, create=True) as mock_push:
            mock_ns._registry.has_listeners.return_value = False
            mock_ns._registry._subscribers.get.return_value = set()

            await hooks.do_action(NOTIFICATION_SENT, notification, "user", "user-123")

        mock_push.assert_awaited_once()
        call_kwargs = mock_push.call_args
        assert call_kwargs.kwargs["user_id"] == "user-123"
        assert call_kwargs.kwargs["title"] == "Test"

        hooks.clear()

    @pytest.mark.asyncio
    async def test_push_hook_skips_non_user_scope(self):
        from skrift.lib.hooks import NOTIFICATION_SENT, hooks
        from skrift.lib.notifications import Notification
        from skrift.lib.push import setup_push_hook

        hooks.clear()

        mock_session_maker = MagicMock()
        setup_push_hook(mock_session_maker)

        notification = Notification(type="test", payload={})

        with patch("skrift.lib.push.send_push", new_callable=AsyncMock) as mock_push:
            await hooks.do_action(NOTIFICATION_SENT, notification, "session", "abc")

        mock_push.assert_not_awaited()
        hooks.clear()


# ===========================================================================
# Service worker (Tier 5)
# ===========================================================================


class TestServiceWorker:
    """Test the service worker file exists and is valid JS."""

    def test_sw_file_exists(self):
        from pathlib import Path

        sw_path = Path(__file__).parent.parent / "skrift" / "static" / "sw.js"
        assert sw_path.is_file()

    def test_sw_handles_push_event(self):
        from pathlib import Path

        sw_path = Path(__file__).parent.parent / "skrift" / "static" / "sw.js"
        content = sw_path.read_text()
        assert 'addEventListener("push"' in content

    def test_sw_handles_notification_click(self):
        from pathlib import Path

        sw_path = Path(__file__).parent.parent / "skrift" / "static" / "sw.js"
        content = sw_path.read_text()
        assert 'addEventListener("notificationclick"' in content

    def test_push_js_exists(self):
        from pathlib import Path

        push_js_path = Path(__file__).parent.parent / "skrift" / "static" / "js" / "push.js"
        assert push_js_path.is_file()

    def test_push_js_has_subscribe_function(self):
        from pathlib import Path

        push_js_path = Path(__file__).parent.parent / "skrift" / "static" / "js" / "push.js"
        content = push_js_path.read_text()
        assert "async subscribe()" in content
        assert "async unsubscribe()" in content
        assert "async isSubscribed()" in content


# ===========================================================================
# Migration (Tier 2)
# ===========================================================================


class TestMigration:
    """Test the migration file exists and has correct structure."""

    def test_migration_file_exists(self):
        from pathlib import Path

        migration_path = Path(__file__).parent.parent / "skrift" / "alembic" / "versions" / "20260308_add_push_subscriptions.py"
        assert migration_path.is_file()

    def test_migration_has_correct_revision_chain(self):
        from skrift.alembic.versions import (
            __path__ as versions_path,
        )
        import importlib.util

        from pathlib import Path
        migration_path = Path(__file__).parent.parent / "skrift" / "alembic" / "versions" / "20260308_add_push_subscriptions.py"
        spec = importlib.util.spec_from_file_location("migration", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.down_revision == "s1t2u3v4w5x6"
        assert hasattr(mod, "upgrade")
        assert hasattr(mod, "downgrade")
