"""Tests for the notification service, SourceRegistry, and source/subscription model."""

import asyncio

import pytest

from skrift.lib.hooks import hooks, NOTIFICATION_PRE_SEND, NOTIFICATION_SENT, NOTIFICATION_DISMISSED
from skrift.lib.notifications import (
    NotDismissibleError,
    Notification,
    NotificationMode,
    NotificationService,
    SourceRegistry,
    dismiss_session_group,
    dismiss_user_group,
    notify_broadcast,
    notify_session,
    notify_source,
    notify_user,
    subscribe_source,
    unsubscribe_source,
)


@pytest.fixture
def svc():
    """Create a fresh NotificationService for each test."""
    return NotificationService()


# ===========================================================================
# SourceRegistry
# ===========================================================================


class TestSourceRegistry:
    """Test the in-memory subscription DAG and listener registry."""

    def test_subscribe_and_resolve_downstream(self):
        reg = SourceRegistry()
        reg.subscribe("session:abc", "user:alice")
        reg.subscribe("user:alice", "global")

        downstream = reg.resolve_downstream("global")
        assert downstream == {"global", "user:alice", "session:abc"}

    def test_resolve_downstream_single_node(self):
        reg = SourceRegistry()
        assert reg.resolve_downstream("session:abc") == {"session:abc"}

    def test_resolve_upstream(self):
        reg = SourceRegistry()
        reg.subscribe("session:abc", "user:alice")
        reg.subscribe("user:alice", "global")
        reg.subscribe("user:alice", "blog:tech")

        upstream = reg.resolve_upstream("session:abc")
        assert upstream == {"session:abc", "user:alice", "global", "blog:tech"}

    def test_unsubscribe(self):
        reg = SourceRegistry()
        reg.subscribe("session:abc", "user:alice")
        reg.unsubscribe("session:abc", "user:alice")

        assert reg.resolve_downstream("user:alice") == {"user:alice"}

    def test_unsubscribe_all(self):
        reg = SourceRegistry()
        reg.subscribe("session:abc", "user:alice")
        reg.subscribe("session:abc", "global")
        reg.unsubscribe_all("session:abc")

        assert reg.resolve_downstream("user:alice") == {"user:alice"}
        assert reg.resolve_downstream("global") == {"global"}

    def test_push_cascades(self):
        reg = SourceRegistry()
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()

        reg.add_listener("session:abc", q1)
        reg.add_listener("session:def", q2)
        reg.subscribe("session:abc", "user:alice")
        reg.subscribe("session:def", "user:alice")
        reg.subscribe("user:alice", "global")

        n = Notification(type="test")
        reg.push("user:alice", n)

        assert q1.get_nowait().id == n.id
        assert q2.get_nowait().id == n.id

    def test_push_to_global_cascades(self):
        reg = SourceRegistry()
        q: asyncio.Queue = asyncio.Queue()
        reg.add_listener("session:abc", q)
        reg.subscribe("session:abc", "global")

        n = Notification(type="broadcast")
        reg.push("global", n)

        assert q.get_nowait().id == n.id

    def test_push_with_custom_source(self):
        reg = SourceRegistry()
        q: asyncio.Queue = asyncio.Queue()
        reg.add_listener("session:abc", q)
        reg.subscribe("session:abc", "user:alice")
        reg.subscribe("user:alice", "blog:tech")

        n = Notification(type="new_post")
        reg.push("blog:tech", n)

        assert q.get_nowait().id == n.id

    def test_has_listeners(self):
        reg = SourceRegistry()
        q: asyncio.Queue = asyncio.Queue()
        assert reg.has_listeners("session:abc") is False
        reg.add_listener("session:abc", q)
        assert reg.has_listeners("session:abc") is True
        reg.remove_listener("session:abc", q)
        assert reg.has_listeners("session:abc") is False

    def test_idempotent_subscribe(self):
        reg = SourceRegistry()
        reg.subscribe("a", "b")
        reg.subscribe("a", "b")
        assert reg.resolve_downstream("b") == {"b", "a"}

    def test_idempotent_unsubscribe(self):
        reg = SourceRegistry()
        reg.unsubscribe("a", "b")  # no-op, shouldn't raise


