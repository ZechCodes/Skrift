"""High-level chat API for durable agents."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from skrift.agents.models import ChatState
from skrift.agents.session import Session, session as get_session
from skrift.agents.state import load_runstate
from skrift.workers import get_runtime
from skrift.workers.models import utcnow


T = TypeVar("T")


@dataclass
class Chat:
    """String-first chat facade over a durable Skrift agent session."""

    agent: Any
    key: str
    actor: Any = None
    deps_ref: dict[str, Any] | None = None
    defaults: dict[str, Any] = field(default_factory=dict)

    async def send(
        self,
        message: str,
        *,
        actor: Any = None,
        model: Any = None,
        reasoning: str | Any | None = None,
        **kwargs: Any,
    ) -> str:
        """Send a chat message and return this turn's string reply."""

        result = await self.send_typed(
            message,
            output_type=str,
            actor=actor,
            model=model,
            reasoning=reasoning,
            **kwargs,
        )
        return result if isinstance(result, str) else str(result)

    async def send_typed(
        self,
        message: str,
        *,
        output_type: type[T] | Any,
        actor: Any = None,
        model: Any = None,
        reasoning: str | Any | None = None,
        **kwargs: Any,
    ) -> T:
        """Send a message and return this turn's typed output."""

        turn_kwargs = self._turn_kwargs(model=model, reasoning=reasoning, **kwargs)
        turn_kwargs["output_type"] = output_type
        session, turn_id = await self._submit(message, actor=actor, turn_kwargs=turn_kwargs)
        return await session.result(turn_id=turn_id)

    async def status(self) -> str:
        session = await self._session_or_none()
        if session is None:
            return "idle"
        return await session.status()

    async def history(self) -> list[dict[str, Any]]:
        session = await self._session_or_none()
        if session is None:
            return []
        state = await session.state()
        messages: list[dict[str, Any]] = []
        for message in state.messages:
            if message.get("role") == "user":
                messages.append(
                    {
                        "role": "user",
                        "content": message.get("content"),
                        "turn_id": message.get("turn_id"),
                    }
                )
                continue
            content = message.get("content")
            if isinstance(content, str):
                messages.append({"role": "assistant", "content": content})
        for turn_id, output in state.turn_results.items():
            messages.append({"role": "assistant", "content": output, "turn_id": turn_id})
        return messages

    async def session(self) -> Session | None:
        return await self._session_or_none()

    async def _submit(
        self,
        message: str,
        *,
        actor: Any,
        turn_kwargs: dict[str, Any],
    ) -> tuple[Session, str]:
        session = await self._session_or_none()
        resolved_actor = actor if actor is not None else self.actor
        if session is None:
            session_id = self._session_id()
            session = await self.agent.run(
                message,
                session_id=session_id,
                actor=resolved_actor,
                deps_ref=self.deps_ref,
                **turn_kwargs,
            )
            await self._store_chat_state(session.id)
            state = await session.state()
            if state.current_turn_id is None:
                raise RuntimeError("Agent run did not create a turn id")
            return session, state.current_turn_id
        turn_id = await session.send(message, actor=resolved_actor, **turn_kwargs)
        await self._store_chat_state(session.id)
        return session, turn_id

    def _turn_kwargs(
        self,
        *,
        model: Any,
        reasoning: str | Any | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        turn_kwargs = {**self.defaults, **kwargs}
        if model is not None:
            turn_kwargs["model"] = model
        if reasoning is not None:
            turn_kwargs["reasoning"] = reasoning
        return turn_kwargs

    async def _session_or_none(self) -> Session | None:
        chat_state = await self._load_chat_state()
        session_id = chat_state.session_id if chat_state else self._session_id()
        if await load_runstate(session_id) is None:
            return None
        return await get_session(session_id)

    async def _load_chat_state(self) -> ChatState | None:
        value = await get_runtime().state_store.get(self._chat_key())
        if value is None:
            return None
        if isinstance(value, ChatState):
            return value
        return ChatState.model_validate(value)

    async def _store_chat_state(self, session_id: str) -> None:
        async def update(value: Any) -> ChatState:
            if value is None:
                return ChatState(
                    agent_name=self.agent.skrift_name,
                    key=self.key,
                    session_id=session_id,
                )
            state = value if isinstance(value, ChatState) else ChatState.model_validate(value)
            state.session_id = session_id
            state.last_active_at = utcnow()
            return state

        await get_runtime().state_store.update(self._chat_key(), update)

    def _chat_key(self) -> str:
        return f"agents:chat:{self.agent.skrift_name}:{self._key_hash()}"

    def _session_id(self) -> str:
        return f"agent_chat_{self._key_hash()}"

    def _key_hash(self) -> str:
        return hashlib.sha256(f"{self.agent.skrift_name}:{self.key}".encode()).hexdigest()[:32]
