"""Tests for the hook/filter system."""

import pytest
from skrift.lib.hooks import HookRegistry, action, filter, hooks


@pytest.fixture
def registry():
    """Create a fresh HookRegistry for each test."""
    return HookRegistry()


class TestHookRegistry:
    """Test the HookRegistry class."""

    def test_add_action_registers_handler(self, registry):
        """Test that add_action registers a handler."""
        def my_handler():
            pass

        registry.add_action("test_action", my_handler)
        assert registry.has_action("test_action")

    def test_add_filter_registers_handler(self, registry):
        """Test that add_filter registers a handler."""
        def my_filter(value):
            return value

        registry.add_filter("test_filter", my_filter)
        assert registry.has_filter("test_filter")

    def test_action_priority_ordering(self, registry):
        """Test that actions are called in priority order."""
        call_order = []

        def handler_low():
            call_order.append("low")

        def handler_high():
            call_order.append("high")

        registry.add_action("test", handler_high, priority=20)
        registry.add_action("test", handler_low, priority=5)

        import asyncio
        asyncio.run(registry.do_action("test"))

        assert call_order == ["low", "high"]

    def test_filter_priority_ordering(self, registry):
        """Test that filters are applied in priority order."""
        def append_a(value):
            return value + "a"

        def append_b(value):
            return value + "b"

        registry.add_filter("test", append_b, priority=20)
        registry.add_filter("test", append_a, priority=10)

        import asyncio
        result = asyncio.run(registry.apply_filters("test", ""))

        assert result == "ab"

    @pytest.mark.asyncio
    async def test_do_action_calls_all_handlers(self, registry):
        """Test that do_action calls all registered handlers."""
        results = []

        def handler1(value):
            results.append(f"handler1:{value}")

        def handler2(value):
            results.append(f"handler2:{value}")

        registry.add_action("test", handler1)
        registry.add_action("test", handler2)

        await registry.do_action("test", "input")

        assert len(results) == 2
        assert "handler1:input" in results
        assert "handler2:input" in results

    @pytest.mark.asyncio
    async def test_apply_filters_chains_values(self, registry):
        """Test that apply_filters chains filter return values."""
        def double(value):
            return value * 2

        def add_ten(value):
            return value + 10

        registry.add_filter("test", double, priority=10)
        registry.add_filter("test", add_ten, priority=20)

        result = await registry.apply_filters("test", 5)
        assert result == 20  # (5 * 2) + 10

    @pytest.mark.asyncio
    async def test_async_action_handler(self, registry):
        """Test that async action handlers are awaited."""
        results = []

        async def async_handler(value):
            results.append(value)

        registry.add_action("test", async_handler)
        await registry.do_action("test", "async_value")

        assert results == ["async_value"]

    @pytest.mark.asyncio
    async def test_async_filter_handler(self, registry):
        """Test that async filter handlers are awaited."""
        async def async_filter(value):
            return value.upper()

        registry.add_filter("test", async_filter)
        result = await registry.apply_filters("test", "hello")

        assert result == "HELLO"

    @pytest.mark.asyncio
    async def test_mixed_sync_async_handlers(self, registry):
        """Test that sync and async handlers can be mixed."""
        def sync_handler(value):
            return value + "_sync"

        async def async_handler(value):
            return value + "_async"

        registry.add_filter("test", sync_handler, priority=10)
        registry.add_filter("test", async_handler, priority=20)

        result = await registry.apply_filters("test", "start")
        assert result == "start_sync_async"

    def test_remove_action(self, registry):
        """Test removing an action handler."""
        def my_handler():
            pass

        registry.add_action("test", my_handler)
        assert registry.has_action("test")

        removed = registry.remove_action("test", my_handler)
        assert removed is True
        assert not registry.has_action("test")

    def test_remove_filter(self, registry):
        """Test removing a filter handler."""
        def my_filter(value):
            return value

        registry.add_filter("test", my_filter)
        assert registry.has_filter("test")

        removed = registry.remove_filter("test", my_filter)
        assert removed is True
        assert not registry.has_filter("test")

    def test_remove_nonexistent_returns_false(self, registry):
        """Test that removing a nonexistent handler returns False."""
        def my_handler():
            pass

        removed = registry.remove_action("nonexistent", my_handler)
        assert removed is False

    @pytest.mark.asyncio
    async def test_empty_hook_returns_original_value(self, registry):
        """Test that filtering with no handlers returns original value."""
        result = await registry.apply_filters("nonexistent", "original")
        assert result == "original"

    def test_clear_removes_all_hooks(self, registry):
        """Test that clear removes all registered hooks."""
        registry.add_action("action1", lambda: None)
        registry.add_filter("filter1", lambda x: x)

        registry.clear()

        assert not registry.has_action("action1")
        assert not registry.has_filter("filter1")


class TestDecoratorRegistration:
    """Test the @action and @filter decorators."""

    def test_action_decorator_registers_handler(self):
        """Test that @action decorator registers a handler."""
        original_actions = hooks._actions.copy()
        try:
            @action("test_decorator_action")
            def my_handler():
                pass

            assert hooks.has_action("test_decorator_action")
        finally:
            hooks._actions = original_actions

    def test_filter_decorator_registers_handler(self):
        """Test that @filter decorator registers a handler."""
        original_filters = hooks._filters.copy()
        try:
            @filter("test_decorator_filter")
            def my_filter(value):
                return value

            assert hooks.has_filter("test_decorator_filter")
        finally:
            hooks._filters = original_filters

    def test_decorator_preserves_function(self):
        """Test that decorators preserve the original function."""
        original_actions = hooks._actions.copy()
        try:
            @action("test_preserve")
            def my_function():
                return "result"

            assert my_function() == "result"
        finally:
            hooks._actions = original_actions
