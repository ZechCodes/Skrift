# Hooks and Filters

Skrift provides a WordPress-like hook/filter system for extending functionality without modifying core code.

## Overview

The hook system has two types of extensibility points:

| Type | Purpose | Example |
|------|---------|---------|
| **Actions** | Execute code at specific points (side effects) | Log when a page is saved |
| **Filters** | Modify values as they pass through | Add custom meta tags |

## Quick Start

```python
from skrift.lib.hooks import action, filter, hooks

# Using decorators (auto-registered on import)
@action("after_page_save")
async def notify_on_save(page, is_new):
    print(f"Page saved: {page.title}")

@filter("page_seo_meta")
async def add_author_meta(meta, page, site_name, base_url):
    # Modify and return the meta object
    return meta

# Or register directly
hooks.add_action("before_page_delete", my_callback)
hooks.add_filter("robots_txt", my_modifier)
```

## Actions

Actions let you execute code when something happens. They don't return values.

### Registering an Action

```python
from skrift.lib.hooks import hooks, action

# Via decorator
@action("after_page_save", priority=10)
async def log_page_save(page, is_new):
    action_type = "created" if is_new else "updated"
    print(f"Page {action_type}: {page.title}")

# Via function call
def sync_handler(page, is_new):
    # Sync handlers work too
    pass

hooks.add_action("after_page_save", sync_handler, priority=20)
```

### Action Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `hook_name` | str | Name of the action hook |
| `callback` | callable | Function to execute |
| `priority` | int | Execution order (lower = first, default: 10) |

### Triggering Actions

```python
from skrift.lib.hooks import hooks

# In your code
await hooks.do_action("my_custom_action", arg1, arg2, kwarg=value)
```

## Filters

Filters let you modify values as they pass through. They must return the (modified) value.

### Registering a Filter

```python
from skrift.lib.hooks import hooks, filter

@filter("page_seo_meta", priority=10)
async def customize_seo(meta, page, site_name, base_url):
    # Always return the value (modified or not)
    meta.title = f"{page.title} - Custom Suffix"
    return meta

# Via function call
hooks.add_filter("sitemap_urls", add_custom_urls, priority=5)
```

### Filter Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `hook_name` | str | Name of the filter hook |
| `callback` | callable | Function to modify value |
| `priority` | int | Execution order (lower = first, default: 10) |

### Applying Filters

```python
from skrift.lib.hooks import hooks

# In your code
result = await hooks.apply_filters("my_filter", initial_value, extra_arg)
```

## Built-in Hooks

### Page Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `before_page_save` | Action | `page`, `is_new` | Before page create/update |
| `after_page_save` | Action | `page`, `is_new` | After page saved |
| `before_page_delete` | Action | `page` | Before page deletion |
| `after_page_delete` | Action | `page` | After page deleted |

### SEO Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `page_seo_meta` | Filter | `meta`, `page`, `site_name`, `base_url` | Modify page SEO metadata |
| `page_og_meta` | Filter | `meta`, `page`, `site_name`, `base_url` | Modify OpenGraph metadata |

### Sitemap Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `sitemap_page` | Filter | `entry`, `page` | Modify/exclude sitemap entry (return None to exclude) |
| `sitemap_urls` | Filter | `entries` | Modify full sitemap URL list |
| `robots_txt` | Filter | `content` | Modify robots.txt content |

### Template Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `template_context` | Filter | `context` | Modify template context |

### Theme Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `resolve_theme` | Filter | `theme_name`, `request` | Override the active theme per-request (see [Theming](theming.md)) |

### Notification Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `notification_pre_send` | Filter | `notification`, `scope`, `scope_id` | Before a notification is stored/broadcast — return `None` to suppress |
| `notification_sent` | Action | `notification`, `scope`, `scope_id` | After a notification is sent |
| `notification_dismissed` | Action | `notification_id` | After a notification is dismissed |
| `webhook_notification_received` | Action | `notification`, `target_type`, `scope_id` | After an external webhook notification is received |

### Form Hooks

| Hook | Type | Arguments | Description |
|------|------|-----------|-------------|
| `form_{name}_validated` | Filter | `data` | Modify validated data for a specific form |
| `form_validated` | Filter | `data`, `name` | Modify validated data for any form |

## Priority

Callbacks execute in priority order (lower numbers first):

```python
@action("my_hook", priority=5)   # Runs first
async def early_handler(): pass

@action("my_hook", priority=10)  # Runs second (default)
async def normal_handler(): pass

@action("my_hook", priority=20)  # Runs third
async def late_handler(): pass
```

## Async Support

Both sync and async callbacks are supported:

```python
# Async callback (recommended)
@action("after_page_save")
async def async_handler(page, is_new):
    await some_async_operation()

# Sync callback (also works)
@action("after_page_save")
def sync_handler(page, is_new):
    some_sync_operation()
```

## Removing Hooks

```python
from skrift.lib.hooks import hooks

# Remove specific callback
hooks.remove_action("my_hook", my_callback)
hooks.remove_filter("my_filter", my_callback)

# Clear all hooks (useful for testing)
hooks.clear()
```

## Examples

### Add Custom Sitemap URLs

```python
from skrift.lib.hooks import filter
from skrift.controllers.sitemap import SitemapEntry

@filter("sitemap_urls")
def add_api_docs(entries):
    entries.append(SitemapEntry(
        loc="https://mysite.com/api/docs",
        changefreq="monthly",
        priority=0.5,
    ))
    return entries
```

### Exclude Pages from Sitemap

```python
from skrift.lib.hooks import filter

@filter("sitemap_page")
def exclude_private_pages(entry, page):
    if page.slug.startswith("internal/"):
        return None  # Exclude from sitemap
    return entry
```

### Customize robots.txt

```python
from skrift.lib.hooks import filter

@filter("robots_txt")
def add_crawl_delay(content):
    return content + "\nCrawl-delay: 10"
```

### Log All Page Changes

```python
from skrift.lib.hooks import action
import logging

logger = logging.getLogger(__name__)

@action("after_page_save")
async def audit_log(page, is_new):
    action = "created" if is_new else "updated"
    logger.info(f"Page {action}: {page.slug} (ID: {page.id})")

@action("after_page_delete")
async def audit_delete(page):
    logger.info(f"Page deleted: {page.slug} (ID: {page.id})")
```

## Hook Constants

Import hook names as constants for better IDE support:

```python
from skrift.lib.hooks import (
    BEFORE_PAGE_SAVE,
    AFTER_PAGE_SAVE,
    BEFORE_PAGE_DELETE,
    AFTER_PAGE_DELETE,
    PAGE_SEO_META,
    PAGE_OG_META,
    SITEMAP_URLS,
    SITEMAP_PAGE,
    ROBOTS_TXT,
    TEMPLATE_CONTEXT,
    RESOLVE_THEME,
    FORM_VALIDATED,
    NOTIFICATION_SENT,
    NOTIFICATION_DISMISSED,
    NOTIFICATION_PRE_SEND,
    WEBHOOK_NOTIFICATION_RECEIVED,
)
```

## Best Practices

1. **Use async when possible** - For database or I/O operations
2. **Always return in filters** - Even if you don't modify the value
3. **Use meaningful priorities** - Reserve low numbers for critical hooks
4. **Keep hooks focused** - One hook, one responsibility
5. **Handle errors gracefully** - Don't let hook errors break the request
