"""Draining a pool whose agent runs await sub-agents (#184).

A parent awaiting a sub-agent keeps its worker, and its claim renewed (#141),
through the drain window, so a sub-agent already running on the pool can finish
and the parent with it. A parent whose sub-agent cannot finish in the window,
because it is still running or no draining worker will claim it, is handed back
with the sub-agent's job for a successor.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
from skrift.agents.session import Session
from skrift.agents.state import load_runstate
from skrift.workers.models import JobStatus, utcnow
from skrift.workers.registry import handler
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


async def test_a_drain_hands_back_a_parent_awaiting_a_sub_agent_and_the_sub_agent():
    release = asyncio.Event()
    children: list[str] = []
    child_started = asyncio.Event()
    child = skrift.Agent(
        TestModel(call_tools=["slow"], custom_output_text="child done"), name="child"
    )
    parent = skrift.Agent(
        TestModel(call_tools=["ask_child"], custom_output_text="parent done"), name="parent"
    )

    @child.tool
    async def slow(ctx: RunContext) -> str:
        child_started.set()
        await release.wait()
        return "slow"

    @parent.tool
    async def ask_child(ctx: RunContext) -> str:
        session = await child.run("go", dispatch="queued")
        children.append(session.id)
        return str(await session.result())

    runtime = _pool(drain_timeout=0.2)
    await runtime.start()
    parent_session = await parent.run("go", dispatch="queued")
    await asyncio.wait_for(child_started.wait(), 5)

    await asyncio.wait_for(runtime.stop(), 5)

    jobs = [
        await runtime.get_job_state((await load_runstate(session_id)).current_run_job_id)
        for session_id in (parent_session.id, children[0])
    ]
    assert [(job.status, job.attempt) for job in jobs] == [(JobStatus.SUBMITTED, 0)] * 2

    release.set()
    successor = _pool(
        queue=runtime.queue, state_store=runtime.state_store, event_log=runtime.event_log
    )
    await successor.start()
    try:
        assert await asyncio.wait_for(Session(parent_session.id).result(), 5) == "parent done"
    finally:
        await asyncio.wait_for(successor.stop(), 5)


LEASE = 0.2


class Block(BaseModel):
    pass


def _parent_and_child(*, child_seconds=None):
    """A parent whose tool awaits a queued child; the child's tool sleeps
    ``child_seconds`` or, if None, waits for ``release``."""

    release, child_started = asyncio.Event(), asyncio.Event()
    calls = {"parent": 0, "child": 0}
    child = skrift.Agent(
        TestModel(call_tools=["work"], custom_output_text="child done"), name="child"
    )
    parent = skrift.Agent(
        TestModel(call_tools=["ask_child"], custom_output_text="parent done"), name="parent"
    )

    @child.tool
    async def work(ctx: RunContext) -> str:
        calls["child"] += 1
        child_started.set()
        if child_seconds is None:
            await release.wait()
        else:
            await asyncio.sleep(child_seconds)
        return "worked"

    @parent.tool
    async def ask_child(ctx: RunContext) -> str:
        calls["parent"] += 1
        session = await child.run("go", dispatch="queued")
        return str(await session.result())

    return parent, calls, release, child_started


def _pool(**config):
    return skrift.configure_workers(
        mode="in_process",
        queues=("agents", "agents-priority"),
        concurrency=2,
        visibility_timeout=LEASE,
        reaper_interval=0.02,
        poll_interval=0.01,
        max_poll_interval=0.02,
        **config,
    )


async def test_a_run_awaiting_a_sub_agent_finishes_inside_the_drain_window(caplog):
    parent, calls, _, child_started = _parent_and_child(child_seconds=4 * LEASE)
    runtime = _pool(drain_timeout=5)
    await runtime.start()
    session = await parent.run("go", dispatch="queued")
    await asyncio.wait_for(child_started.wait(), 5)
    await asyncio.sleep(2 * LEASE)  # the parent has waited past its lease

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="skrift.workers.runtime"):
        await asyncio.wait_for(runtime.stop(), 5)

    assert time.monotonic() - started < 2
    assert await asyncio.wait_for(Session(session.id).result(), 1) == "parent done"
    assert calls == {"parent": 1, "child": 1}
    assert "lost its claim" not in caplog.text


async def test_a_run_awaiting_a_sub_agent_no_draining_worker_will_claim_is_handed_back():
    parent, calls, release, _ = _parent_and_child(child_seconds=0)
    blocked = asyncio.Event()

    @handler("drain.blocker", queue="agents")
    async def blocker(payload: Block) -> None:
        blocked.set()
        await release.wait()

    runtime = _pool(drain_timeout=0.3)
    await runtime.start()
    await runtime.submit(Block())
    await blocked.wait()  # one worker is busy, so the child stays queued
    session = await parent.run("go", dispatch="queued")
    while calls["parent"] < 1:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)

    await asyncio.wait_for(runtime.stop(), 5)

    state = await load_runstate(session.id)
    job = await runtime.get_job_state(state.current_run_job_id)
    assert (job.status, job.attempt) == (JobStatus.SUBMITTED, 0)
    assert calls["child"] == 0

    release.set()
    successor = _pool(
        queue=runtime.queue, state_store=runtime.state_store, event_log=runtime.event_log
    )
    await successor.start()
    try:
        assert await asyncio.wait_for(Session(session.id).result(), 5) == "parent done"
    finally:
        await asyncio.wait_for(successor.stop(), 5)


async def test_an_abandoned_agent_run_stops_renewing_its_claim():
    # Its handler ignores the drain's cancellation and keeps running; the claim
    # it held must still expire, so another worker can take the job.
    started, release = asyncio.Event(), asyncio.Event()

    async def stubborn_deps(ctx):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    agent = skrift.Agent(
        TestModel(custom_output_text="done"),
        name="stubborn",
        deps_factory=stubborn_deps,
    )
    runtime = _pool(drain_timeout=0.05, drain_cancel_timeout=0.05)
    await runtime.start()
    await agent.run("go", dispatch="queued")
    await asyncio.wait_for(started.wait(), 5)
    try:
        (job_id,) = await asyncio.wait_for(runtime.stop(), 5)
        await asyncio.sleep(3 * LEASE)
        queue = runtime.queue
        await queue._release_expired_claims(utcnow())
        claimed = await queue.claim(["agents", "agents-priority"], visibility_timeout=30)
        assert claimed is not None and claimed.job.id == job_id
    finally:
        release.set()
        await asyncio.sleep(0.05)
