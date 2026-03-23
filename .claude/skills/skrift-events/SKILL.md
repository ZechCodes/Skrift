---
name: skrift-events
description: "Skrift event system — hooks/filters extensibility, real-time SSE notifications, pluggable backends, and dismiss patterns."
---

# Skrift Events

Hooks/filters for extensibility and real-time SSE notifications for user feedback.

## Hooks & Filters

WordPress-inspired event system. **Actions** trigger side effects. **Filters** transform values through a chain of handlers.

### Registration — Decorators

```python
from skrift.lib.hooks import hooks, action, filter

@action("after_page_save", priority=10)
async def invalidate_cache(page, is_new: bool):
    cache.delete(f"page:{page.slug}")

@filter("page_seo_meta", priority=10)
async def add_default_author(meta: dict, page) -> dict:
    if "author" not in meta:
        meta["author"] = "Site Author"
    return meta
```

### Registration — Direct (runtime)

```python
hooks.add_action("hook_name", callback, priority=10)
hooks.add_filter("hook_name", callback, priority=10)
```

### Triggering

```python
# Actions (fire and forget)
await hooks.do_action("hook_name", arg1, arg2)

# Filters (chain transforms — each handler receives the previous return value)
result = await hooks.apply_filters("hook_name", initial_value, arg1)
```

Priority: lower numbers execute first. Default is 10.

### Built-in Hook Points

**Actions:**

| Hook | Arguments | Fired when |
|------|-----------|------------|
| `before_page_save` | `(page, is_new)` | Before saving a page |
| `after_page_save` | `(page, is_new)` | After saving a page |
| `before_page_delete` | `(page,)` | Before deleting a page |
| `after_page_delete` | `(page,)` | After deleting a page |
| `NOTIFICATION_SENT` | `(notification,)` | After a notification is sent |
| `NOTIFICATION_DISMISSED` | `(notification_id,)` | After a notification is dismissed |
| `LOGFIRE_CONFIGURED` | `()` | After Logfire instrumentation is complete |

**Filters:**

| Hook | Signature | Purpose |
|------|-----------|---------|
| `page_seo_meta` | `(meta, page) → meta` | Modify SEO metadata dict |
| `page_og_meta` | `(meta, page) → meta` | Modify OpenGraph metadata dict |
| `sitemap_urls` | `(urls,) → urls` | Modify sitemap URL list |
| `sitemap_page` | `(page_data, page) → page_data` | Modify single sitemap entry |
| `robots_txt` | `(content,) → content` | Modify robots.txt content |
| `template_context` | `(context,) → context` | Modify template context dict |
| `resolve_theme` | `(theme_name, request) → theme_name` | Override active theme per-request |
| `form_{name}_validated` | `(data,) → data` | Modify form data after validation |
| `form_validated` | `(data, name) → data` | Modify form data after validation (global) |

### Custom Hook Points

```python
MY_DATA_FILTER = "my_data_filter"
MY_AFTER_SAVE = "my_after_save"

async def save_thing(db_session, data: dict) -> Thing:
    data = await hooks.apply_filters(MY_DATA_FILTER, data)
    thing = Thing(**data)
    db_session.add(thing)
    await db_session.commit()
    await hooks.do_action(MY_AFTER_SAVE, thing)
    return thing
```

### Testing Hooks

```python
async def test_hook_called(db_session):
    called_with = []

    async def track_save(page, is_new):
        called_with.append((page.title, is_new))

    hooks.add_action("after_page_save", track_save)
    try:
        await page_service.create(db_session, slug="test", title="Test")
        assert len(called_with) == 1
        assert called_with[0] == ("Test", True)
    finally:
        hooks.remove_action("after_page_save", track_save)
```

---

## Notifications — Server API

Real-time notifications delivered via Server-Sent Events (SSE).

### Delivery Scopes

```python
from skrift.lib.notifications import notify_session, notify_user, notify_broadcast, _ensure_nid

nid = _ensure_nid(request)
await notify_session(nid, "generic", title="Saved", message="Your draft was saved.")
await notify_user(str(user.id), "generic", title="New reply", message="Someone replied.")
await notify_broadcast("new_tweet", tweet_id="...", content_html="...")
```

| Function | Stored? | Target | Use case |
|----------|---------|--------|----------|
| `notify_session(nid, type, *, group, mode, **payload)` | Yes | Single session | Transient feedback |
| `notify_user(user_id, type, *, group, mode, **payload)` | Yes | All user sessions | Cross-device |
| `notify_broadcast(type, *, group, **payload)` | No | All connections | Feed updates |

### Group Key — Replace-in-Place

A new notification with the same group key automatically dismisses the previous one:

```python
nid = _ensure_nid(request)
await notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 1/3")
await notify_session(nid, "generic", group="deploy", title="Deployed!", message="Done")
```

### Dismissing by Group Key

```python
from skrift.lib.notifications import dismiss_session_group, dismiss_user_group

await dismiss_session_group(nid, "deploy")
await dismiss_user_group(str(user.id), "upload-status")
```

### Notification Modes