# ===========================================================================
# NotificationMode
# ===========================================================================


class TestNotificationMode:
    """Test the NotificationMode enum and its effect on Notification."""

    def test_enum_values(self):
        assert NotificationMode.QUEUED.value == "queued"
        assert NotificationMode.TIMESERIES.value == "timeseries"
        assert NotificationMode.EPHEMERAL.value == "ephemeral"

    def test_default_mode_is_queued(self):
        n = Notification(type="generic")
        assert n.mode == NotificationMode.QUEUED

    def test_to_dict_includes_mode(self):
        n = Notification(type="generic")
        d = n.to_dict()
        assert d["mode"] == "queued"

    def test_to_dict_includes_created_at(self):
        n = Notification(type="generic")
        d = n.to_dict()
        assert "created_at" in d
        assert isinstance(d["created_at"], float)

    def test_to_dict_timeseries_mode(self):
        n = Notification(type="generic", mode=NotificationMode.TIMESERIES)
        d = n.to_dict()
        assert d["mode"] == "timeseries"

    def test_to_dict_ephemeral_mode(self):
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL)
        d = n.to_dict()
        assert d["mode"] == "ephemeral"


# ===========================================================================
# Ephemeral mode
# ===========================================================================


class TestEphemeralMode:
    """Test that ephemeral notifications are not stored."""

    @pytest.mark.asyncio
    async def test_ephemeral_not_stored_session(self, svc):
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL, payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 0

    @pytest.mark.asyncio
    async def test_ephemeral_not_stored_user(self, svc):
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL, payload={"msg": "hi"})
        await svc.send_to_user("u1", n)

        queued = await svc.get_queued("_none_", "u1")
        assert len(queued) == 0

    @pytest.mark.asyncio
    async def test_ephemeral_pushed_to_live_connections(self, svc):
        q = await svc.register_connection("s1", None)
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL, payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        msg = q.get_nowait()
        assert msg.id == n.id

    @pytest.mark.asyncio
    async def test_ephemeral_not_in_get_since(self, svc):
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL, payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        since = await svc.get_since("s1", None, 0.0)
        assert len(since) == 0


# ===========================================================================
# Timeseries mode
# ===========================================================================


