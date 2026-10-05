# Agents

Skrift agents wrap Pydantic AI agents in durable worker-backed sessions. A session keeps committed conversation history, audit events, pending tool decisions, deferred tool results, and queued user turns in runtime state.

## Guides and reference

- [Basic Agent Chat](agent-chat.md) shows string-in/string-out chat with simple tools.
- [Multi-turn Object Processing](agent-object-processing.md) shows typed outputs for stateful workflow decisions.
- [Adapting Pydantic AI Agents](pydantic-ai-agents.md) explains how to move existing Pydantic AI agents to Skrift and what the preview limitations are.
- [Agents Reference](../reference/agents.md) summarizes the public API surface.

## Supported Pydantic AI versions

The agent runtime supports Pydantic AI `>=1.89.1,<3.0.0`: the 1.x line from 1.89.1, and 2.x. The `agents` extras (`skrift[agents]`, `skrift[agents-google]`, `skrift[agents-openai]`, `skrift[agents-anthropic]`) install `pydantic-ai-slim` from that range. With another version installed, the agent runtime fails where it first needs Pydantic AI with an `ImportError` that names the installed version and the supported range: when a worker runs an agent, or when a dispatch checks a kwarg Skrift rebuilds, such as `usage_limits`. Importing `skrift` and defining agents do not check the version, and a process that only dispatches agents still does not need Pydantic AI installed at all.

Pydantic AI 2.x renamed built-in tools to native tools, `pydantic_ai.builtin_tools` to `pydantic_ai.native_tools`, and its `Agent` and run methods no longer take `builtin_tools=`: they take the tools as `NativeTool` capabilities. Skrift accepts the tools as either `builtin_tools=` or `native_tools=`, on `skrift.Agent(...)` and on every run and send, whichever major is installed, and passes them to Pydantic AI the way that major takes them. Passing both names sends both lists. Import tool classes such as `WebSearchTool` from `pydantic_ai`, which exports them under both majors.

```python
from pydantic_ai import WebSearchTool

session = await assistant.run("What changed this week?", native_tools=[WebSearchTool()])
```

## Defining an agent

```python
import skrift
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

assistant = skrift.Agent(
    GoogleModel("gemini-3.1-flash-lite-preview", provider=GoogleProvider(api_key="...")),
    name="support.assistant",
    system_prompt="Answer concisely and preserve the user's context across turns.",
)


@assistant.tool_plain
def calculate(left: float, operator: str, right: float) -> float:
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if right == 0:
        raise ValueError("Cannot divide by zero")
    return left / right
```

`Agent.run()` returns a `Session`, not the model output. Await `session.result()` when you need the current turn result.

```python
session = await assistant.run("Remember that my order number is A123.")
reply = await session.result()
```

## Multi-turn send behavior

Most application code should use the chat facade:

```python
chat = assistant.chat(key=f"user:{user.id}", actor=user.id)

reply = await chat.send(
    "Remember my order is A123.",
    model="openai:gpt-5.4-mini",
    reasoning="low",
    model_settings={"temperature": 0.2},
)
```

`chat.send()` takes a string and returns a string. The chat key maps to a durable session, so callers do not need to store or pass `session_id` for normal multi-turn chat.

`Session.send()` is chat-oriented: every incoming user message is recorded and will be processed without replacing an active run.

| Current session status | `send()` behavior |
| --- | --- |
| `completed` | Starts a new turn immediately using the committed message history. |
| `failed` | Revives the session, clears the terminal error, and starts a new turn with the existing context. |
| `cancelled` | Revives the session and starts a new turn with the existing context. |
| `queued` or `running` | Records the message in `pending_user_messages`; it will start after the active turn finishes. |
| `awaiting_approval` | Cancels pending approvals by returning denied deferred tool results, wakes the active run, and queues the new user message as the next turn. |
| `paused` | Queues the message and wakes or restarts the worker job so the session can continue. |

When an active turn finishes and queued user messages exist, the runtime emits `UserMessageActivated`, submits the next run job, and processes the next queued message. Queued turns are processed one at a time in arrival order.

A turn that raises is retried by the worker like any other job, whether the exception comes before the agent loop (for example from `deps_factory`) or inside it (from the model or a tool). A retry runs the turn again from the start of its message history, so tools called before the failure are called again, with the same or different arguments. Write tools with side effects to be safe to repeat, for example by keying writes on something that does not change between attempts. The session stays `running` between attempts, and the turn fails, with one `AgentFailed` event, only when its run job's `max_attempts` run out.

