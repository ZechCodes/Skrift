"""Durable models for Skrift agents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from skrift.workers.models import Job, utcnow


AGENT_EVENT_SCHEMA_VERSION = 1


class Actor(BaseModel):
    kind: Literal["user", "service", "unknown"] = "unknown"
    id: str = "unknown"


class ResumeContext(BaseModel):
    session_id: str
    tool_call_id: str | None = None
    actor: Actor = Field(default_factory=Actor)
    deps_ref: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Steer(BaseModel):
    steer_id: str = Field(default_factory=lambda: uuid4().hex)
    text: str
    role: str = "user"
    actor: Actor = Field(default_factory=Actor)
    submitted_at: datetime = Field(default_factory=utcnow)


class ToolPolicy(BaseModel):
    approval: bool = False
    approval_mode: Literal["none", "static", "callable"] = "none"
    approval_callable_name: str | None = None
    idempotent: bool = False
    detached: bool = False
    approval_on_retry: bool = False
    policy_description: str | None = None
    format_called_name: str | None = None
    format_returned_name: str | None = None
    format_errored_name: str | None = None


class ToolDisplayMessage(BaseModel):
    title: str | None = None
    message: str
    level: Literal["info", "success", "error"] = "info"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolDisplayContext(BaseModel):
    session_id: str
    tool_call_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: dict[str, Any] | None = None
    attempt: int | None = None


class ApprovalRejection(BaseModel):
    rejected: Literal[True] = True
    reason: str
    payload: Any | None = None


class ToolExecutionState(BaseModel):
    tool_call_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utcnow)
    status: Literal["started", "executing", "completed", "errored"] = "started"
    idempotent: bool = False
    approval_on_retry: bool = False
    detached_tool_job_id: str | None = None
    result: Any = None
    error: dict[str, Any] | None = None


class ChatState(BaseModel):
    agent_name: str
    key: str
    session_id: str
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)


class AgentUsageTotals(BaseModel):
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    input_audio_tokens: int = 0
    cache_audio_read_tokens: int = 0
    output_audio_tokens: int = 0
    details: dict[str, int] = Field(default_factory=dict)


class AgentUsageRecord(AgentUsageTotals):
    session_id: str
    turn_id: str
    run_job_id: str | None = None
    agent_name: str
    actor: Actor = Field(default_factory=Actor)
    root_session_id: str | None = None
    parent_session_id: str | None = None
    model_name: str | None = None
    configured_model: str | None = None
    provider_name: str | None = None
    provider_url: str | None = None
    recorded_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboxEvent(BaseModel):
    kind: Literal["event"] = "event"
    entry_id: str = Field(default_factory=lambda: uuid4().hex)
    stream: str
    event: dict[str, Any]


class OutboxSubmit(BaseModel):
    kind: Literal["submit"] = "submit"
    entry_id: str = Field(default_factory=lambda: uuid4().hex)
    job_type: str
    payload: dict[str, Any]
    queue: str
    job_id: str


class OutboxWake(BaseModel):
    kind: Literal["wake"] = "wake"
    entry_id: str = Field(default_factory=lambda: uuid4().hex)
    job_id: str
    resume_at: datetime | None = None


OutboxEntry = OutboxEvent | OutboxSubmit | OutboxWake


class RunState(BaseModel):
    session_id: str
    agent_name: str
    status: Literal[
        "queued",
        "running",
        "awaiting_approval",
        "paused",
        "completed",
        "failed",
        "cancelled",
    ] = "queued"
    version: int = 0
    current_run_job_id: str | None = None
    current_turn_id: str | None = None
    current_tool_execution: ToolExecutionState | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    pending_user_messages: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    pending_steers: list[Steer] = Field(default_factory=list)
    deferred_tool_results: dict[str, Any] = Field(default_factory=dict)
    outbox: list[OutboxEntry] = Field(default_factory=list)
    last_seq: int = 0
    cursor: dict[str, Any] = Field(default_factory=dict)
    deps_ref: dict[str, Any] = Field(default_factory=dict)
    parent_session_id: str | None = None
    root_session_id: str | None = None
    run_kwargs: dict[str, Any] = Field(default_factory=dict)
    created_by: Actor = Field(default_factory=Actor)
    started_at: datetime | None = None
    paused_at: datetime | None = None
    terminal_at: datetime | None = None
    status_before_pause: str | None = None
    last_snapshot_at: datetime | None = None
    schema_version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)
    output: Any = None
    error: dict[str, Any] | None = None
    turn_results: dict[str, Any] = Field(default_factory=dict)
    turn_output_types: dict[str, Any] = Field(default_factory=dict)
    turn_errors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    turn_usage: dict[str, AgentUsageRecord] = Field(default_factory=dict)
    usage_totals: AgentUsageTotals = Field(default_factory=AgentUsageTotals)


class AgentRunJob(Job):
    session_id: str
    agent_name: str


class AgentToolCallJob(Job):
    session_id: str
    tool_call_id: str


class BlobRef(BaseModel):
    offload: bool = Field(default=True, alias="_offload")
    blob_id: str
    hash: str
    size: int
    content_type: str = "application/octet-stream"
