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


class TestInMemoryBotStateStoreBounds:
    """Keys are client IPs: a client that trips a metric once and never
    returns must not leave a permanent entry behind."""

    @pytest.mark.asyncio
    async def test_expired_entries_are_swept_without_being_read(self, monkeypatch):
        store = InMemoryBotStateStore(sweep_every=10)
        for index in range(50):
            await store.set("ns", f"gone-{index}", "value", ttl=1)

        future = time.monotonic() + 30
        monkeypatch.setattr(time, "monotonic", lambda: future)

        # Enough writes to trip the amortized sweep.
        for index in range(10):
            await store.set("ns", f"fresh-{index}", "value", ttl=600)

        assert len(store._data) == 10

    @pytest.mark.asyncio
    async def test_sweep_keeps_live_entries(self, monkeypatch):
        store = InMemoryBotStateStore(sweep_every=4)
        await store.set("ns", "long-lived", "value", ttl=600)
        await store.set("ns", "short-lived", "value", ttl=1)

        future = time.monotonic() + 30
        monkeypatch.setattr(time, "monotonic", lambda: future)

        for index in range(4):
            await store.set("ns", f"other-{index}", "value", ttl=600)

        assert await store.get("ns", "long-lived") == "value"
        assert await store.get("ns", "short-lived") is None

    @pytest.mark.asyncio
    async def test_max_entries_evicts_oldest_first(self):
        store = InMemoryBotStateStore(max_entries=3)
        for name in ("a", "b", "c", "d"):
            await store.set("ns", name, name, ttl=600)

        assert len(store._data) == 3
        assert await store.get("ns", "a") is None
        assert await store.get("ns", "d") == "d"

    @pytest.mark.asyncio
    async def test_rewriting_a_key_refreshes_its_eviction_order(self):
        store = InMemoryBotStateStore(max_entries=2)
        await store.set("ns", "a", "a", ttl=600)
        await store.set("ns", "b", "b", ttl=600)
        await store.set("ns", "a", "a2", ttl=600)
        await store.set("ns", "c", "c", ttl=600)

        # "b" is now the oldest write, so it is the one that goes.
        assert await store.get("ns", "b") is None
        assert await store.get("ns", "a") == "a2"
        assert await store.get("ns", "c") == "c"
