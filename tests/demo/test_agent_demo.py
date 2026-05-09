"""Tests for the realtime agent demo project."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

import skrift
import skrift.config as config_mod
import skrift.lib.notifications as notifications_mod
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
from skrift.config import clear_settings_cache, set_config_path
from skrift.lib.notifications import NotificationMode, NotificationService
from skrift.workers.registry import registry as worker_registry


DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo" / "agent-demo"


class FakeRequest:
    def __init__(self, payload: dict):
        self.session: dict = {}
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    if str(DEMO_ROOT) not in sys.path:
        sys.path.insert(0, str(DEMO_ROOT))
    worker_registry.clear()
    register_agent_handlers()
    agent_registry.clear()
    skrift.configure_workers(mode="inline")
    yield
    worker_registry.clear()
    agent_registry.clear()
    config_mod._config_path_override = None
    clear_settings_cache()


def test_compose_config_uses_distributed_workers_notifications_and_agents(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://skrift_test:skrift_test@localhost:15433/skrift_test",
    )
    set_config_path(DEMO_ROOT / "compose.app.yaml")

    settings = config_mod.get_settings()

    assert settings.workers.preset == "distributed"
    assert settings.workers.queues == ["agents"]
    assert "agentdemo.agents" in settings.workers.imports
    assert ".redis:RedisStateStore" in settings.workers.backends.state_store
    assert ".redis:RedisQueue" in settings.workers.backends.queue
    assert settings.notifications.backend == "skrift.lib.notification_backends:RedisBackend"
    assert settings.agents.default_queue == "agents"
    assert settings.agents.tool_call_queue == "agents"
    assert settings.agents.blob_backend == "skrift.agents.blob:ArchiveBlobStore"


def test_demo_agent_exposes_basic_calculator_tool():
    agents = importlib.import_module("agentdemo.agents")

    assert agents.calculate(3, "+", 4) == 7
    assert agents.calculate(10, "-", 2) == 8
    assert agents.calculate(6, "*", 7) == 42
    assert agents.calculate(8, "/", 2) == 4
    with pytest.raises(ValueError, match="divide by zero"):
        agents.calculate(1, "/", 0)


async def test_agent_event_watcher_emits_timeseries_notifications(monkeypatch):
    controllers = importlib.import_module("agentdemo.controllers")
    svc = NotificationService()
    monkeypatch.setattr("skrift.lib.notifications.notifications", svc)
    monkeypatch.setattr(controllers, "notify_session", notifications_mod.notify_session)

    agent = skrift.Agent(TestModel(custom_output_text="hello from demo"), name="demo.watch")
    session = await agent.run("hi", actor="ada")

    await controllers._watch_agent_events("browser-session", session.id)
    notifications = await svc.get_since("browser-session", None, 0)

    assert notifications
    assert all(item.mode == NotificationMode.TIMESERIES for item in notifications)
    assert any(item.type == "agent.message" for item in notifications)
    assert any(item.payload["session_id"] == session.id for item in notifications)


async def test_agent_event_watcher_continues_across_queued_turn(monkeypatch):
    controllers = importlib.import_module("agentdemo.controllers")
    svc = NotificationService()
    monkeypatch.setattr("skrift.lib.notifications.notifications", svc)
    monkeypatch.setattr(controllers, "notify_session", notifications_mod.notify_session)

    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello from demo"), name="demo.watch.queue")
    session = await agent.run("first", actor="ada")
    await session.send("second", actor="ada")
    await runtime.start()
    try:
        assert await session.result() == "hello from demo"
    finally:
        await runtime.stop()

    await controllers._watch_agent_events("browser-session", session.id)
    notifications = await svc.get_since("browser-session", None, 0)

    assert any(item.payload.get("event_type") == "UserMessageActivated" for item in notifications)
    assert sum(1 for item in notifications if item.type == "agent.message") == 2


async def test_controller_starts_session_and_queues_watcher(monkeypatch):
    controllers = importlib.import_module("agentdemo.controllers")
    svc = NotificationService()
    monkeypatch.setattr("skrift.lib.notifications.notifications", svc)
    monkeypatch.setattr(controllers, "notify_session", notifications_mod.notify_session)
    monkeypatch.setattr(controllers, "_gemini_configured", lambda: True)
    monkeypatch.setattr(
        controllers,
        "assistant",
        skrift.Agent(TestModel(custom_output_text="controller reply"), name="demo.controller"),
    )

    request = FakeRequest({"message": "hello"})
    controller = object.__new__(controllers.AgentDemoController)
    response = await controllers.AgentDemoController.start_session.fn(controller, request)
    await asyncio.sleep(0)

    try:
        assert response["ok"] is True
        assert response["session_id"]
        notifications = await svc.get_since(request.session["_nid"], None, 0)
        assert any(item.type == "agent.session.started" for item in notifications)
    finally:
        await controllers.stop_agent_demo_watchers(None)


async def test_audit_page_renders_agent_audit_for_session():
    controllers = importlib.import_module("agentdemo.controllers")
    agent = skrift.Agent(TestModel(custom_output_text="audit reply"), name="demo.audit")
    session = await agent.run("audit this", actor="ada")
    request = FakeRequest({})
    request.query_params = {"session_id": session.id}
    controller = object.__new__(controllers.AgentDemoController)

    response = await controllers.AgentDemoController.audit.fn(controller, request)

    assert response.template_name == "agent-demo/audit.html"
    assert response.context["session_id"] == session.id
    assert response.context["audit"]["session_id"] == session.id
    assert "AgentCompleted" in response.context["audit_json"]
