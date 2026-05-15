"""Tests for admin controllers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


class TestGetAdminContext:
    @pytest.mark.asyncio
    async def test_raises_without_user_id(self):
        """Should raise NotAuthorizedException if no user_id in session."""
        from litestar.exceptions import NotAuthorizedException
        from skrift.admin.helpers import get_admin_context

        request = MagicMock()
        request.session = {}
        db_session = AsyncMock()

        with pytest.raises(NotAuthorizedException):
            await get_admin_context(request, db_session)

    @pytest.mark.asyncio
    async def test_raises_for_invalid_user(self):
        """Should raise NotAuthorizedException if user not found in DB."""
        from litestar.exceptions import NotAuthorizedException
        from skrift.admin.helpers import get_admin_context

        user_id = str(uuid4())
        request = MagicMock()
        request.session = {"user_id": user_id}
        request.url.path = "/admin"

        db_session = AsyncMock()

        with (
            patch("skrift.admin.helpers.get_by_pk", new_callable=AsyncMock, return_value=None),
            pytest.raises(NotAuthorizedException),
        ):
            await get_admin_context(request, db_session)

    @pytest.mark.asyncio
    async def test_returns_context_with_nav(self):
        """Should return context dict with user, permissions, and nav."""
        from skrift.admin.helpers import get_admin_context

        user_id = str(uuid4())
        mock_user = MagicMock()
        mock_user.id = user_id

        request = MagicMock()
        request.session = {"user_id": user_id}
        request.url.path = "/admin"

        db_session = AsyncMock()

        mock_perms = MagicMock()
        mock_nav = [MagicMock()]

        with (
            patch(
                "skrift.admin.helpers.get_by_pk",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch(
                "skrift.admin.helpers.get_user_permissions",
                new_callable=AsyncMock,
                return_value=mock_perms,
            ),
            patch(
                "skrift.admin.helpers.build_admin_nav",
                new_callable=AsyncMock,
                return_value=mock_nav,
            ),
        ):
            ctx = await get_admin_context(request, db_session)

        assert ctx["user"] is mock_user
        assert ctx["permissions"] is mock_perms
        assert ctx["admin_nav"] == mock_nav
        assert ctx["current_path"] == "/admin"


class TestWorkersAdminController:
    @pytest.mark.asyncio
    async def test_workers_page_renders_snapshot(self):
        from skrift.admin.workers import WorkersAdminController

        request = MagicMock()
        db_session = AsyncMock()
        snapshot = {
            "mode": "inline",
            "concurrency": 1,
            "queues": [],
            "jobs": [],
            "handlers": [],
            "events": [],
        }

        controller = WorkersAdminController(owner=MagicMock())
        with (
            patch(
                "skrift.admin.workers.get_admin_context",
                new_callable=AsyncMock,
                return_value={"admin_nav": [], "current_path": "/admin/workers"},
            ),
            patch("skrift.admin.workers.get_flash_messages", return_value=[]),
            patch("skrift.admin.workers.get_runtime") as get_runtime,
        ):
            get_runtime.return_value.inspect = AsyncMock(return_value=snapshot)
            result = await controller.workers.fn(controller, request, db_session)

        assert result.template_name == "admin/workers.html"
        assert result.context["snapshot"] == snapshot
        assert result.context["current_path"] == "/admin/workers"

    @pytest.mark.asyncio
    async def test_workers_stream_returns_sse_response(self):
        from litestar.response.sse import ServerSentEvent
        from skrift.admin.workers import WorkersAdminController

        controller = WorkersAdminController(owner=MagicMock())
        request = MagicMock()

        with patch("skrift.admin.workers.get_runtime") as get_runtime:
            get_runtime.return_value.event_log.subscribe = MagicMock()
            result = await controller.stream.fn(controller, request)

        assert isinstance(result, ServerSentEvent)

    def test_worker_snapshot_serialization(self):
        from skrift.admin.workers import _serialize_snapshot
        from skrift.workers import HandlerRegistry, Job, WorkerRuntime

        class AdminObserved(Job):
            name: str

        local_registry = HandlerRegistry()
        local_registry.register(
            "admin_observed",
            lambda job: job.name,
            payload_model=AdminObserved,
        )
        runtime = WorkerRuntime(handler_registry=local_registry)

        async def _inspect():
            await runtime.submit(AdminObserved(name="Ada"))
            return await runtime.inspect()

        import asyncio

        payload = _serialize_snapshot(asyncio.run(_inspect()))

        assert payload["mode"] == "inline"
        assert payload["queues"][0]["oldest_ready_age_seconds"] == 0
        assert payload["queue_wait_bucket_seconds"] == 900
        assert payload["queue_trend_history"][0]["queues"][0]["queue"] == "default"
        assert payload["queue_wait_history"][0]["queues"][0]["queue"] == "default"
        assert payload["jobs"][0]["type"] == "admin_observed"
        assert payload["jobs_total"] == 1
        assert payload["jobs_active_total"] == 0
        assert payload["handlers"][0]["payload"] == "AdminObserved"
        assert payload["events"][0]["type"] == "job_completed"

    @pytest.mark.asyncio
    async def test_dlq_page_renders_entries(self):
        from skrift.admin.workers import WorkersAdminController
        from skrift.workers import DeadLetterCause, DeadLetterState
        from skrift.workers.models import DeadJobEntry, JobEnvelope

        entry = DeadJobEntry(
            job=JobEnvelope(type="admin_observed", payload={"name": "Ada"}),
            queue="default",
            job_type="admin_observed",
            cause=DeadLetterCause.RETRIES_EXHAUSTED,
            latest_error="boom",
        )
        controller = WorkersAdminController(owner=MagicMock())
        request = MagicMock()
        db_session = AsyncMock()

        with (
            patch(
                "skrift.admin.workers.get_admin_context",
                new_callable=AsyncMock,
                return_value={"admin_nav": [], "current_path": "/admin/workers/dlq"},
            ),
            patch("skrift.admin.workers.get_flash_messages", return_value=[]),
            patch("skrift.admin.workers.get_runtime") as get_runtime,
        ):
            get_runtime.return_value.inspect_dlq = AsyncMock(return_value=[entry])
            result = await controller.dlq.fn(controller, request, db_session)

        assert result.template_name == "admin/workers_dlq.html"
        assert result.context["entries"][0]["cause"] == DeadLetterCause.RETRIES_EXHAUSTED
        assert DeadLetterState.OPEN.value in result.context["states"]

    @pytest.mark.asyncio
    async def test_dlq_action_retries_selected_entries(self):
        from skrift.admin.workers import WorkersAdminController

        controller = WorkersAdminController(owner=MagicMock())
        request = MagicMock()
        data = {"entry_abc": "on", "action": "retry"}

        with (
            patch("skrift.admin.workers.flash_success"),
            patch("skrift.admin.workers.get_runtime") as get_runtime,
        ):
            get_runtime.return_value.retry_dlq_entries = AsyncMock(return_value=[])
            result = await controller.dlq_action.fn(controller, request, data)

        assert result.status_code == 302
        get_runtime.return_value.retry_dlq_entries.assert_awaited_once_with(["abc"])


class TestAgentUsageAdminController:
    @pytest.mark.asyncio
    async def test_agent_usage_page_renders_dashboard(self):
        from skrift.admin.agent_usage import AgentUsageAdminController

        controller = AgentUsageAdminController(owner=MagicMock())
        request = MagicMock()
        db_session = AsyncMock()
        dashboard = {
            "overall": {"requests_display": "1"},
            "runs": [],
            "agents": [],
            "actors": [],
            "models": [],
            "run_count": 0,
            "turn_count": 0,
        }

        with (
            patch(
                "skrift.admin.agent_usage.get_admin_context",
                new_callable=AsyncMock,
                return_value={"admin_nav": [], "current_path": "/admin/agent-usage"},
            ),
            patch("skrift.admin.agent_usage.get_flash_messages", return_value=[]),
            patch(
                "skrift.admin.agent_usage.build_agent_usage_dashboard",
                new_callable=AsyncMock,
                return_value=dashboard,
            ),
            patch("skrift.admin.agent_usage.get_runtime", return_value=MagicMock()),
        ):
            result = await controller.agent_usage.fn(controller, request, db_session)

        assert result.template_name == "admin/agent_usage.html"
        assert result.context["dashboard"] == dashboard
        assert result.context["current_path"] == "/admin/agent-usage"

    @pytest.mark.asyncio
    async def test_build_agent_usage_dashboard_groups_records(self):
        from skrift.admin.agent_usage import build_agent_usage_dashboard
        from skrift.agents.models import Actor, AgentUsageRecord, RunState

        state = RunState(
            session_id="session-123456",
            agent_name="support",
            created_by=Actor(kind="user", id="ada"),
        )
        state.turn_usage = {
            "turn-1": AgentUsageRecord(
                session_id=state.session_id,
                turn_id="turn-1",
                agent_name="support",
                actor=Actor(kind="user", id="ada"),
                model_name="gpt-test",
                requests=1,
                input_tokens=10,
                cache_read_tokens=3,
                cache_write_tokens=2,
                output_tokens=5,
            ),
            "turn-2": AgentUsageRecord(
                session_id=state.session_id,
                turn_id="turn-2",
                agent_name="support",
                actor=Actor(kind="user", id="ada"),
                model_name="gpt-test",
                requests=2,
                input_tokens=7,
                output_tokens=6,
            ),
        }

        class StateStore:
            async def keys(self, prefix=""):
                return ["runstate:session-123456"]

            async def get(self, key):
                return state

        runtime = MagicMock()
        runtime.state_store = StateStore()

        dashboard = await build_agent_usage_dashboard(runtime)

        assert dashboard["run_count"] == 1
        assert dashboard["turn_count"] == 2
        assert dashboard["overall"]["requests"] == 3
        assert dashboard["overall"]["input_tokens"] == 17
        assert dashboard["overall"]["cache_read_tokens"] == 3
        assert dashboard["overall"]["cache_write_tokens"] == 2
        assert dashboard["overall"]["output_tokens"] == 11
        assert dashboard["runs"][0]["models"] == "gpt-test"
        assert dashboard["runs"][0]["actor"] == "user:ada"
        assert dashboard["agents"][0]["label"] == "support"
        assert dashboard["actors"][0]["label"] == "user:ada"
        assert dashboard["models"][0]["label"] == "gpt-test"


class TestExtractPageFormData:
    def test_complete_valid_data(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "My Page",
            "slug": "my-page",
            "content": "Content",
            "is_published": "on",
            "order": "3",
            "publish_at": "2026-06-15T12:00:00",
            "meta_description": "SEO desc",
            "og_title": "OG Title",
            "og_description": "OG Desc",
            "og_image": "https://img.url",
            "meta_robots": "noindex",
        }
        result = extract_page_form_data(data)
        assert result.title == "My Page"
        assert result.is_published is True
        assert result.order == 3
        assert result.publish_at is not None
        assert result.meta_description == "SEO desc"
        assert result.meta_robots == "noindex"

    def test_empty_optional_fields_become_none(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "Page",
            "slug": "page",
            "content": "",
            "og_title": "",
            "og_description": "  ",
            "og_image": "",
            "meta_robots": "",
            "meta_description": "",
        }
        result = extract_page_form_data(data)
        assert result.og_title is None
        assert result.og_description is None
        assert result.og_image is None
        assert result.meta_robots is None

    def test_invalid_datetime_raises_valueerror(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "Page",
            "slug": "page",
            "content": "",
            "publish_at": "invalid-date",
        }
        with pytest.raises(ValueError, match="Invalid publish date"):
            extract_page_form_data(data)


class TestRequirePage:
    @pytest.mark.asyncio
    async def test_returns_page_if_found(self):
        from skrift.admin.helpers import require_page

        mock_page = MagicMock()
        page_id = uuid4()

        with patch("skrift.admin.helpers.page_service") as mock_ps:
            mock_ps.get_page_by_id = AsyncMock(return_value=mock_page)
            result = await require_page(AsyncMock(), page_id)
            assert result is mock_page

    @pytest.mark.asyncio
    async def test_raises_if_not_found(self):
        from skrift.admin.helpers import require_page

        with patch("skrift.admin.helpers.page_service") as mock_ps:
            mock_ps.get_page_by_id = AsyncMock(return_value=None)
            with pytest.raises(ValueError, match="Page not found"):
                await require_page(AsyncMock(), uuid4())


class TestPageListFiltering:
    @pytest.mark.asyncio
    async def test_editor_sees_all_pages(self):
        """Editor with manage-pages should see all pages."""
        from skrift.admin.page_type_factory import create_page_type_controller
        from skrift.config import PageTypeConfig

        PageController = create_page_type_controller(
            PageTypeConfig(name="page", plural="pages")
        )

        user_id = str(uuid4())
        mock_perms = MagicMock()
        mock_perms.permissions = {"manage-pages"}

        controller = PageController(owner=MagicMock())
        request = MagicMock()
        request.session = {"user_id": user_id}
        request.url.path = "/admin/pages"

        db_session = AsyncMock()
        mock_user = MagicMock()

        mock_context = {
            "user": mock_user,
            "permissions": mock_perms,
            "admin_nav": [],
            "current_path": "/admin/pages",
        }

        with patch("skrift.admin.page_type_factory.get_admin_context", new_callable=AsyncMock, return_value=mock_context), \
             patch("skrift.admin.page_type_factory.get_flash_messages", return_value=[]), \
             patch(
                 "skrift.admin.page_type_factory.list_pages_for_admin",
                 new_callable=AsyncMock,
                 return_value=[MagicMock(), MagicMock()],
             ) as mock_list_pages:

            result = await PageController.list_pages.fn(
                controller, request, db_session
            )
            assert result.template_name == "admin/pages/list.html"
            mock_list_pages.assert_awaited_once()


class TestPageMutationErrors:
    @pytest.mark.asyncio
    async def test_create_page_uses_generic_flash_on_unexpected_error(self):
        from skrift.admin.page_type_factory import create_page_type_controller
        from skrift.config import PageTypeConfig

        PageController = create_page_type_controller(
            PageTypeConfig(name="page", plural="pages")
        )
        controller = PageController(owner=MagicMock())
        request = MagicMock()
        request.session = {"user_id": str(uuid4())}
        db_session = AsyncMock()

        form = MagicMock()
        form.title = "Title"
        form.slug = "title"

        with patch("skrift.admin.page_type_factory.extract_page_form_data", return_value=form), \
             patch("skrift.admin.page_type_factory.create_typed_page", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("skrift.admin.page_type_factory.flash_error") as mock_flash, \
             patch("skrift.admin.page_type_factory.logger.exception") as mock_log:
            result = await PageController.create_page.fn(
                controller, request, db_session, {"title": "Title", "slug": "title"}
            )

        assert result.url == "/admin/pages/new"
        mock_flash.assert_called_once_with(
            request, "Could not create page. Check the server logs and try again."
        )
        mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_page_uses_generic_flash_on_unexpected_error(self):
        from skrift.admin.page_type_factory import create_page_type_controller
        from skrift.config import PageTypeConfig

        PageController = create_page_type_controller(
            PageTypeConfig(name="page", plural="pages")
        )
        controller = PageController(owner=MagicMock())
        request = MagicMock()
        request.session = {"user_id": str(uuid4())}
        db_session = AsyncMock()
        page_id = uuid4()
        page = MagicMock()

        form = MagicMock()
        form.title = "Title"
        form.slug = "title"

        with patch("skrift.admin.page_type_factory.extract_page_form_data", return_value=form), \
             patch("skrift.admin.page_type_factory.page_service.get_page_by_id", new_callable=AsyncMock, return_value=page), \
             patch("skrift.admin.page_type_factory.check_page_access", new_callable=AsyncMock), \
             patch("skrift.admin.page_type_factory.update_typed_page", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
             patch("skrift.admin.page_type_factory.flash_error") as mock_flash, \
             patch("skrift.admin.page_type_factory.logger.exception") as mock_log:
            result = await PageController.update_page.fn(
                controller,
                request,
                db_session,
                page_id,
                {"title": "Title", "slug": "title"},
            )

        assert result.url == f"/admin/pages/{page_id}/edit"
        mock_flash.assert_called_once_with(
            request, "Could not update page. Check the server logs and try again."
        )
        mock_log.assert_called_once()


class TestSettingsController:
    @pytest.mark.asyncio
    async def test_favicon_preview_failure_logs_and_renders(self):
        from skrift.admin.settings import SettingsAdminController

        controller = SettingsAdminController(owner=MagicMock())
        request = MagicMock()
        request.query_params = {}
        request.app.state.storage_manager = MagicMock()
        db_session = AsyncMock()

        request.app.state.storage_manager.get = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("skrift.admin.settings.get_admin_context", new_callable=AsyncMock, return_value={}), \
             patch("skrift.admin.settings.get_flash_messages", return_value=[]), \
             patch("skrift.admin.settings.setting_service.get_site_settings", new_callable=AsyncMock, return_value={"site_favicon_key": "favicon-key"}), \
             patch("skrift.admin.settings.importlib.metadata.version", return_value="0.1.0"), \
             patch("skrift.config.get_settings", return_value=MagicMock(sites={}, domain="")), \
             patch("skrift.admin.settings.logger.warning") as mock_log:
            result = await SettingsAdminController.site_settings.fn(
                controller, request, db_session
            )

        assert result.template_name == "admin/settings/site.html"
        assert result.context["current_favicon_url"] == ""
        mock_log.assert_called_once()