A turn that fails keeps what its last run produced before it raised: the turn's prompt, and the model responses, tool calls and tool results that followed it join the session's message history, so the next turn's model sees what the failed turn did. A retry does not see them; it runs the turn again from the history the turn started with. If the run raised while tool calls in its last model response were still unanswered, because a tool raised or the tool-call limit was reached, each of those calls gets a tool return with outcome `failed` saying the run stopped before the call returned: pydantic-ai does not accept a new prompt after unanswered tool calls. Only the turn's last run counts: if it failed before the agent loop started, such as in `deps_factory`, or was cancelled or paused, the turn keeps nothing from an earlier run.

A run that exceeds its `usage_limits` fails its turn without a retry, since another attempt would start from the same messages and reach the same limit. The turn's error records `exception_type` `UsageLimitExceeded`, its `AgentFailed` event has the cause `permanent_failure`, and the run's messages are kept as above.

Each turn runs at most once to a failure. Its run job's `max_attempts` is the whole retry budget for the turn, so a turn that has failed is never given a fresh job; if a copy of it is found on the pending queue, the runtime drops the copy with a warning and moves on to the next queued turn.

## Approvals and tools

Tools can require approval:

```python
@assistant.tool_plain(approval=True, policy_description="Modifies account state")
def close_account(account_id: str) -> str:
    ...
```

If a user sends a new message while a tool call is waiting for approval, Skrift treats that as a chat interruption. The pending approval is rejected with a cancellation reason, the model receives the denial, and the new user message is queued as the next turn.

Detached tools can run in separate worker jobs:

```python
@assistant.tool_plain(detached=True, idempotent=True)
def slow_lookup(record_id: str) -> dict:
    ...
```

Detached tool results are stored as deferred tool results and wake the parent run when available.

## Tool display messages

Tool events are durable audit records. They keep structured fields such as `tool_name`, `args`, `result`, and error data, and can also carry a `display` object for user-facing UI text.

```python
@assistant.tool_plain(
    format_called=lambda ctx: f"Checking inventory for {ctx.args['sku']}.",
    format_returned=lambda ctx: {
        "title": "Inventory checked",
        "message": f"{ctx.result['available']} units available.",
        "level": "success",
    },
    format_errored=lambda ctx: f"Inventory check failed: {ctx.error['exception_message']}",
)
def check_inventory(sku: str) -> dict:
    ...
```

`format_called`, `format_returned`, and `format_errored` can be sync or async. They receive `ToolDisplayContext` with `session_id`, `tool_call_id`, `tool_name`, `args`, and either `result` or `error` depending on the event. Return a string for simple messages or a `ToolDisplayMessage`-compatible dict with `title`, `message`, `level`, and `metadata`.

Formatter errors are logged and replaced with a generic fallback message. Use formatters for deterministic, redacted UI copy; do not rely on them to preserve audit data, because the raw structured event payload is already stored separately.

## Runtime kwargs

Skrift forwards Pydantic AI run kwargs through the durable runtime where possible:

```python
from pydantic_ai.usage import UsageLimits

session = await assistant.run(
    "Summarize this thread.",
    usage_limits=UsageLimits(request_limit=2),
    model_settings={"temperature": 0.2},
)
```

Runtime-owned values such as `deps`, committed `message_history`, and `deferred_tool_results` are merged by Skrift so session state remains durable. If you pass an explicit `session_id` that already exists, `Agent.run()` raises `AgentSessionError`; use `Session.send()` for follow-up turns.

Run kwargs are stored with the turn, so a queued turn runs with the kwargs it was sent with. The SQLAlchemy and Redis state stores save them as JSON, and Skrift rebuilds the Pydantic AI dataclasses among them, `usage_limits`, `usage`, the messages in `message_history` and the tools in `builtin_tools` or `native_tools`, before the run, so a queued turn enforces its `usage_limits` on every store. Values JSON cannot carry work only with the in-memory state store. On the others, `Agent.run()` and `Session.send()` refuse them, with any dispatch, since inline dispatch also runs its turn from the stored kwargs. `toolsets` and functions raise a serialization error. A `Model` instance raises a `TypeError`: pass `model` by name, such as `"openai:gpt-5.4-mini"`. So does a value among the rebuilt kwargs that would not come back the same, such as a `UsageLimits` subclass with fields of its own. You may pass a dict of a rebuilt kwarg's fields instead of the object, such as `usage_limits={"request_limit": 2}`, on any store. The run rebuilds it, so a dict with a key the dataclass does not have, or a value not of its field's type, raises the same `TypeError`, even with the in-memory store. That check needs Pydantic AI, so a process that only dispatches, without it installed, stores the dict as given, and the worker raises the error when the turn runs.

