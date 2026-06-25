"""Skrift application package.

The everyday toolkit is importable from the top level::

    from skrift import FormModel, Form, auth_guard, Permission, get_settings
    from skrift import action, add_filter, do_action, apply_filters
    from skrift import flash_success, notify_user, Template, render_markdown

Subsystem modules: skrift.hooks, skrift.forms, skrift.notifications,
skrift.push, skrift.flash, skrift.template, skrift.markdown, skrift.seo,
skrift.storage, skrift.workers, skrift.agents, skrift.webhooks.
"""

import importlib

from skrift.auth.guards import Permission, Role, auth_guard
from skrift.config import get_settings, register_config_section
from skrift.flash import (
    flash_error,
    flash_info,
    flash_success,
    flash_warning,
    get_flash_messages,
)
from skrift.forms import Form, FormModel, form
from skrift.hooks import (
    action,
    add_action,
    add_filter,
    apply_filters,
    do_action,
    hooks,
)
from skrift.markdown import render_markdown
from skrift.notifications import (
    NotificationMode,
    ensure_nid,
    notify_broadcast,
    notify_session,
    notify_user,
)
from skrift.template import Template
from skrift.workers import (
    Job,
    JobCancelled,
    JobFailed,
    JobHandle,
    Pause,
    RetryPolicy,
    configure_workers,
    get_handle,
    get_runtime,
    handler,
    local_executor,
    submit,
    wake,
)
from skrift.webhooks import (
    configure_webhooks,
    enqueue as enqueue_webhook,
    enqueue_standalone as enqueue_webhook_standalone,
)

# The agents subpackage transitively materializes pydantic-ai only when an agent
# runs, but importing it eagerly here would still pull it into the import graph.
# Expose its public symbols lazily so `import skrift` (and non-agent submodules)
# stay free of the agent stack until an agent symbol is actually used.
_LAZY_AGENT_EXPORTS = {
    "Agent",
    "ApprovalContext",
    "ApprovalRejection",
    "BlobRef",
    "Chat",
    "ReasoningLevel",
    "ResumeContext",
    "Session",
    "Steer",
    "attach_artifact",
    "audit_export",
    "replay",
    "record_artifact",
    "require_approval",
    "session",
    "set_actor",
    "set_blob_store",
}


def __getattr__(name: str):
    if name in _LAZY_AGENT_EXPORTS:
        value = getattr(importlib.import_module("skrift.agents"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_AGENT_EXPORTS})


__all__ = [
    "Agent",
    "ApprovalContext",
    "ApprovalRejection",
    "BlobRef",
    "Chat",
    "Form",
    "FormModel",
    "Job",
    "JobCancelled",
    "JobFailed",
    "JobHandle",
    "NotificationMode",
    "Pause",
    "Permission",
    "ReasoningLevel",
    "ResumeContext",
    "RetryPolicy",
    "Role",
    "Session",
    "Steer",
    "Template",
    "action",
    "add_action",
    "add_filter",
    "apply_filters",
    "attach_artifact",
    "audit_export",
    "auth_guard",
    "configure_webhooks",
    "configure_workers",
    "do_action",
    "enqueue_webhook",
    "enqueue_webhook_standalone",
    "ensure_nid",
    "flash_error",
    "flash_info",
    "flash_success",
    "flash_warning",
    "form",
    "get_flash_messages",
    "get_handle",
    "get_runtime",
    "get_settings",
    "handler",
    "hooks",
    "local_executor",
    "notify_broadcast",
    "notify_session",
    "notify_user",
    "record_artifact",
    "register_config_section",
    "render_markdown",
    "replay",
    "require_approval",
    "session",
    "set_actor",
    "set_blob_store",
    "submit",
    "wake",
]
