"""Durable agent subsystem."""

from skrift.agents.agent import Agent
from skrift.agents.approval import ApprovalContext, require_approval
from skrift.agents.artifacts import attach_artifact, record_artifact
from skrift.agents.audit import AuditTrail, audit_export, replay
from skrift.agents.blob import (
    ArchiveBlobStore,
    BlobIntegrityError,
    InMemoryBlobStore,
    set_blob_store,
)
from skrift.agents.chat import Chat
from skrift.agents.context import set_actor
from skrift.agents.models import (
    AgentUsageRecord,
    AgentUsageTotals,
    ApprovalRejection,
    BlobRef,
    ResumeContext,
    Steer,
    ToolDisplayContext,
    ToolDisplayMessage,
)
from skrift.agents.registry import registry
from skrift.agents.session import AgentSessionError, Session, session
from skrift.agents.turns import ReasoningLevel

# Register worker handlers so agent jobs can be dispatched. This is
# pydantic-ai-free: the real (pydantic-ai-backed) runtime is imported lazily
# only when a job actually executes.
from skrift.agents.handlers import register_agent_handlers

register_agent_handlers()

__all__ = [
    "Agent",
    "AgentSessionError",
    "AgentUsageRecord",
    "AgentUsageTotals",
    "ApprovalContext",
    "ApprovalRejection",
    "ArchiveBlobStore",
    "AuditTrail",
    "attach_artifact",
    "BlobIntegrityError",
    "BlobRef",
    "Chat",
    "InMemoryBlobStore",
    "ResumeContext",
    "ReasoningLevel",
    "Session",
    "Steer",
    "ToolDisplayContext",
    "ToolDisplayMessage",
    "audit_export",
    "registry",
    "replay",
    "record_artifact",
    "require_approval",
    "session",
    "set_actor",
    "set_blob_store",
]
