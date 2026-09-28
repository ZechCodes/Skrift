"""The in-process worker an agent run occupies, and its claim (#141).

An agent job claimed by the in-process worker pool holds that worker, and its
claim, for as long as it runs, including while it waits for a sub-agent. With
the in-memory queue the claim is renewed for the whole run: that queue lives in
this process, so its lease running out cannot mean the worker died, and letting
it lapse would hand the job to another worker to run a second time. A sub-agent
that still needs a worker can never run once every worker is waiting on one,
so awaiting it then fails at once instead of hanging.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from skrift.workers.memory import InMemoryQueue


class NoFreeWorkerError(RuntimeError):
    """Raised when awaiting a sub-agent that no in-process worker is free to run."""


@dataclass(eq=False)
class WorkerSlot:
    runtime: Any
    queue: InMemoryQueue
    queue_name: str
    job_id: str
    token: str


_slot: ContextVar[WorkerSlot | None] = ContextVar("skrift_agent_worker_slot", default=None)
# Per runtime, how many waits each of its workers is in.
_waiting: WeakKeyDictionary[Any, dict[WorkerSlot, int]] = WeakKeyDictionary()


@asynccontextmanager
async def occupying_worker(context: Any) -> AsyncIterator[None]:
    """Keep the claim on ``context.job`` while the code within runs on the pool
    worker that took it."""

    runtime = context.runtime
    queue = runtime.queue
    token = None
    if runtime.config.mode == "in_process" and isinstance(queue, InMemoryQueue):
        token = queue.claim_held_by(context.job.queue, context.job.id, asyncio.current_task())
    if token is None:
        yield
        return
    slot = WorkerSlot(runtime, queue, context.job.queue, context.job.id, token)
    reset = _slot.set(slot)
    keeper = asyncio.create_task(_keep_claim(slot))
    try:
        yield
    finally:
        keeper.cancel()
        with suppress(asyncio.CancelledError):
            await keeper
        _slot.reset(reset)


@asynccontextmanager
async def waiting_on_sub_agent() -> AsyncIterator[None]:
    """Count this run's worker as waiting on a sub-agent that has yet to finish.

    Raises ``NoFreeWorkerError`` if every worker would then be waiting.
    """

    slot = _slot.get()
    if slot is None:
        yield
        return
    waiting = _waiting.setdefault(slot.runtime, {})
    concurrency = slot.runtime.config.concurrency
    if len(waiting.keys() | {slot}) >= concurrency:
        raise NoFreeWorkerError(
            f"Awaiting this sub-agent would deadlock: all {concurrency} in-process "
            "worker(s) would be waiting on sub-agents, so no in-process worker is free "
            "to run it. Dispatch sub-agents whose result you await with "
            "dispatch='inline', or raise workers.concurrency."
        )
    waiting[slot] = waiting.get(slot, 0) + 1
    try:
        yield
    finally:
        waiting[slot] -= 1
        if not waiting[slot]:
            del waiting[slot]


async def _keep_claim(slot: WorkerSlot) -> None:
    lease = slot.runtime.default_visibility_timeout
    while await slot.queue.renew_claim(
        slot.queue_name, slot.job_id, slot.token, visibility_timeout=lease
    ):
        await asyncio.sleep(lease / 3)
