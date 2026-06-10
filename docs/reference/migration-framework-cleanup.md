# Migration: Framework Cleanup (June 2026)

This release restructures the public Python API. **There are no deprecation aliases** — old import paths raise `ModuleNotFoundError`/`ImportError`. Everything you need to change is listed below; a mechanical update script is at the [bottom](#mechanical-update).

## Module Moves

Public modules moved out of the internal `skrift.lib` package to top-level paths. `skrift.lib` is now internal-only — downstream code should never import from it.

| Before | After |
|--------|-------|
| `skrift.lib.hooks` | `skrift.hooks` |
| `skrift.lib.flash` | `skrift.flash` |
| `skrift.lib.template` | `skrift.template` |
| `skrift.lib.notifications` | `skrift.notifications` |
| `skrift.lib.push` | `skrift.push` |
| `skrift.lib.markdown` | `skrift.markdown` |
| `skrift.lib.seo` | `skrift.seo` |
| `skrift.lib.storage` | `skrift.storage` |

The `skrift.lib` package itself no longer re-exports anything:

| Before | After |
|--------|-------|
| `from skrift.lib import Template` | `from skrift.template import Template` (or `from skrift import Template`) |
| `from skrift.lib import render_markdown` | `from skrift.markdown import render_markdown` (or `from skrift import render_markdown`) |
| `from skrift.lib import hooks, action, filter, add_action, add_filter, do_action, apply_filters` | `from skrift.hooks import ...` (most also via `from skrift import ...`) |
| `from skrift.lib import notifications` | `from skrift import notifications` |

Modules that **stay** in `skrift.lib` (internal — config strings referencing them, e.g. `notifications.backend: "skrift.lib.notification_backends:RedisBackend"`, are unchanged): `client_ip`, `exceptions`, `observability`, `notification_backends`, `email`, `email_backends`, `imaging`, `redirects`, `theme`, `trusted_proxy`, `sliding_window`, `sliding_window_redis`.

## Renames

| Before | After | Notes |
|--------|-------|-------|
| `skrift.lib.notifications._ensure_nid` | `skrift.notifications.ensure_nid` | Now public; also `from skrift import ensure_nid` |

## New Top-Level Exports (additive)

The everyday toolkit is now importable from `skrift` directly — prefer these in new code:

```python
from skrift import (
    FormModel, Form, form,                          # forms
    auth_guard, Permission, Role,                   # guards
    action, add_action, add_filter, do_action, apply_filters, hooks,
    flash_success, flash_error, flash_warning, flash_info, get_flash_messages,
    NotificationMode, notify_user, notify_session, notify_broadcast, ensure_nid,
    get_settings, register_config_section,          # config
    Template, render_markdown,                      # rendering
)
```

## Config System

| Change | Impact |
|--------|--------|
| `register_config_section(name, Model)` added | Extensions add typed app.yaml sections without editing core. Register at import time, before app creation. See [Configuration](../core-concepts/configuration.md#custom-config-sections). |
| `api_keys:` app.yaml section is now parsed | **Behavior change.** Previously documented but silently ignored — if your app.yaml contains an `api_keys:` section, its values now take effect. |
| `set_config_path()` clears the settings cache | **Behavior change.** Previously a silent no-op if `get_settings()` had already been called; the new config file is now actually read. Remove any manual `clear_settings_cache()` calls paired with it. |
| Reserved top-level keys | `storage`, `page_types`, `sites`, `controllers`, `models`, `middleware`, `environment`, `debug`, `secret_key`, `theme`, `domain`, `security_contact`, `oauth2_enabled` cannot be used as registered section names. |

## Admin Extension API

| Before | After |
|--------|-------|
| `AdminController()._get_admin_context(request, db_session)` (documented pattern; the method no longer existed) | `from skrift.admin import get_admin_context` → `await get_admin_context(request, db_session)` |
| `tags=[ADMIN_NAV_TAG], opt={"label": ..., "icon": ..., "order": ...}` | `**admin_nav("Label", icon="...", order=...)` (long form still works) |
| `from skrift.admin.navigation import ADMIN_NAV_TAG` | `from skrift.admin import ADMIN_NAV_TAG` (old path still works) |
| Editing `skrift/auth/roles.py` to add roles (documented pattern) | `register_permission(...)` / `register_role(...)` from `skrift.auth.permissions` / `skrift.auth.roles` |

## Page Types

| Change | Impact |
|--------|--------|
| `PageTypeConfig.public_routes` added (default `true`) | Set `public_routes: false` on a `page_types` entry to hand-write the public `/{name}/` routes in your own controller while keeping generated admin CRUD and permissions. Previously a declared type's generated routes collided with hand-written ones at startup with no opt-out. |

## Database / Models

| Change | Impact |
|--------|--------|
| `APIKey.user_id` annotation: `Mapped[str]` → `Mapped[UUID]` | No schema change — the GUID column already returned `UUID` at runtime; the annotation lied. Code that assumed `str` (e.g. `UUID(api_key.user_id)` wrappers or string comparisons) should treat it as `UUID`. |
| Republish service functions take `user_id: UUID` | `upsert_republished_page`, `list_outbound_links`, `save_outbound_link`, `list_inbound_publishers` previously annotated `str`. |
| `RepublishSiteLink.last_published_at` removed | Column had no writer. Removed from the model **and edited out of the unreleased `20260522_add_republish_tables` migration**. If you already ran that migration in a dev database, downgrade past it and re-upgrade (or drop the column manually). |

## API Behavior Fixes

| Change | Impact |
|--------|--------|
| `DELETE /api/republish/posts` declares `status_code=200` | Previously raised `ImproperlyConfiguredException` at startup whenever republish was enabled (Litestar's `@delete` defaults to 204-no-body). No client-side change — the endpoint simply works now. |

## Forms

| Change | Impact |
|--------|--------|
| `list_forms()` added | `from skrift.forms import list_forms` — all registered forms by name. Additive. |

## Mechanical Update

For a downstream codebase, this updates all moved import paths and the rename in one pass (review the diff afterwards):

```bash
find . -type f \( -name "*.py" -o -name "*.md" \) -not -path "*/.venv/*" -print0 | \
  xargs -0 perl -pi -e '
    s/\bskrift\.lib\.(hooks|flash|template|notifications|push|markdown|seo|storage)\b/skrift.$1/g;
    s/\bfrom skrift\.lib import notifications\b/from skrift import notifications/g;
    s/\b_ensure_nid\b/ensure_nid/g;
  '
```

Then grep for anything left: `grep -rn "skrift\.lib\." --include="*.py" .` — remaining hits should only be the internal modules listed above (and those only in config strings, not imports).