class TestTimeseriesMode:
    """Test timeseries mode storage and retrieval."""

    @pytest.mark.asyncio
    async def test_timeseries_stored(self, svc):
        n = Notification(type="generic", mode=NotificationMode.TIMESERIES, payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        # Stored but not returned by get_queued
        queued = await svc.get_queued("s1", None)
        assert len(queued) == 0

    @pytest.mark.asyncio
    async def test_timeseries_returned_by_get_since(self, svc):
        n = Notification(type="generic", mode=NotificationMode.TIMESERIES, payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        since = await svc.get_since("s1", None, 0.0)
        assert len(since) == 1
        assert since[0].id == n.id

    @pytest.mark.asyncio
    async def test_timeseries_filtered_by_timestamp(self, svc):
        import time

        n1 = Notification(type="generic", mode=NotificationMode.TIMESERIES, payload={"msg": "old"})
        n1.created_at = time.time() - 100  # 100 seconds ago
        await svc.send_to_session("s1", n1)

        cutoff = time.time() - 50

        n2 = Notification(type="generic", mode=NotificationMode.TIMESERIES, payload={"msg": "new"})
        await svc.send_to_session("s1", n2)

        since = await svc.get_since("s1", None, cutoff)
        assert len(since) == 1
        assert since[0].id == n2.id

    @pytest.mark.asyncio
    async def test_timeseries_dismiss_raises(self, svc):
        n = Notification(type="generic", mode=NotificationMode.TIMESERIES, payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        with pytest.raises(NotDismissibleError):
            await svc.dismiss("s1", None, n.id)

    @pytest.mark.asyncio
    async def test_timeseries_user_scope(self, svc):
        n = Notification(type="generic", mode=NotificationMode.TIMESERIES, payload={"msg": "hi"})
        await svc.send_to_user("u1", n)

        queued = await svc.get_queued("_none_", "u1")
        assert len(queued) == 0

        since = await svc.get_since("_none_", "u1", 0.0)
        assert len(since) == 1
        assert since[0].id == n.id


# ===========================================================================
# Convenience functions mode param
# ===========================================================================


class TestConvenienceFunctionsModeParam:
    """Test that convenience functions forward mode correctly."""

    @pytest.mark.asyncio
    async def test_notify_session_mode_forwarded(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_session(
                "s1", "generic", mode=NotificationMode.TIMESERIES, title="Hi"
            )
            assert n.mode == NotificationMode.TIMESERIES

            # Should be in get_since, not get_queued
            queued = await svc.get_queued("s1", None)
            assert len(queued) == 0
            since = await svc.get_since("s1", None, 0.0)
            assert len(since) == 1
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_notify_user_mode_forwarded(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_user(
                "u1", "generic", mode=NotificationMode.TIMESERIES, title="Hi"
            )
            assert n.mode == NotificationMode.TIMESERIES
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_notify_broadcast_is_ephemeral(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_broadcast("generic", title="Hi")
            assert n.mode == NotificationMode.EPHEMERAL
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_notify_session_default_mode_queued(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_session("s1", "generic", title="Hi")
            assert n.mode == NotificationMode.QUEUED
        finally:
            mod.notifications = original


# ===========================================================================
# Notification group field
# ===========================================================================


class TestNotificationGroupField:
    """Test the group field on the Notification dataclass."""

    def test_group_defaults_to_none(self):
        n = Notification(type="generic")
        assert n.group is None

    def test_group_set_explicitly(self):
        n = Notification(type="generic", group="deploy")
        assert n.group == "deploy"

    def test_to_dict_excludes_group_when_none(self):
        n = Notification(type="generic")
        d = n.to_dict()
        assert "group" not in d

    def test_to_dict_includes_group_when_set(self):
        n = Notification(type="generic", group="deploy")
        d = n.to_dict()
        assert d["group"] == "deploy"

    def test_to_dict_includes_payload_and_group(self):
        n = Notification(type="generic", group="deploy", payload={"title": "Hi"})
        d = n.to_dict()
        assert d["group"] == "deploy"
        assert d["title"] == "Hi"
        assert d["type"] == "generic"


# ===========================================================================
# Send-to-session group replacement
# ===========================================================================


class TestSendToSessionGroup:
    """Test group replacement in send_to_session."""

    @pytest.mark.asyncio
    async def test_replaces_same_group_in_queue(self, svc):
        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        n2 = Notification(type="generic", group="deploy", payload={"step": "2"})

        await svc.send_to_session("s1", n1)
        await svc.send_to_session("s1", n2)

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 1
        assert queued[0].id == n2.id

    @pytest.mark.asyncio
    async def test_pushes_dismissed_event_for_old(self, svc):
        q = await svc.register_connection("s1", None)

        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_session("s1", n1)

        # Drain n1 from queue
        q.get_nowait()

        n2 = Notification(type="generic", group="deploy", payload={"step": "2"})
        await svc.send_to_session("s1", n2)

        # Should get: dismissed(n1), then n2
        dismissed = q.get_nowait()
        assert dismissed.type == "dismissed"
        assert dismissed.id == n1.id

        new_notif = q.get_nowait()
        assert new_notif.id == n2.id

    @pytest.mark.asyncio
    async def test_different_groups_coexist(self, svc):
        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        n2 = Notification(type="generic", group="build", payload={"step": "1"})

        await svc.send_to_session("s1", n1)
        await svc.send_to_session("s1", n2)

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 2

    @pytest.mark.asyncio
    async def test_no_group_notifications_unaffected(self, svc):
        n1 = Notification(type="generic", payload={"msg": "a"})
        n2 = Notification(type="generic", payload={"msg": "b"})

        await svc.send_to_session("s1", n1)
        await svc.send_to_session("s1", n2)

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 2

    @pytest.mark.asyncio
    async def test_no_dismissed_when_no_previous_group(self, svc):
        q = await svc.register_connection("s1", None)

        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_session("s1", n1)

        # Only the notification itself, no dismissed event
        msg = q.get_nowait()
        assert msg.id == n1.id
        assert q.empty()


# ===========================================================================
# Send-to-user group replacement
# ===========================================================================


class TestSendToUserGroup:
    """Test group replacement in send_to_user."""

    @pytest.mark.asyncio
    async def test_replaces_same_group_in_user_queue(self, svc):
        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        n2 = Notification(type="generic", group="deploy", payload={"step": "2"})

        await svc.send_to_user("u1", n1)
        await svc.send_to_user("u1", n2)

        queued = await svc.get_queued("_none_", "u1")
        assert len(queued) == 1
        assert queued[0].id == n2.id

    @pytest.mark.asyncio
    async def test_pushes_dismissed_to_all_user_sessions(self, svc):
        q1 = await svc.register_connection("s1", "u1")
        q2 = await svc.register_connection("s2", "u1")

        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_user("u1", n1)

        # Drain n1
        q1.get_nowait()
        q2.get_nowait()

        n2 = Notification(type="generic", group="deploy", payload={"step": "2"})
        await svc.send_to_user("u1", n2)

        # Both sessions should get dismissed then new
        for q in (q1, q2):
            dismissed = q.get_nowait()
            assert dismissed.type == "dismissed"
            assert dismissed.id == n1.id

            new_notif = q.get_nowait()
            assert new_notif.id == n2.id

    @pytest.mark.asyncio
    async def test_different_groups_coexist_user(self, svc):
        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        n2 = Notification(type="generic", group="build", payload={"step": "1"})

        await svc.send_to_user("u1", n1)
        await svc.send_to_user("u1", n2)

        queued = await svc.get_queued("_none_", "u1")
        assert len(queued) == 2


# ===========================================================================
# Convenience functions
# ===========================================================================


class TestConvenienceFunctions:
    """Test that convenience functions pass group through."""

    @pytest.mark.asyncio
    async def test_notify_session_passes_group(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_session("s1", "generic", group="deploy", title="Hi")
            assert n.group == "deploy"
            assert n.payload == {"title": "Hi"}

            queued = await svc.get_queued("s1", None)
            assert len(queued) == 1
            assert queued[0].group == "deploy"
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_notify_user_passes_group(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_user("u1", "generic", group="build", title="Done")
            assert n.group == "build"
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_notify_broadcast_passes_group(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_broadcast("generic", group="live", title="Update")
            assert n.group == "live"
            assert n.payload == {"title": "Update"}
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_notify_session_no_group(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = await notify_session("s1", "generic", title="Hi")
            assert n.group is None
        finally:
            mod.notifications = original


# ===========================================================================
# Dismiss by group
# ===========================================================================


class TestDismissByGroup:
    """Test dismiss(group=...) on NotificationService."""

    @pytest.mark.asyncio
    async def test_dismiss_by_group_removes_from_session_queue(self, svc):
        n = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_session("s1", n)

        assert await svc.dismiss("s1", None, group="deploy") is True
        assert await svc.get_queued("s1", None) == []

    @pytest.mark.asyncio
    async def test_dismiss_by_group_removes_from_user_queue(self, svc):
        n = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_user("u1", n)

        assert await svc.dismiss("s1", "u1", group="deploy") is True
        assert await svc.get_queued("_none_", "u1") == []

    @pytest.mark.asyncio
    async def test_dismiss_by_group_returns_false_when_not_found(self, svc):
        assert await svc.dismiss("s1", None, group="nonexistent") is False

    @pytest.mark.asyncio
    async def test_dismiss_by_group_pushes_dismissed_event(self, svc):
        q = await svc.register_connection("s1", None)

        n = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_session("s1", n)
        q.get_nowait()  # drain the notification

        await svc.dismiss("s1", None, group="deploy")

        dismissed = q.get_nowait()
        assert dismissed.type == "dismissed"
        assert dismissed.id == n.id

    @pytest.mark.asyncio
    async def test_dismiss_by_group_pushes_to_other_user_sessions(self, svc):
        q1 = await svc.register_connection("s1", "u1")
        q2 = await svc.register_connection("s2", "u1")

        n = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_user("u1", n)
        q1.get_nowait()
        q2.get_nowait()

        await svc.dismiss("s1", "u1", group="deploy")

        for q in (q1, q2):
            dismissed = q.get_nowait()
            assert dismissed.type == "dismissed"
            assert dismissed.id == n.id

    @pytest.mark.asyncio
    async def test_dismiss_by_group_does_not_affect_other_groups(self, svc):
        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        n2 = Notification(type="generic", group="build", payload={"step": "1"})
        await svc.send_to_session("s1", n1)
        await svc.send_to_session("s1", n2)

        await svc.dismiss("s1", None, group="deploy")

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 1
        assert queued[0].group == "build"

    @pytest.mark.asyncio
    async def test_dismiss_by_group_does_not_affect_ungrouped(self, svc):
        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        n2 = Notification(type="generic", payload={"msg": "hello"})
        await svc.send_to_session("s1", n1)
        await svc.send_to_session("s1", n2)

        await svc.dismiss("s1", None, group="deploy")

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 1
        assert queued[0].id == n2.id

    @pytest.mark.asyncio
    async def test_dismiss_by_id_still_works(self, svc):
        """Existing dismiss-by-UUID behavior is preserved."""
        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        assert await svc.dismiss("s1", None, n.id) is True
        assert await svc.get_queued("s1", None) == []

    @pytest.mark.asyncio
    async def test_dismiss_no_id_no_group_returns_false(self, svc):
        """Calling dismiss with neither id nor group finds nothing."""
        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        assert await svc.dismiss("s1", None) is False


# ===========================================================================
# Dismiss group convenience
# ===========================================================================


class TestDismissGroupConvenience:
    """Test dismiss_session_group and dismiss_user_group convenience functions."""

    @pytest.mark.asyncio
    async def test_dismiss_session_group(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            n = Notification(type="generic", group="deploy", payload={"step": "1"})
            await svc.send_to_session("s1", n)

            assert await dismiss_session_group("s1", "deploy") is True
            assert await svc.get_queued("s1", None) == []
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_dismiss_session_group_not_found(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            assert await dismiss_session_group("s1", "nope") is False
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_dismiss_user_group(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            q = await svc.register_connection("s1", "u1")

            n = Notification(type="generic", group="deploy", payload={"step": "1"})
            await svc.send_to_user("u1", n)
            q.get_nowait()  # drain

            assert await dismiss_user_group("u1", "deploy") is True
            assert await svc.get_queued("_none_", "u1") == []

            dismissed = q.get_nowait()
            assert dismissed.type == "dismissed"
            assert dismissed.id == n.id
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_dismiss_user_group_not_found(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            assert await dismiss_user_group("u1", "nope") is False
        finally:
            mod.notifications = original


# ===========================================================================
# Hooks
# ===========================================================================


class TestNotificationHooks:
    """Test hook integration in the notification service."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self, clean_hooks):
        hooks.clear()

    @pytest.fixture
    def svc(self):
        return NotificationService()

    # --- NOTIFICATION_PRE_SEND filter ---

    @pytest.mark.asyncio
    async def test_filter_can_modify_notification_session(self, svc):
        async def modify(notification, scope, scope_id):
            notification.payload["injected"] = True
            return notification

        hooks.add_filter(NOTIFICATION_PRE_SEND, modify)

        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        queued = await svc.get_queued("s1", None)
        assert len(queued) == 1
        assert queued[0].payload["injected"] is True

    @pytest.mark.asyncio
    async def test_filter_can_modify_notification_user(self, svc):
        async def modify(notification, scope, scope_id):
            notification.payload["injected"] = True
            return notification

        hooks.add_filter(NOTIFICATION_PRE_SEND, modify)

        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_user("u1", n)

        queued = await svc.get_queued("_none_", "u1")
        assert len(queued) == 1
        assert queued[0].payload["injected"] is True

    @pytest.mark.asyncio
    async def test_filter_can_modify_notification_broadcast(self, svc):
        async def modify(notification, scope, scope_id):
            notification.payload["injected"] = True
            return notification

        hooks.add_filter(NOTIFICATION_PRE_SEND, modify)

        q = await svc.register_connection("s1", None)
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL, payload={"msg": "hi"})
        await svc.broadcast(n)

        msg = q.get_nowait()
        assert msg.payload["injected"] is True

    @pytest.mark.asyncio
    async def test_filter_returning_none_suppresses_session(self, svc):
        async def suppress(notification, scope, scope_id):
            return None

        hooks.add_filter(NOTIFICATION_PRE_SEND, suppress)

        q = await svc.register_connection("s1", None)
        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        assert await svc.get_queued("s1", None) == []
        assert q.empty()

    @pytest.mark.asyncio
    async def test_filter_returning_none_suppresses_user(self, svc):
        async def suppress(notification, scope, scope_id):
            return None

        hooks.add_filter(NOTIFICATION_PRE_SEND, suppress)

        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_user("u1", n)

        assert await svc.get_queued("_none_", "u1") == []

    @pytest.mark.asyncio
    async def test_filter_returning_none_suppresses_broadcast(self, svc):
        async def suppress(notification, scope, scope_id):
            return None

        hooks.add_filter(NOTIFICATION_PRE_SEND, suppress)

        q = await svc.register_connection("s1", None)
        n = Notification(type="generic", mode=NotificationMode.EPHEMERAL, payload={"msg": "hi"})
        await svc.broadcast(n)

        assert q.empty()

    @pytest.mark.asyncio
    async def test_filter_receives_correct_scope_session(self, svc):
        received = []

        async def capture(notification, scope, scope_id):
            received.append((scope, scope_id))
            return notification

        hooks.add_filter(NOTIFICATION_PRE_SEND, capture)

        await svc.send_to_session("sess-42", Notification(type="generic"))
        assert received == [("session", "sess-42")]

    @pytest.mark.asyncio
    async def test_filter_receives_correct_scope_user(self, svc):
        received = []

        async def capture(notification, scope, scope_id):
            received.append((scope, scope_id))
            return notification

        hooks.add_filter(NOTIFICATION_PRE_SEND, capture)

        await svc.send_to_user("user-99", Notification(type="generic"))
        assert received == [("user", "user-99")]

    @pytest.mark.asyncio
    async def test_filter_receives_correct_scope_broadcast(self, svc):
        received = []

        async def capture(notification, scope, scope_id):
            received.append((scope, scope_id))
            return notification

        hooks.add_filter(NOTIFICATION_PRE_SEND, capture)

        await svc.broadcast(Notification(type="generic", mode=NotificationMode.EPHEMERAL))
        assert received == [("broadcast", None)]

    # --- NOTIFICATION_SENT action ---

    @pytest.mark.asyncio
    async def test_sent_action_fires_session(self, svc):
        fired = []

        async def on_sent(notification, scope, scope_id):
            fired.append((notification.type, scope, scope_id))

        hooks.add_action(NOTIFICATION_SENT, on_sent)

        await svc.send_to_session("s1", Notification(type="deploy"))
        assert fired == [("deploy", "session", "s1")]

    @pytest.mark.asyncio
    async def test_sent_action_fires_user(self, svc):
        fired = []

        async def on_sent(notification, scope, scope_id):
            fired.append((notification.type, scope, scope_id))

        hooks.add_action(NOTIFICATION_SENT, on_sent)

        await svc.send_to_user("u1", Notification(type="alert"))
        assert fired == [("alert", "user", "u1")]

    @pytest.mark.asyncio
    async def test_sent_action_fires_broadcast(self, svc):
        fired = []

        async def on_sent(notification, scope, scope_id):
            fired.append((notification.type, scope, scope_id))

        hooks.add_action(NOTIFICATION_SENT, on_sent)

        await svc.broadcast(Notification(type="maintenance", mode=NotificationMode.EPHEMERAL))
        assert fired == [("maintenance", "broadcast", None)]

    @pytest.mark.asyncio
    async def test_sent_action_does_not_fire_when_suppressed(self, svc):
        async def suppress(notification, scope, scope_id):
            return None

        hooks.add_filter(NOTIFICATION_PRE_SEND, suppress)

        fired = []

        async def on_sent(notification, scope, scope_id):
            fired.append(True)

        hooks.add_action(NOTIFICATION_SENT, on_sent)

        await svc.send_to_session("s1", Notification(type="deploy"))
        assert fired == []

    # --- NOTIFICATION_DISMISSED action ---

    @pytest.mark.asyncio
    async def test_dismissed_action_fires(self, svc):
        fired = []

        async def on_dismissed(notification_id):
            fired.append(notification_id)

        hooks.add_action(NOTIFICATION_DISMISSED, on_dismissed)

        n = Notification(type="generic", payload={"msg": "hi"})
        await svc.send_to_session("s1", n)

        await svc.dismiss("s1", None, n.id)
        assert fired == [n.id]

    @pytest.mark.asyncio
    async def test_dismissed_action_does_not_fire_when_not_found(self, svc):
        fired = []

        async def on_dismissed(notification_id):
            fired.append(notification_id)

        hooks.add_action(NOTIFICATION_DISMISSED, on_dismissed)

        await svc.dismiss("s1", None, group="nonexistent")
        assert fired == []


# ===========================================================================
# Source/subscription model
# ===========================================================================


class TestSourceSubscriptionModel:
    """Test the new source/subscription features of NotificationService."""

    @pytest.mark.asyncio
    async def test_custom_source_cascades_to_subscribed_user(self, svc):
        """Publishing to blog:tech cascades to user:alice → session:abc."""
        q = await svc.register_connection("abc", "alice")

        # Subscribe user:alice to blog:tech (persistent in InMemory)
        await svc.subscribe("user:alice", "blog:tech")

        n = Notification(type="new_post", mode=NotificationMode.EPHEMERAL)
        await svc.send("blog:tech", n)

        msg = q.get_nowait()
        assert msg.id == n.id

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_cascade(self, svc):
        q = await svc.register_connection("abc", "alice")

        await svc.subscribe("user:alice", "blog:tech")
        await svc.unsubscribe("user:alice", "blog:tech")

        n = Notification(type="new_post", mode=NotificationMode.EPHEMERAL)
        await svc.send("blog:tech", n)

        assert q.empty()

    @pytest.mark.asyncio
    async def test_get_queued_includes_upstream_sources(self, svc):
        """get_queued should pull from all upstream storage keys."""
        await svc.register_connection("abc", "alice")
        await svc.subscribe("user:alice", "blog:tech")

        n = Notification(type="stored_post", group=None)
        await svc.send("blog:tech", n)

        queued = await svc.get_queued("abc", "alice")
        assert len(queued) == 1
        assert queued[0].id == n.id

    @pytest.mark.asyncio
    async def test_global_broadcast_reaches_all_sessions(self, svc):
        q1 = await svc.register_connection("s1", None)
        q2 = await svc.register_connection("s2", "user1")

        n = Notification(type="alert", mode=NotificationMode.EPHEMERAL)
        await svc.broadcast(n)

        assert q1.get_nowait().id == n.id
        assert q2.get_nowait().id == n.id

    @pytest.mark.asyncio
    async def test_user_notification_reaches_all_user_sessions(self, svc):
        q1 = await svc.register_connection("s1", "alice")
        q2 = await svc.register_connection("s2", "alice")

        n = Notification(type="user_msg", mode=NotificationMode.EPHEMERAL)
        await svc.send_to_user("alice", n)

        assert q1.get_nowait().id == n.id
        assert q2.get_nowait().id == n.id

    @pytest.mark.asyncio
    async def test_session_teardown_cleans_edges(self, svc):
        q = await svc.register_connection("abc", "alice")
        svc.unregister_connection("abc", q)

        # Session should no longer receive notifications
        n = Notification(type="test", mode=NotificationMode.EPHEMERAL)
        await svc.send_to_user("alice", n)

        assert q.empty()

    @pytest.mark.asyncio
    async def test_notify_source_convenience(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            q = await svc.register_connection("abc", "alice")
            await svc.subscribe("user:alice", "blog:tech")

            n = await notify_source("blog:tech", "new_post", mode=NotificationMode.EPHEMERAL, title="New Post")
            assert n.type == "new_post"
            assert n.payload == {"title": "New Post"}

            msg = q.get_nowait()
            assert msg.id == n.id
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_subscribe_source_convenience(self):
        svc = NotificationService()

        from skrift.lib import notifications as mod
        original = mod.notifications
        mod.notifications = svc
        try:
            q = await svc.register_connection("abc", "alice")
            await subscribe_source("user:alice", "blog:tech")

            n = Notification(type="post", mode=NotificationMode.EPHEMERAL)
            await svc.send("blog:tech", n)

            msg = q.get_nowait()
            assert msg.id == n.id

            await unsubscribe_source("user:alice", "blog:tech")

            n2 = Notification(type="post2", mode=NotificationMode.EPHEMERAL)
            await svc.send("blog:tech", n2)

            assert q.empty()
        finally:
            mod.notifications = original

    @pytest.mark.asyncio
    async def test_persistent_subs_loaded_on_connect(self, svc):
        """Persistent subscriptions from InMemoryBackend are loaded on first connect."""
        backend = svc._get_backend()
        await backend.add_subscription("user:alice", "blog:tech")

        q = await svc.register_connection("abc", "alice")

        # Now blog:tech should cascade
        n = Notification(type="post", mode=NotificationMode.EPHEMERAL)
        await svc.send("blog:tech", n)

        msg = q.get_nowait()
        assert msg.id == n.id

    @pytest.mark.asyncio
    async def test_self_echo_prevention(self, svc):
        """Messages with the same publisher_id should be ignored."""
        q = await svc.register_connection("s1", None)

        # Simulate a remote message from the same publisher
        await svc._handle_remote({
            "a": "s",
            "sk": "session:s1",
            "pid": svc._publisher_id,
            "n": Notification(type="echo").to_dict(),
        })

        assert q.empty()

    @pytest.mark.asyncio
    async def test_remote_message_from_other_replica(self, svc):
        """Messages from different publisher_id should be delivered."""
        q = await svc.register_connection("s1", None)

        n = Notification(type="remote")
        await svc._handle_remote({
            "a": "s",
            "sk": "session:s1",
            "pid": "other-replica-id",
            "n": n.to_dict(),
        })

        msg = q.get_nowait()
        assert msg.id == n.id
