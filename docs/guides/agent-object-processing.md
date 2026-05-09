# Multi-turn Object Processing

Use `send_typed()` when a turn should return a structured object instead of plain chat text. This is useful for classifiers, planning workflows, extraction, routing, and stateful review flows where the user may refine the task over several turns.

## Define an output type

```python
from typing import Literal

from pydantic import BaseModel, Field


class SupportAction(BaseModel):
    action: Literal["answer", "refund", "escalate"]
    message: str
    confidence: float = Field(ge=0, le=1)
```

## Define tools and dependencies

Structured turns can still use tools and durable session context.

```python
import skrift
from pydantic_ai import RunContext


class SupportDeps(BaseModel):
    tenant_id: str


async def deps_factory(ctx: skrift.ResumeContext) -> SupportDeps:
    return SupportDeps(tenant_id=str(ctx.deps_ref["tenant_id"]))


router = skrift.Agent(
    model,
    name="support.router",
    deps_type=SupportDeps,
    deps_factory=deps_factory,
    system_prompt=(
        "Classify support requests. Use account tools when needed and return "
        "the requested structured output."
    ),
)


@router.tool
def account_tier(ctx: RunContext[SupportDeps], account_id: str) -> str:
    return lookup_account_tier(ctx.deps.tenant_id, account_id)
```

`deps_factory` can be sync or async. It receives a `ResumeContext` with the durable session id, `deps_ref`, and runtime metadata.

## Process an object

```python
chat = router.chat(
    key=f"support:{ticket.id}",
    actor=f"agent:{current_user.id}",
    deps_ref={"tenant_id": ticket.tenant_id},
)

action = await chat.send_typed(
    "Customer says order A123 arrived damaged and wants a refund.",
    output_type=SupportAction,
    reasoning="medium",
)
```

`action` is a `SupportAction` instance. The chat key keeps the underlying durable session stable, so later refinements use committed context.

```python
updated = await chat.send_typed(
    "The customer now says a replacement is acceptable.",
    output_type=SupportAction,
)
```

## Mix chat and typed turns

Use normal chat for user-facing text and typed turns for workflow decisions.

```python
summary = await chat.send("Summarize the current ticket for the support agent.")

decision = await chat.send_typed(
    "Choose the next workflow action.",
    output_type=SupportAction,
    model="openai:gpt-5.4",
    reasoning=skrift.ReasoningLevel.HIGH,
)
```

`chat.send()` always returns `str`. `chat.send_typed()` returns the requested type. Keeping the methods separate avoids surprising return types in application code.

## Queued typed turns

If a typed send arrives while another turn is running, Skrift stores the message, output type, model override, reasoning level, and other turn kwargs with the pending turn. When that turn activates, the stored configuration is decoded and passed to Pydantic AI.

This matters for multi-step object processing because a queued turn can safely request a different output type or model from the active turn.

Most server handlers should still await each send in order. If your UI deliberately allows multiple pending requests for the same chat key, Skrift stores each turn's output type and turn kwargs with that pending message before activation.

## Audit

Typed turn outputs, tool calls, approval decisions, and queued turn activations are written to the agent event stream.

```python
session = await chat.session()
audit = await skrift.audit_export(session.id)
```

Use audit export for debugging and compliance views. Use `chat.history()` only for a simplified UI projection.
