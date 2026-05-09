# Agents

Skrift agents wrap Pydantic AI agents in durable worker-backed sessions. A session keeps committed conversation history, audit events, pending tool decisions, deferred tool results, and queued user turns in runtime state.

## Guides and reference

- [Basic Agent Chat](agent-chat.md) shows string-in/string-out chat with simple tools.
- [Multi-turn Object Processing](agent-object-processing.md) shows typed outputs for stateful workflow decisions.
- [Adapting Pydantic AI Agents](pydantic-ai-agents.md) explains how to move existing Pydantic AI agents to Skrift and what the preview limitations are.
- [Agents Reference](../reference/agents.md) summarizes the public API surface.

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

High-level chat sends accept the same per-turn overrides:

```python
reply = await chat.send(
    "Use a cheaper model for this one.",
    model="openai:gpt-5.4-mini",
    reasoning=skrift.ReasoningLevel.LOW,
)
```

`reasoning` accepts either a string or `ReasoningLevel`. Skrift stores it in turn metadata and maps it to Pydantic AI `model_settings["thinking"]`.

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

## Resume model

Skrift resumes agents from committed durable boundaries:

- committed model messages
- pending or resolved deferred tool calls
- approval decisions
- queued user turns
- stored run kwargs
- `deps_ref`

The runtime does not try to resume from arbitrary internal Pydantic AI graph nodes. That avoids repeating model calls or tool work from an unsafe mid-step checkpoint.
