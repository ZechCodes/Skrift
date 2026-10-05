"""The agent runtime on both supported pydantic-ai majors (#241).

pydantic-ai 2.x renamed the built-in tools module and dropped the
``builtin_tools`` kwarg, made ``AgentRun.usage`` a property, and ends a run a
tool raised in with an empty request. ``skrift.agents._compat`` bridges them;
these tests run on whichever major is installed, and CI runs both.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from pydantic_ai import WebSearchTool
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

import skrift
from skrift.agents import _compat
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
from skrift.workers.registry import registry as worker_registry


@pytest.fixture(autouse=True)
def clean_registries():
    worker_registry.clear()
    register_agent_handlers()
    agent_registry.clear()
    skrift.set_blob_store(InMemoryBlobStore())
    yield
    worker_registry.clear()
    agent_registry.clear()
    skrift.configure_workers(mode="inline")


@pytest.mark.parametrize(
    ("version", "major"),
    [("1.89.1", 1), ("1.99.0", 1), ("2.0.0b1", 2), ("2.46.0", 2), ("2.54.0", 2)],
)
def test_a_supported_version_gives_its_major(version, major):
    assert _compat.check_version(version) == major


@pytest.mark.parametrize("version", ["0.8.1", "1.0.0", "1.89.0", "3.0.0", "3.1.0a1", "unknown"])
def test_an_unsupported_version_is_refused_naming_it_and_the_range(version):
    with pytest.raises(_compat.UnsupportedPydanticAIVersion) as raised:
        _compat.check_version(version)
    assert f"pydantic-ai {version} is installed" in str(raised.value)
    assert ">=1.89.1,<3.0.0" in str(raised.value)


def test_the_installed_version_is_supported():
    assert _compat.MAJOR == _compat.check_version(_compat.VERSION)


@pytest.mark.parametrize("version", ["1.50.0", "3.0.0"])
def test_an_unsupported_install_fails_where_the_runtime_first_needs_pydantic_ai(version):
    # A fresh interpreter, so the version check runs again on a faked version.
    body = f"""
        import asyncio
        import sys

        import pydantic_ai

        pydantic_ai.__version__ = {version!r}

        import skrift
        from skrift.agents.blob import InMemoryBlobStore
        from skrift.agents.turns import decode_turn_kwargs

        skrift.configure_workers(mode="inline")
        skrift.set_blob_store(InMemoryBlobStore())
        agent = skrift.Agent("test", name="demo")
        assert "skrift.agents._compat" not in sys.modules

        message = "Skrift's agent runtime supports pydantic-ai >=1.89.1,<3.0.0, but "
        message += "pydantic-ai {version} is installed."
        first_uses = (
            lambda: agent.materialized,
            lambda: decode_turn_kwargs({{}}),
            # The worker runs the turn inline, here in this process.
            lambda: asyncio.run(agent.run("hi", dispatch="inline")),
        )
        for first_use in first_uses:
            try:
                first_use()
            except ImportError as error:
                assert type(error).__name__ == "UnsupportedPydanticAIVersion", repr(error)
                assert message in str(error), str(error)
            else:
                raise AssertionError("no error")
        """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["builtin_tools", "native_tools"])
async def test_an_agent_defined_with_native_tools_under_either_name_sends_them(name):
    seen = []

    def respond(messages, info):
        params = info.model_request_parameters
        seen.append(
            list(getattr(params, "native_tools", None) or getattr(params, "builtin_tools", None))
        )
        return ModelResponse(parts=[TextPart("ok")])

    skrift.configure_workers(mode="inline")
    agent = skrift.Agent(FunctionModel(respond), name="defined", **{name: [WebSearchTool()]})
    session = await agent.run("hi", dispatch="inline")

    assert await session.result() == "ok"
    assert seen == [[WebSearchTool()]]


def test_both_names_pass_their_tools_together_as_the_installed_major_takes_them():
    first, second = WebSearchTool(max_uses=1), WebSearchTool(max_uses=2)
    kwargs = _compat.native_tool_kwargs(
        {"builtin_tools": [first], "native_tools": [second], "model": "test"}
    )
    if _compat.MAJOR >= 2:
        from pydantic_ai.capabilities import NativeTool

        assert kwargs == {"model": "test", "capabilities": [NativeTool(first), NativeTool(second)]}
    else:
        assert kwargs == {"model": "test", "builtin_tools": [first, second]}
    assert _compat.native_tool_kwargs({"model": "test"}) == {"model": "test"}


class _MethodUsage:
    def usage(self):
        return "used"


class _PropertyUsage:
    @property
    def usage(self):
        return "used"


@pytest.mark.parametrize("run", [_MethodUsage(), _PropertyUsage()], ids=["1.x", "2.x"])
def test_run_usage_reads_a_method_or_a_property(run):
    assert _compat.run_usage(run) == "used"


class _Run:
    def __init__(self, messages):
        self.messages = messages

    def new_messages(self):
        return list(self.messages)


def test_run_new_messages_drops_a_request_the_run_never_filled():
    request = ModelRequest(parts=[UserPromptPart("hi")])
    response = ModelResponse(parts=[TextPart("ok")])

    assert _compat.run_new_messages(_Run([request, response, ModelRequest(parts=[])])) == [
        request,
        response,
    ]
    assert _compat.run_new_messages(_Run([request, response])) == [request, response]
    assert _compat.run_new_messages(_Run([])) == []
