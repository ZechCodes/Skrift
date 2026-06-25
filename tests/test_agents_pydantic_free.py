"""The site process must never import pydantic-ai just to define or dispatch agents.

Each guarantee is checked in a fresh interpreter (subprocess) because pydantic-ai
is unavoidably imported once any agent actually runs in-process — and the rest of
the agent test suite does exactly that. Running in a clean process is the only way
to observe what ``import skrift`` / defining / dispatching pulls in on its own.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def _run_in_clean_interpreter(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"subprocess failed (code {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_importing_skrift_does_not_import_pydantic_ai():
    result = _run_in_clean_interpreter(
        """
        import sys
        import skrift
        assert "pydantic_ai" not in sys.modules, "import skrift pulled in pydantic_ai"
        # Touch non-agent submodules to make sure they stay clean too.
        from skrift import flash_success, notify_user, render_markdown  # noqa: F401
        assert "pydantic_ai" not in sys.modules, "non-agent imports pulled in pydantic_ai"
        """
    )
    _assert_ok(result)


def test_defining_an_agent_does_not_import_pydantic_ai():
    result = _run_in_clean_interpreter(
        """
        import sys
        import skrift

        agent = skrift.Agent(model="gemini/gemini-1.5-flash", system_prompt="hi", name="demo")

        @agent.tool_plain
        def add(a: int, b: int) -> int:
            return a + b

        @agent.system_prompt(dynamic=True)
        def extra_prompt() -> str:
            return "be helpful"

        @agent.instructions
        def instr() -> str:
            return "be terse"

        @agent.output_validator
        def validate(data):
            return data

        assert "pydantic_ai" not in sys.modules, "defining an agent pulled in pydantic_ai"
        """
    )
    _assert_ok(result)


def test_dispatching_to_a_worker_does_not_import_pydantic_ai():
    result = _run_in_clean_interpreter(
        """
        import asyncio
        import sys
        import skrift

        # out_of_process: submit() enqueues the job to the (default in-memory)
        # queue without executing it, exactly like a real web/CMS pod handing off
        # to a separate worker pod.
        skrift.configure_workers(mode="out_of_process", queues=("agents",))
        agent = skrift.Agent(model="gemini/gemini-1.5-flash", name="demo")

        async def main():
            session = await agent.run("hi", actor="ada")
            assert session.id

        asyncio.run(main())
        assert "pydantic_ai" not in sys.modules, "dispatching an agent pulled in pydantic_ai"
        """
    )
    _assert_ok(result)


def test_materialization_imports_pydantic_ai_only_when_running():
    result = _run_in_clean_interpreter(
        """
        import asyncio
        import sys
        import skrift
        from skrift.agents.blob import InMemoryBlobStore

        skrift.configure_workers(mode="inline")
        skrift.set_blob_store(InMemoryBlobStore())

        from pydantic_ai.models.test import TestModel  # the mock model

        # Importing the mock obviously loads pydantic_ai; the point is that
        # materializing the facade reuses it and actually runs end-to-end.
        agent = skrift.Agent(TestModel(custom_output_text="pong"), name="demo")
        assert agent._materialized is None, "agent materialized before running"

        async def main():
            session = await agent.run("ping", actor="ada")
            return await session.result()

        result = asyncio.run(main())
        assert agent._materialized is not None, "agent never materialized despite running"
        assert result == "pong", f"unexpected agent output: {result!r}"
        """
    )
    _assert_ok(result)


@pytest.mark.parametrize("mode", ["inline", "in_process"])
def test_agent_runs_end_to_end_in_each_mode(mode):
    result = _run_in_clean_interpreter(
        f"""
        import asyncio
        import sys
        import skrift
        from skrift.agents.blob import InMemoryBlobStore
        from skrift.agents.state import drain_pending_outboxes
        from pydantic_ai.models.test import TestModel

        runtime = skrift.configure_workers(mode={mode!r}, queues=("agents",))
        skrift.set_blob_store(InMemoryBlobStore())
        agent = skrift.Agent(TestModel(custom_output_text="done"), name="demo")

        async def main():
            if {mode!r} == "in_process":
                await runtime.start()
            session = await agent.run("go", actor="ada")
            for _ in range(200):
                state = await session.state()
                if state.status in {{"completed", "failed"}}:
                    break
                await drain_pending_outboxes()
                await asyncio.sleep(0.01)
            assert state.status == "completed", f"status was {{state.status}}"
            assert await session.result() == "done"

        asyncio.run(main())
        """
    )
    _assert_ok(result)
