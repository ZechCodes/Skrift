# Adapting Pydantic AI Agents

Skrift agents present an interface modeled on Pydantic AI agents — the same construction signature and the familiar `tool` and `run` concepts — but they are a separate facade rather than a `pydantic_ai.Agent` subclass. They add durable sessions, worker execution, audit events, queued turns, and chat helpers.

Defining an agent is free of Pydantic AI: `skrift.Agent(...)` records configuration and tool definitions without importing `pydantic_ai`, so web/CMS processes that only define and dispatch agents stay lean. The real `pydantic_ai.Agent` is materialized the first time the agent runs — in the worker that executes it. Because it is a facade and not a subclass, the parts of the Pydantic AI API that Skrift does not proxy (see [Limitations](#limitations)) are not available on `skrift.Agent`.

Use this guide when moving an existing Pydantic AI agent into Skrift.

## Basic conversion

Start with a Pydantic AI agent:

```python
from pydantic_ai import Agent

assistant = Agent(
    model,
    system_prompt="Answer support questions.",
)


@assistant.tool_plain
def calculate(left: float, right: float) -> float:
    return left + right
```

Convert it to `skrift.Agent` by adding a durable name:

```python
import skrift

assistant = skrift.Agent(
    model,
    name="support.assistant",
    system_prompt="Answer support questions.",
)


@assistant.tool_plain
def calculate(left: float, right: float) -> float:
    return left + right
```

The name is part of the durable runtime identity. Keep it stable across deploys.

## Running the agent

Pydantic AI code usually awaits `agent.run()` and receives a result:

```python
result = await assistant.run("Hello")
print(result.output)
```

Skrift `Agent.run()` queues or executes a durable run and returns a `Session`:

```python
session = await assistant.run("Hello")
print(await session.result())
```

For application chat, prefer the high-level chat API:

```python
chat = assistant.chat(key=f"user:{user.id}", actor=user.id)
reply = await chat.send("Hello")
```

## Dependencies

If your Pydantic AI agent uses `deps_type`, Skrift requires a `deps_factory` so dependencies can be recreated when a worker resumes a run.

```python
async def deps_factory(ctx: skrift.ResumeContext) -> AppDeps:
    return AppDeps(
        tenant_id=ctx.deps_ref["tenant_id"],
        db=await open_db_session(),
    )


assistant = skrift.Agent(
    model,
    name="support.assistant",
    deps_type=AppDeps,
    deps_factory=deps_factory,
)
```

Pass serializable dependency references when starting the chat or run:

```python
chat = assistant.chat(
    key=f"tenant:{tenant.id}:user:{user.id}",
    deps_ref={"tenant_id": tenant.id},
)
```

## Run kwargs

Skrift forwards common Pydantic AI run kwargs through durable state:

```python
reply = await chat.send(
    "Summarize this.",
    model="openai:gpt-5.4-mini",
    model_settings={"temperature": 0.2},
    usage_limits=UsageLimits(request_limit=2),
    metadata={"route": "summary"},
    reasoning="low",
)
```

Skrift owns and merges these kwargs:

- `deps`
- committed `message_history`
- `deferred_tool_results`
- `output_type` wrapping for deferred tools

Use `deps_ref` plus `deps_factory` instead of passing live dependencies through `deps`.

## Output types

For plain chat, use `chat.send()`:

```python
reply: str = await chat.send("Write a reply.")
```

For structured output, use `chat.send_typed()`:

```python
decision = await chat.send_typed(
    "Classify this request.",
    output_type=SupportAction,
)
```

At the lower level, `Agent.run(..., output_type=SupportAction)` and `Session.send(..., output_type=SupportAction)` are also supported, but the chat API is the intended preview surface.

## Tools

Normal Pydantic AI tools work for in-run execution:

```python
@assistant.tool
def lookup(ctx: RunContext[AppDeps], account_id: str) -> str:
    return find_account(ctx.deps, account_id)
```

Plain tools can be marked for durability policies:

```python
@assistant.tool_plain(
    approval=True,
    policy_description="Charges a customer account.",
)
def charge(account_id: str, amount: float) -> str:
    ...
```

Detached plain tools run in their own worker jobs:

```python
@assistant.tool_plain(detached=True, idempotent=True)
def slow_lookup(record_id: str) -> dict:
    ...
```

Skrift tools also accept deterministic display formatters:

```python
@assistant.tool_plain(
    format_called=lambda ctx: f"Looking up {ctx.args['record_id']}.",
    format_returned=lambda ctx: "Lookup complete.",
    format_errored=lambda ctx: f"Lookup failed: {ctx.error['exception_message']}",
)
def lookup(record_id: str) -> dict:
    ...
```

Formatter output is stored as `payload.display` on tool audit events. Raw tool arguments, results, and errors remain available in the structured event payload.

## System prompts and instructions

Dynamic system prompts and instructions port over unchanged. `@agent.system_prompt` (bare or `@agent.system_prompt(dynamic=True)`) and `@agent.instructions` are proxied onto the underlying Pydantic AI agent at run time:

```python
@assistant.system_prompt
def base_prompt() -> str:
    return "Answer support questions."


@assistant.system_prompt(dynamic=True)
def tenant_prompt(ctx: RunContext[AppDeps]) -> str:
    return f"You are helping tenant {ctx.deps.tenant_id}."


@assistant.instructions
def style() -> str:
    return "Be concise."
```

Static prompts can also stay on the constructor (`system_prompt="..."`, `instructions="..."`).

Output validators port over too, with one caveat — see [Limitations](#limitations):

```python
from pydantic_ai import ModelRetry


@assistant.output_validator
def reject_pii(text: str) -> str:
    if contains_pii(text):
        raise ModelRetry("Remove personal data and try again.")
    return text
```

## Limitations

Skrift preview support intentionally avoids a few unsafe or unresolved areas:

- `skrift.Agent` is a facade, not a `pydantic_ai.Agent` subclass. It proxies construction, `tool`/`tool_plain`, `system_prompt`/`instructions`/`output_validator`, and durable `run()`/`chat()`, but the rest of the Pydantic AI agent API is not exposed on it — for example `run_sync`, `run_stream`, `iter`, and `isinstance(agent, pydantic_ai.Agent)`. Drive runs through the Skrift session surface.
- Output validators and a per-turn `output_type` override cannot be combined (a Pydantic AI constraint). An agent with `@agent.output_validator` must declare its output type on the constructor rather than passing `output_type=` to an individual `run()` / `chat.send_typed()` call.
- The agent runtime is an optional install. The process that executes agents needs `skrift[agents]` plus a provider (for example `skrift[agents-google]`, `skrift[agents-openai]`, or `skrift[agents-anthropic]`); a process that only defines and dispatches agents needs only `skrift`. Running an agent without the runtime installed raises a `ModuleNotFoundError` pointing at the extra.
- Detached context tools are not supported yet. Use `tool_plain(detached=True)` with serializable arguments and look up resources inside the tool.
- Resume is boundary-based. Skrift resumes from committed messages, deferred tool results, approval decisions, queued user turns, and stored run kwargs. It does not resume arbitrary internal Pydantic AI graph nodes.
- Compatibility with Pydantic AI run kwargs is broad but not exhaustive. Provider-specific options should be tested in the workflow that uses them.
- Conversation history projection is intentionally simple in preview. Use audit export for full event-level inspection.
- Agent names and chat keys should be stable. Changing them creates new durable identities.

## When to use each API

| API | Use when |
| --- | --- |
| `chat.send()` | Application chat that takes and returns strings. |
| `chat.send_typed()` | A turn should return a typed object. |
| `Agent.run()` | You need a low-level durable session handle. |
| `Session.send()` | You already have a session id and need lower-level control. |
| `audit_export()` | You need full audit and lineage data. |

`audit_export()` also includes per-turn usage records and aggregate usage totals when the model provider reports usage.
