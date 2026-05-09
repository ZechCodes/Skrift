# Agents Reference

This page summarizes the public preview APIs for durable Skrift agents.

## `skrift.Agent`

```python
agent = skrift.Agent(
    model,
    name="support.assistant",
    system_prompt="...",
    deps_type=AppDeps,
    deps_factory=deps_factory,
)
```

`Agent` subclasses Pydantic AI's `Agent` and registers itself with the Skrift agent runtime.

| Parameter | Description |
| --- | --- |
| `model` | Any Pydantic AI model accepted by `pydantic_ai.Agent`. |
| `name` | Required stable durable identity for the agent. |
| `deps_factory` | Optional sync or async callable receiving `ResumeContext` and returning dependencies. Required when `deps_type` is set. |
| `**kwargs` | Forwarded to Pydantic AI `Agent` construction. |

### `Agent.chat()`

```python
chat = agent.chat(
    key="user:123",
    actor="123",
    deps_ref={"tenant_id": "acme"},
    reasoning="low",
)
```

Creates a high-level chat facade for a stable conversation key.

| Argument | Description |
| --- | --- |
| `key` | Stable application conversation key. The same key reuses the same durable session. |
| `actor` | Optional default actor for audit events. May be a string, dict, or `Actor`. |
| `deps_ref` | Serializable dependency reference passed to `deps_factory`. |
| `**defaults` | Default turn kwargs applied to `send()` and `send_typed()`. |

### `Agent.run()`

```python
session = await agent.run(
    "Hello",
    actor="123",
    deps_ref={"tenant_id": "acme"},
    model="openai:gpt-5.4-mini",
    reasoning="low",
)
```

Starts a low-level durable run and returns `Session`.

| Argument | Description |
| --- | --- |
| `user_prompt` | Initial prompt for the run. |
| `dispatch` | `"queued"` or `"same_worker"`. |
| `session_id` | Optional explicit durable session id. Raises `AgentSessionError` if it already exists. |
| `actor` | Actor written to audit events. |
| `deps_ref` | Serializable dependency reference. |
| `parent_session_id` / `root_session_id` | Optional lineage overrides for sub-agent workflows. |
| `**kwargs` | Pydantic AI run kwargs plus Skrift `reasoning`. |

## `skrift.Chat`

High-level string-first interface over a durable session.

### `Chat.send()`

```python
reply = await chat.send(
    "Remember my order is A123.",
    model="openai:gpt-5.4-mini",
    reasoning="low",
    model_settings={"temperature": 0.2},
)
```

Takes a string and returns a string.

Supported turn kwargs include:

- `model`
- `model_settings`
- `usage_limits`
- `metadata`
- `instructions`
- `toolsets`
- `builtin_tools`
- `capabilities`
- `spec`
- `reasoning`

`reasoning` may be a string or `ReasoningLevel`. Skrift maps it to `model_settings["thinking"]` and stores it in metadata as `skrift_reasoning`.

### `Chat.send_typed()`

```python
result = await chat.send_typed(
    "Classify this request.",
    output_type=SupportAction,
    reasoning="medium",
)
```

Takes a string and returns the requested output type.

| Argument | Description |
| --- | --- |
| `message` | User message for this turn. |
| `output_type` | Pydantic-compatible output type for this turn. |
| `actor` | Optional actor override for this turn. |
| `model` | Optional model override for this turn. |
| `reasoning` | Optional string or `ReasoningLevel`. |
| `**kwargs` | Additional Pydantic AI run kwargs. |

### `Chat.status()`

```python
status = await chat.status()
```

Returns `"idle"` if no durable session exists yet, otherwise the backing session status.

### `Chat.history()`

```python
messages = await chat.history()
```

Returns a simplified message projection for UI previews. Use `audit_export()` for complete event history.

### `Chat.session()`

```python
session = await chat.session()
```

Returns the backing `Session` if one exists, otherwise `None`.

## `skrift.Session`

Low-level durable run handle.

| Method | Description |
| --- | --- |
| `state()` | Return the full `RunState`. |
| `status()` | Return current status. |
| `messages()` | Return raw durable message records. |
| `lineage()` | Return parent/root session ids. |
| `send(message, **kwargs)` | Record a new user turn and return its `turn_id`. |
| `result(turn_id=None)` | Wait for session output or a specific turn output. |
| `pause()` | Pause a queued/running/approval session. |
| `resume()` | Resume a paused session. |
| `cancel()` | Cancel the current run. |
| `approve(tool_call_id)` | Approve a pending tool call. |
| `reject(tool_call_id, reason=...)` | Reject a pending tool call. |
| `steer(text)` | Queue steering text for the next model request. |
| `async for position, event in session` | Stream agent events. |

Session statuses:

- `queued`
- `running`
- `awaiting_approval`
- `paused`
- `completed`
- `failed`
- `cancelled`

## Tool Policies

`Agent.tool()` and `Agent.tool_plain()` accept Skrift policy metadata:

```python
@agent.tool_plain(
    approval=True,
    idempotent=True,
    detached=True,
    approval_on_retry=True,
    policy_description="Writes to an external system.",
)
def write_record(record_id: str) -> str:
    ...
```

| Option | Description |
| --- | --- |
| `approval` | Require human or application approval before returning the tool result to the model. |
| `idempotent` | Marks the tool safe to retry from the application's perspective. |
| `detached` | Run a plain tool in a separate worker job. |
| `approval_on_retry` | Request approval again when retrying. |
| `policy_description` | Human-readable explanation stored in snapshots and audit data. |

Detached context tools registered with `@agent.tool(detached=True)` are not supported in preview.

## `ReasoningLevel`

```python
skrift.ReasoningLevel.MINIMAL
skrift.ReasoningLevel.LOW
skrift.ReasoningLevel.MEDIUM
skrift.ReasoningLevel.HIGH
skrift.ReasoningLevel.XHIGH
```

All APIs also accept plain strings for reasoning values.

## `ResumeContext`

Passed to `deps_factory`.

| Field | Description |
| --- | --- |
| `session_id` | Durable session id. |
| `tool_call_id` | Current tool call id when relevant. |
| `actor` | Actor associated with the resume context. |
| `deps_ref` | Serializable dependency reference supplied at chat/run creation. |
| `metadata` | Runtime metadata, including worker job id when running in a worker. |

## Audit and Replay

```python
events = await skrift.replay(session_id)
audit = await skrift.audit_export(session_id, include_lineage=True)
```

`audit_export()` dereferences large offloaded payloads and includes lineage sessions by default.
