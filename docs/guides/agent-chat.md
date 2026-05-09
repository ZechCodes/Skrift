# Basic Agent Chat

This guide builds a string-in/string-out agent chat with simple tools. Use this path when your application is a normal chat surface and the agent should answer with text.

## Define the agent

```python
from typing import Literal

import skrift
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider


assistant = skrift.Agent(
    GoogleModel("gemini-3.1-flash-lite-preview", provider=GoogleProvider(api_key="...")),
    name="docs.chat_assistant",
    system_prompt=(
        "You are a concise support assistant. Keep useful context across turns "
        "and use tools when they make the answer more accurate."
    ),
)


@assistant.tool_plain
def calculate(
    left: float,
    operator: Literal["+", "-", "*", "/"],
    right: float,
) -> float:
    """Calculate a basic arithmetic operation."""

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

`@tool_plain` tools are the simplest durable tools because the tool receives only serializable arguments. Context tools are supported for normal in-run execution, but detached context tools are not supported yet.

## Send chat messages

Create a chat handle with a stable key. Skrift maps the key to a durable session, so app code does not need to store a session id.

```python
chat = assistant.chat(key=f"user:{user.id}", actor=user.id)

reply = await chat.send("Remember that my order number is A123.")
follow_up = await chat.send("What was my order number?")
```

`chat.send()` returns the string output for the specific turn it submitted. If another turn is already active, Skrift queues the new message and waits for that queued turn to run.

## Override model settings per turn

Every send can override the model, model settings, usage limits, metadata, and reasoning level.

```python
reply = await chat.send(
    "Use a cheaper model for this question.",
    model="openai:gpt-5.4-mini",
    reasoning="low",
    model_settings={"temperature": 0.2},
)
```

`reasoning` accepts a string or `skrift.ReasoningLevel`. Skrift records it in turn metadata and passes it to Pydantic AI as `model_settings["thinking"]`.

```python
reply = await chat.send(
    "Think harder about this account issue.",
    reasoning=skrift.ReasoningLevel.HIGH,
)
```

## Chat behavior

`chat.send()` uses the same durable semantics as `Session.send()`:

- If the backing session is idle or completed, the next message starts immediately.
- If the session is running, the message is recorded as a pending turn.
- If the session is waiting for tool approval, new input rejects the pending approval with a cancellation reason and queues the new message.
- If the previous run failed or was cancelled, the new message revives the session with committed conversation context intact.

## Inspect the chat

Use the high-level helpers for normal application UI state.

```python
status = await chat.status()
history = await chat.history()
session = await chat.session()
```

`history()` returns a simplified projection suitable for previews and basic UI. Use the lower-level `Session` or audit APIs when you need full event history.

```python
audit = await skrift.audit_export(session.id)
```