High-level chat sends accept the same per-turn overrides:

```python
reply = await chat.send(
    "Use a cheaper model for this one.",
    model="openai:gpt-5.4-mini",
    reasoning=skrift.ReasoningLevel.LOW,
)
```

`reasoning` accepts either a string or `ReasoningLevel`. Skrift stores it in turn metadata and maps it to Pydantic AI `model_settings["thinking"]`.

## Usage tracking

When Pydantic AI returns usage, Skrift records it per durable turn. Usage is stored on `RunState.turn_usage`, summarized in `RunState.usage_totals`, emitted as `AgentUsageRecorded`, and included in `audit_export()`.

Each usage record includes the actor, session lineage, model/provider identity, request count, tool call count, input tokens, cache read/write tokens, output tokens, audio token counters, and provider details. The admin dashboard at `/admin/agent-usage` aggregates those records per run, per agent, per actor, per model, and overall.

Skrift records usage data for later cost calculation, but it does not calculate money yet.

## Typed outputs

Use `send_typed()` when a turn should return structured data instead of a chat string.

```python
from typing import Literal
from pydantic import BaseModel


class SupportAction(BaseModel):
    action: Literal["answer", "refund", "escalate"]
    message: str


action = await chat.send_typed(
    "Customer wants a refund.",
    output_type=SupportAction,
    reasoning="medium",
)
```

This keeps the default chat path string-in/string-out while still allowing explicit typed workflows.

## Dependencies

Agents with `deps_type` must provide `deps_factory`. The factory receives a `ResumeContext` and may be sync or async.

```python
async def deps_factory(ctx: skrift.ResumeContext) -> DatabaseSession:
    return await open_session(ctx.deps_ref["tenant_id"])


assistant = skrift.Agent(
    model,
    name="tenant.assistant",
    deps_type=DatabaseSession,
    deps_factory=deps_factory,
)
```

Passing `deps_ref` for an agent that has no `deps_factory` raises `AgentSessionError` on `Agent.run()`, `Session.send()`, and the chat sends, because the reference would be silently ignored.

### Updating `deps_ref` across turns

`deps_ref` is not frozen at session creation. Every send may carry a fresh `deps_ref`, and the semantics are **replace-when-provided**:

- `deps_ref=None` (the default) leaves the session's stored `deps_ref` unchanged.
- Any dict — including an empty `{}` — replaces the stored `deps_ref` for the turn it is submitted with and every turn after it. There is no merging.

The new reference is applied to durable state at the moment the turn it rode in on **activates**, never earlier. Each queued turn therefore runs with the `deps_ref` it was submitted with, even when several sends are in flight at once:

- Two rapid sends each keep their own `deps_ref`; the second does not overwrite the first turn before it runs.
- A send that arrives while a turn is mid-flight (for example `awaiting_approval`) does not change the in-flight turn's deps when it resumes. The in-flight turn finishes with its original `deps_ref`, and the queued turn activates with the new one.

When a turn activates with a `deps_ref` that differs from the stored value, the runtime appends a `DepsRefUpdated` audit event carrying the new `deps_ref`, the `turn_id`, and the actor. No event is emitted when the provided `deps_ref` equals the stored one.

For chat, `Chat(..., deps_ref=...)` is passed on every turn — not just the first — so a chat reconstructed with a fresh `deps_ref` under the same key updates the session going forward. A per-call `chat.send(..., deps_ref=...)` or `chat.send_typed(..., deps_ref=...)` overrides the constructor default for that turn.

## Resume model

Skrift resumes agents from committed durable boundaries:

- committed model messages
- pending or resolved deferred tool calls
- approval decisions
- queued user turns
- stored run kwargs
- `deps_ref` (as last replaced by an activated turn)

The runtime does not try to resume from arbitrary internal Pydantic AI graph nodes. That avoids repeating model calls or tool work from an unsafe mid-step checkpoint.
