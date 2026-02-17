"""WordPress-like hook/filter system for Skrift CMS extensibility.

This module provides an async-first hook system that allows registering
callbacks to be executed at specific points in the application lifecycle.

Actions: Execute callbacks without modifying a value (side effects)
Filters: Execute callbacks that can modify a value (transformations)

Usage:
    from skrift.lib.hooks import hooks, action, filter

    # Using decorators (auto-registered on import)
    @action("after_page_save", priority=10)
    async def notify_on_page_save(page):
        print(f"Page saved: {page.title}")

    @filter("page_seo_meta", priority=10)
    async def add_custom_meta(meta, page):
        meta["author"] = "Custom Author"
        return meta

    # Using direct registration
    hooks.add_action("before_page_save", my_callback)
    hooks.add_filter("page_og_meta", my_modifier)

    # Triggering hooks
    await hooks.do_action("after_page_save", page)
    meta = await hooks.apply_filters("page_seo_meta", {}, page)
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, TypeVar

T = TypeVar("T")


@dataclass(order=True)
class HookHandler:
    """A registered hook handler with priority."""

    priority: int
    callback: Callable = field(compare=False)

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        """Call the handler, handling both sync and async callbacks."""
        result = self.callback(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result


class HookRegistry:
    """Central registry for all hooks (actions and filters)."""

    def __init__(self) -> None:
        self._actions: dict[str, list[HookHandler]] = defaultdict(list)
        self._filters: dict[str, list[HookHandler]] = defaultdict(list)

    def add_action(
        self,
        hook_name: str,
        callback: Callable[..., Any],
        priority: int = 10,
    ) -> None:
        """Register an action callback.

        Args:
            hook_name: Name of the action hook
            callback: Function to call when action is triggered
            priority: Lower numbers execute first (default: 10)
        """
        handler = HookHandler(priority=priority, callback=callback)
        self._actions[hook_name].append(handler)
        self._actions[hook_name].sort()

    def add_filter(
        self,
        hook_name: str,
        callback: Callable[..., T],
        priority: int = 10,
    ) -> None:
        """Register a filter callback.

        Args:
            hook_name: Name of the filter hook
            callback: Function to call to modify value
            priority: Lower numbers execute first (default: 10)
        """
        handler = HookHandler(priority=priority, callback=callback)
        self._filters[hook_name].append(handler)
        self._filters[hook_name].sort()

    def remove_action(
        self,
        hook_name: str,
        callback: Callable[..., Any],
    ) -> bool:
        """Remove an action callback.

        Args:
            hook_name: Name of the action hook
            callback: Function to remove

        Returns:
            True if callback was found and removed
        """
        handlers = self._actions.get(hook_name, [])
        for i, handler in enumerate(handlers):
            if handler.callback is callback:
                handlers.pop(i)
                return True
        return False

    def remove_filter(
        self,
        hook_name: str,
        callback: Callable[..., Any],
    ) -> bool:
        """Remove a filter callback.

        Args:
            hook_name: Name of the filter hook
            callback: Function to remove

        Returns:
            True if callback was found and removed
        """
        handlers = self._filters.get(hook_name, [])
        for i, handler in enumerate(handlers):
            if handler.callback is callback:
                handlers.pop(i)
                return True
        return False

    def has_action(self, hook_name: str) -> bool:
        """Check if any actions are registered for a hook."""
        return bool(self._actions.get(hook_name))

    def has_filter(self, hook_name: str) -> bool:
        """Check if any filters are registered for a hook."""
        return bool(self._filters.get(hook_name))

    async def do_action(
        self,
        hook_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Execute all registered action callbacks.

        Args:
            hook_name: Name of the action hook
            *args: Positional arguments to pass to callbacks
            **kwargs: Keyword arguments to pass to callbacks
        """
        from skrift.lib.observability import span

        with span(f"hook.action:{hook_name}", hook_name=hook_name):
            handlers = self._actions.get(hook_name, [])
            for handler in handlers:
                await handler.call(*args, **kwargs)

    async def apply_filters(
        self,
        hook_name: str,
        value: T,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Apply all registered filter callbacks to a value.

        Args:
            hook_name: Name of the filter hook
            value: Initial value to filter
            *args: Additional positional arguments to pass to callbacks
            **kwargs: Keyword arguments to pass to callbacks

        Returns:
            The filtered value after all callbacks have been applied
        """
        from skrift.lib.observability import span

        with span(f"hook.filter:{hook_name}", hook_name=hook_name):
            handlers = self._filters.get(hook_name, [])
            for handler in handlers:
                value = await handler.call(value, *args, **kwargs)
            return value

    def clear(self) -> None:
        """Clear all registered hooks. Useful for testing."""
        self._actions.clear()
        self._filters.clear()


# Global singleton registry
hooks = HookRegistry()


# Convenience functions that use the global registry
def add_action(
    hook_name: str,
    callback: Callable[..., Any],
    priority: int = 10,
) -> None:
    """Register an action callback to the global registry."""
    hooks.add_action(hook_name, callback, priority)


def add_filter(
    hook_name: str,
    callback: Callable[..., T],
    priority: int = 10,
) -> None:
    """Register a filter callback to the global registry."""
    hooks.add_filter(hook_name, callback, priority)


def remove_action(hook_name: str, callback: Callable[..., Any]) -> bool:
    """Remove an action callback from the global registry."""
    return hooks.remove_action(hook_name, callback)


def remove_filter(hook_name: str, callback: Callable[..., Any]) -> bool:
    """Remove a filter callback from the global registry."""
    return hooks.remove_filter(hook_name, callback)


async def do_action(hook_name: str, *args: Any, **kwargs: Any) -> None:
    """Execute all registered action callbacks via the global registry."""
    await hooks.do_action(hook_name, *args, **kwargs)


async def apply_filters(hook_name: str, value: T, *args: Any, **kwargs: Any) -> T:
    """Apply all registered filter callbacks via the global registry."""
    return await hooks.apply_filters(hook_name, value, *args, **kwargs)


# Decorator factories for auto-registration
def action(hook_name: str, priority: int = 10) -> Callable[[Callable], Callable]:
    """Decorator to register a function as an action handler.

    Usage:
        @action("after_page_save", priority=5)
        async def my_handler(page):
            ...
    """

    def decorator(func: Callable) -> Callable:
        hooks.add_action(hook_name, func, priority)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper

    return decorator


def filter(hook_name: str, priority: int = 10) -> Callable[[Callable], Callable]:
    """Decorator to register a function as a filter handler.

    Usage:
        @filter("page_seo_meta", priority=5)
        async def my_filter(meta, page):
            meta["custom"] = "value"
            return meta
    """

    def decorator(func: Callable) -> Callable:
        hooks.add_filter(hook_name, func, priority)

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper

    return decorator


# Define standard hook names as constants for discoverability
# Actions
BEFORE_PAGE_SAVE = "before_page_save"
AFTER_PAGE_SAVE = "after_page_save"
BEFORE_PAGE_DELETE = "before_page_delete"
AFTER_PAGE_DELETE = "after_page_delete"

# Form hooks
FORM_VALIDATED = "form_validated"

# Filters
PAGE_SEO_META = "page_seo_meta"
PAGE_OG_META = "page_og_meta"
SITEMAP_URLS = "sitemap_urls"
SITEMAP_PAGE = "sitemap_page"
ROBOTS_TXT = "robots_txt"
TEMPLATE_CONTEXT = "template_context"

# Observability hooks
LOGFIRE_CONFIGURED = "logfire_configured"

# Notification hooks
NOTIFICATION_SENT = "notification_sent"
NOTIFICATION_DISMISSED = "notification_dismissed"