| Mode | Stored? | Replay | Auto-Clear | Dismissible |
|------|---------|--------|------------|-------------|
| `QUEUED` (default) | Yes | On reconnect | No | Yes |
| `TIMESERIES` | Yes | Via `?since=` | 8s client-side | No |
| `EPHEMERAL` | No | Never | 5s client-side | No |

```python
from skrift.lib.notifications import NotificationMode

await notify_session(nid, "generic", mode=NotificationMode.EPHEMERAL, title="Ping")
```

### Controller Pattern — Notify on Action

```python
from skrift.lib.notifications import notify_user

@post("/{item_id:uuid}/comment", guards=[auth_guard])
async def comment(self, request, db_session, item_id):
    comment = await comment_service.create(db_session, user.id, item_id, data.content)
    item = await item_service.get_by_id(db_session, item_id)
    if item and str(item.user_id) != str(user.id):
        await notify_user(str(item.user_id), "generic",
            title=f"{user.name} commented", message=data.content[:100])
    return Redirect(f"/items/{item_id}")
```

### Notification Types

- `"generic"` — rendered as toast with `title` + `message` (built-in UI)
- `"dismissed"` — internal, triggers client-side removal
- Custom types — dispatched via `sk:notification` CustomEvent for app-specific handling

---

## SSE Protocol (Three-Phase)

1. **Flush**: Server sends all queued notifications for the session/user
2. **Sync**: Server sends `event: sync` — client reconciles (removes dismissed-elsewhere items)
3. **Live**: Server pushes new notifications as they arrive; 30s keepalive comments prevent proxy timeouts

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/notifications/stream` | SSE stream (auto-connected by `notifications.js`) |
| `DELETE` | `/notifications/{id}` | Dismiss by notification UUID |
| `DELETE` | `/notifications/group/{group}` | Dismiss by group key |

---

## Backend Configuration

| Backend | Storage | Cross-Replica | Config |
|---------|---------|---------------|--------|
| InMemory (default) | Python dicts | None | `backend: ""` |
| Redis | Database + Redis pub/sub | Yes | `backend: "skrift.lib.notification_backends:RedisBackend"` |
| PgNotify | Database + LISTEN/NOTIFY | Yes | `backend: "skrift.lib.notification_backends:PgNotifyBackend"` |

```yaml
# Redis backend
redis:
  url: $REDIS_URL
  prefix: "myapp"
notifications:
  backend: "skrift.lib.notification_backends:RedisBackend"

# PgNotify backend (uses existing db.url)
notifications:
  backend: "skrift.lib.notification_backends:PgNotifyBackend"
```

Database-backed backends auto-clean: queued notifications after 24h, timeseries after 7 days.

---

## Client-Side SSE JS

The `notifications.js` script provides `window.__skriftNotifications`:

### Configuration

```javascript
window.__skriftNotifications.configure({
    persistConnection: true,  // keep SSE alive when tab loses focus
    statusIndicator: {
        enabled: true,
        element: "#my-status",       // CSS selector or HTMLElement
        labels: { connected: "Live", disconnected: "Offline" },
    },
});
```

When `persistConnection` is enabled, health-checks on page-visible: force-reconnects if hidden > 30s.

### Custom Event Handling

```javascript
document.addEventListener('sk:notification', (e) => {
    const data = e.detail;  // { type, id, ...payload }
    if (data.type === 'my_custom_type') {
        e.preventDefault();  // prevents default generic toast
    }
});
```

### Connection Status Events

```javascript
document.addEventListener('sk:notification-status', (e) => {
    console.log(e.detail.status);
    // "connecting" | "connected" | "disconnected" | "reconnecting" | "suspended"
});
```

### Client Mode Defaults

```javascript
const _modeDefaults = {
    queued:     { dismiss: "server", autoClear: false },
    timeseries: { dismiss: false,    autoClear: 8000 },
    ephemeral:  { dismiss: false,    autoClear: 5000 },
};
```

Override via `configure({ timeseries: { autoClear: 12000 } })`.

### Dismiss Flow

1. User clicks dismiss → client adds `.sk-notification-exit` animation
2. Client sends `DELETE /notifications/{id}`
3. Server broadcasts `"dismissed"` event to user's other sessions
4. Other sessions remove the toast

---

## Key Files

| File | Purpose |
|------|---------|
| `skrift/lib/hooks.py` | Hook registry, `@action`/`@filter` decorators, `hooks` singleton |
| `skrift/lib/notifications.py` | `NotificationService` singleton, convenience functions |
| `skrift/lib/notification_backends.py` | `InMemoryBackend`, `RedisBackend`, `PgNotifyBackend` |
| `skrift/db/models/notification.py` | `StoredNotification` model |
| `skrift/controllers/notifications.py` | SSE stream + dismiss HTTP endpoints |
| `skrift/static/js/notifications.js` | `SkriftNotifications` client class |
| `skrift/static/css/skrift.css` | `.sk-notification*` toast styles |
| `skrift/config.py` | `RedisConfig`, `NotificationsConfig` |
