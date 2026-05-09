"""Demo site that continuously submits random local worker jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import timedelta
from typing import Any

import skrift
from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse

from skrift.lib.hooks import AFTER_USER_CREATED, APP_SHUTDOWN, APP_STARTUP, action
from skrift.workers.models import utcnow


logger = logging.getLogger(__name__)
_generator_task: asyncio.Task | None = None


class DemoJob(skrift.Job):
    """A noisy demo job."""

    label: str
    steps: int
    fail_chance: float = 0.0
    pause_chance: float = 0.0


@skrift.handler("demo.render_asset", queue="demo-fast", max_attempts=3)
async def render_asset(job: DemoJob, context) -> dict[str, Any]:
    return await _run_noisy_job("render_asset", job, context)


@skrift.handler("demo.sync_index", queue="demo-slow", max_attempts=2)
async def sync_index(job: DemoJob, context) -> dict[str, Any]:
    return await _run_noisy_job("sync_index", job, context)


@skrift.handler("demo.webhook_delivery", queue="demo-io", max_attempts=4)
async def webhook_delivery(job: DemoJob, context) -> dict[str, Any]:
    return await _run_noisy_job("webhook_delivery", job, context)


async def _run_noisy_job(kind: str, job: DemoJob, context) -> dict[str, Any]:
    """Emit random demo-domain events while lifecycle events hit the observer."""
    completed = int(context.paused_state.get("completed", 0))
    for step in range(completed + 1, job.steps + 1):
        await context.emit(
            f"demo:job:{context.job.id}",
            {
                "kind": kind,
                "label": job.label,
                "step": step,
                "steps": job.steps,
                "message": random.choice(
                    [
                        "loaded inputs",
                        "processed chunk",
                        "waited on dependency",
                        "wrote output",
                        "published progress",
                    ]
                ),
                "timestamp": utcnow().isoformat(),
            },
        )
        await asyncio.sleep(random.uniform(0.12, 0.75))

        if step < job.steps and random.random() < job.pause_chance:
            return skrift.Pause(
                resume_at=utcnow() + timedelta(seconds=random.uniform(0.5, 2.0)),
                state={"completed": step},
            )

    if random.random() < job.fail_chance:
        raise RuntimeError(f"{kind} demo failure for {job.label}")

    return {"kind": kind, "label": job.label, "steps": job.steps}


async def _submit_random_jobs() -> None:
    runtime = skrift.get_runtime()
    counter = 0
    job_types = [
        "demo.render_asset",
        "demo.sync_index",
        "demo.webhook_delivery",
    ]
    while True:
        try:
            counter += 1
            job_type = random.choice(job_types)
            handle = await runtime.submit(
                job_type,
                DemoJob(
                    label=f"demo-{counter:04d}",
                    steps=random.randint(2, 7),
                    fail_chance=random.choice([0.0, 0.0, 0.1, 0.2]),
                    pause_chance=random.choice([0.0, 0.15, 0.3]),
                ),
            )
            logger.info("Submitted demo worker job %s (%s)", handle.id, job_type)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker demo job generator failed to submit a job")

        await asyncio.sleep(random.uniform(0.35, 2.0))


async def _submit_manual_job() -> str:
    runtime = skrift.get_runtime()
    handle = await runtime.submit(
        "demo.render_asset",
        DemoJob(
            label=f"manual-{random.randint(1000, 9999)}",
            steps=random.randint(2, 5),
            fail_chance=0.0,
            pause_chance=0.15,
        ),
    )
    logger.info("Submitted manual demo worker job %s", handle.id)
    return handle.id


@action(APP_STARTUP)
async def start_worker_demo(_app) -> None:
    """Start the local worker runtime and random job generator."""
    global _generator_task
    from skrift.config import get_settings

    settings = get_settings()
    if settings.workers.enabled:
        runtime = skrift.get_runtime()
        logger.info("Worker demo using configured %s runtime", runtime.config.mode)
    else:
        runtime = skrift.configure_workers(
            mode="in_process",
            queues=("demo-fast", "demo-slow", "demo-io"),
            concurrency=4,
        )
        await runtime.start()
        logger.info("Worker demo started local in-process runtime")
    if _generator_task is None or _generator_task.done():
        _generator_task = asyncio.create_task(
            _submit_random_jobs(),
            name="worker-demo-generator",
        )
        logger.info("Worker demo job generator started")


@action(APP_SHUTDOWN)
async def stop_worker_demo(_app) -> None:
    """Stop the demo generator and worker runtime."""
    global _generator_task
    if _generator_task is not None:
        _generator_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _generator_task
        _generator_task = None
    await skrift.get_runtime().stop()


@action(AFTER_USER_CREATED)
async def make_demo_users_admin(login_result, request) -> None:
    """Let a fresh dummy-login user open the admin observer immediately."""
    from skrift.auth.services import assign_role_to_user

    session_maker = request.app.state.session_maker_class
    async with session_maker() as session:
        await assign_role_to_user(session, login_result.user.id, "admin")


class WorkerDemoController(Controller):
    """Small landing page for the worker demo."""

    path = "/"

    @get("/")
    async def index(self, request: Request) -> TemplateResponse:
        return TemplateResponse(
            "worker-demo/index.html",
            context={"user": request.session.get("user_id")},
        )

    @post("/jobs")
    async def submit_job(self) -> Redirect:
        await _submit_manual_job()
        return Redirect(path="/admin/workers")
