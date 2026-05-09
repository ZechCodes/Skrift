"""Agents used by the realtime agent demo."""

from __future__ import annotations

import os
from typing import Literal

import skrift
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider


DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"
AGENT_NAME = "agent_demo.assistant"


def build_model() -> GoogleModel:
    """Build the Gemini model used by the demo agent."""

    api_key = os.getenv("GEMINI_API_KEY") or "missing-gemini-api-key"
    return GoogleModel(
        os.getenv("AGENT_DEMO_MODEL", DEFAULT_MODEL),
        provider=GoogleProvider(api_key=api_key),
    )


assistant = skrift.Agent(
    build_model(),
    name=AGENT_NAME,
    system_prompt=(
        "You are the Skrift realtime agent demo assistant. Keep replies concise, "
        "remember the conversation across turns, and use the calculator tool for "
        "basic arithmetic so tool calls are visible in the audit trail."
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
