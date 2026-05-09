"""Handler registry for Skrift workers."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel

from skrift.workers.models import RetryPolicy


@dataclass(frozen=True)
class HandlerDescriptor:
    """Registered handler metadata."""

    job_type: str
    func: Callable[..., Any]
    payload_model: type[BaseModel]
    queue: str
    retry_policy: RetryPolicy
    visibility_timeout: float
    dead_callback: Callable[..., Any] | None = None


class HandlerRegistry:
    """Maps job type names and payload models to handlers."""

    def __init__(self) -> None:
        self._by_type: dict[str, HandlerDescriptor] = {}
        self._by_model: dict[type[BaseModel], str] = {}

    def register(
        self,
        job_type: str,
        func: Callable[..., Any],
        *,
        payload_model: type[BaseModel] | None = None,
        localns: dict[str, Any] | None = None,
        queue: str = "default",
        retry_policy: RetryPolicy | None = None,
        max_attempts: int | None = None,
        visibility_timeout: float = 30.0,
    ) -> HandlerDescriptor:
        if job_type in self._by_type:
            raise ValueError(f"Handler already registered for job type {job_type!r}")
        model = payload_model or self._infer_payload_model(func, localns=localns)
        if not issubclass(model, BaseModel):
            raise TypeError("Worker payload model must be a Pydantic BaseModel subclass")
        policy = retry_policy or RetryPolicy()
        if max_attempts is not None:
            policy = policy.model_copy(update={"max_attempts": max_attempts})
        descriptor = HandlerDescriptor(
            job_type=job_type,
            func=func,
            payload_model=model,
            queue=queue,
            retry_policy=policy,
            visibility_timeout=visibility_timeout,
        )
        self._by_type[job_type] = descriptor
        self._by_model[model] = job_type
        return descriptor

    def set_dead_callback(
        self,
        job_type: str,
        callback: Callable[..., Any],
    ) -> HandlerDescriptor:
        descriptor = self.get(job_type)
        updated = HandlerDescriptor(
            job_type=descriptor.job_type,
            func=descriptor.func,
            payload_model=descriptor.payload_model,
            queue=descriptor.queue,
            retry_policy=descriptor.retry_policy,
            visibility_timeout=descriptor.visibility_timeout,
            dead_callback=callback,
        )
        self._by_type[job_type] = updated
        self._by_model[updated.payload_model] = job_type
        return updated

    def get(self, job_type: str) -> HandlerDescriptor:
        try:
            return self._by_type[job_type]
        except KeyError as exc:
            raise KeyError(f"No worker handler registered for job type {job_type!r}") from exc

    def list_handlers(self) -> list[HandlerDescriptor]:
        """Return registered handlers sorted by job type."""
        return [self._by_type[key] for key in sorted(self._by_type)]

    def job_type_for_payload(self, payload: BaseModel) -> str:
        for model, job_type in self._by_model.items():
            if isinstance(payload, model):
                return job_type
        raise KeyError(f"No worker handler registered for payload model {type(payload).__name__}")

    def clear(self) -> None:
        self._by_type.clear()
        self._by_model.clear()

    @staticmethod
    def _infer_payload_model(
        func: Callable[..., Any], *, localns: dict[str, Any] | None = None
    ) -> type[BaseModel]:
        signature = inspect.signature(func)
        parameters = list(signature.parameters.values())
        if not parameters:
            raise TypeError("Worker handlers must accept a payload argument")
        first = parameters[0]
        hints = get_type_hints(func, localns=localns)
        annotation = hints.get(first.name, first.annotation)
        if annotation is inspect.Signature.empty:
            raise TypeError("Worker handlers need a payload type annotation or payload_model")
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            raise TypeError("Worker handler payload annotation must be a BaseModel subclass")
        return annotation


registry = HandlerRegistry()


def handler(
    job_type: str,
    *,
    payload_model: type[BaseModel] | None = None,
    queue: str = "default",
    retry_policy: RetryPolicy | None = None,
    max_attempts: int | None = None,
    visibility_timeout: float = 30.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a worker handler at import time."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        frame = inspect.currentframe()
        caller_locals = frame.f_back.f_locals.copy() if frame and frame.f_back else None
        registry.register(
            job_type,
            func,
            payload_model=payload_model,
            localns=caller_locals,
            queue=queue,
            retry_policy=retry_policy,
            max_attempts=max_attempts,
            visibility_timeout=visibility_timeout,
        )

        def on_dead(callback: Callable[..., Any]) -> Callable[..., Any]:
            registry.set_dead_callback(job_type, callback)
            return callback

        setattr(func, "on_dead", on_dead)
        return func

    return decorator
