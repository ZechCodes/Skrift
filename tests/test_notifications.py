"""Tests for the notification service group key feature."""

import pytest

from skrift.lib.notifications import (
    Notification,
    NotificationService,
    dismiss_session_group,
    dismiss_user_group,
    notify_broadcast,
    notify_session,
    notify_user,
)


@pytest.fixture
def svc():
    """Create a fresh NotificationService for each test."""
    return NotificationService()


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
        q = svc.register_connection("s1", None)

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
        q = svc.register_connection("s1", None)

        n1 = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_session("s1", n1)

        # Only the notification itself, no dismissed event
        msg = q.get_nowait()
        assert msg.id == n1.id
        assert q.empty()


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
        q1 = svc.register_connection("s1", "u1")
        q2 = svc.register_connection("s2", "u1")

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
        q = svc.register_connection("s1", None)

        n = Notification(type="generic", group="deploy", payload={"step": "1"})
        await svc.send_to_session("s1", n)
        q.get_nowait()  # drain the notification

        await svc.dismiss("s1", None, group="deploy")

        dismissed = q.get_nowait()
        assert dismissed.type == "dismissed"
        assert dismissed.id == n.id

    @pytest.mark.asyncio
    async def test_dismiss_by_group_pushes_to_other_user_sessions(self, svc):
        q1 = svc.register_connection("s1", "u1")
        q2 = svc.register_connection("s2", "u1")

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
            q = svc.register_connection("s1", "u1")

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
