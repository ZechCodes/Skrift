"""Tests for the in-memory bot state store."""

import asyncio
import time

import pytest

from skrift.bot_detection.store import InMemoryBotStateStore


class TestInMemoryBotStateStore:
    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_key(self):
        store = InMemoryBotStateStore()
        assert await store.get("ns", "missing") is None

    @pytest.mark.asyncio
    async def test_set_then_get_round_trip(self):
        store = InMemoryBotStateStore()
        await store.set("ns", "key", "value", ttl=60)
        assert await store.get("ns", "key") == "value"

    @pytest.mark.asyncio
    async def test_namespace_isolates_keys(self):
        store = InMemoryBotStateStore()
        await store.set("ns1", "key", "v1", ttl=60)
        await store.set("ns2", "key", "v2", ttl=60)
        assert await store.get("ns1", "key") == "v1"
        assert await store.get("ns2", "key") == "v2"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self):
        store = InMemoryBotStateStore()
        await store.set("ns", "key", "value", ttl=60)
        await store.delete("ns", "key")
        assert await store.get("ns", "key") is None

    @pytest.mark.asyncio
    async def test_expired_key_returns_none(self, monkeypatch):
        store = InMemoryBotStateStore()
        await store.set("ns", "key", "value", ttl=1)

        # Simulate clock advance past TTL.
        original_monotonic = time.monotonic
        future = original_monotonic() + 5
        monkeypatch.setattr(time, "monotonic", lambda: future)

        assert await store.get("ns", "key") is None
