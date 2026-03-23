---
name: skrift-push
description: "Skrift Web Push notifications — VAPID keys, browser subscriptions, push delivery with SSE fallback, service worker, and client-side filtering."
---

# Skrift Web Push

Browser push notifications with VAPID authentication. Sends push to users without an active SSE connection, providing offline notification delivery.

Requires `pip install 'skrift[push]'` (`pywebpush` dependency) and must be explicitly enabled:

```yaml
controllers:
  - "skrift.controllers.push:PushController"
```

Without `PushController` registered, no push-related routes, hooks, or service worker are loaded.

## VAPID Key Management

Keys are auto-generated on first use and stored in the settings table:

```python
from skrift.lib.push import get_vapid_public_key

# Returns base64url-encoded public key for the browser Push API
public_key = await get_vapid_public_key(db_session)
```

Settings keys: `webpush:vapid_private`, `webpush:vapid_public`. Keys use ECDSA P-256. Both are cached in-memory after first load.

## Sending Push Notifications

### Direct — `send_push()`

Send to all of a user's subscribed browsers:

```python
from skrift.lib.push import send_push

count = await send_push(
    db_session,
    user_id="some-uuid",
    title="New message",
    body="You have a reply",
    url="/chat",           # optional — opened on click
    tag="new_message",     # optional — replaces same-tag notification
)
```

Returns the number sent. Automatically removes expired endpoints (HTTP 404/410).

### Unified — `notify()`

Sends SSE notification; falls back to push when user has no active SSE connection:

```python
from skrift.lib.push import notify

await notify(
    db_session,
    user_id="some-uuid",
    event="new_message",
    data={"title": "New message", "body": "You have a reply", "url": "/chat"},
    push_fallback=True,       # default: True
    group="chat-updates",     # optional SSE group key
)
```

Override push content independently from SSE payload:

```python
await notify(
    db_session,
    user_id=user_id,
    event="comment",
    data={"comment_id": "...", "html": "<p>...</p>"},  # SSE payload
    push_title="New comment",
    push_body="Someone commented on your post",
    push_url="/posts/123#comments",
    push_tag="comment",
)
```

### Per-Notification Control — `push_notify`

Control push behavior per notification via the `push_notify` payload key:

```python
from skrift.lib.notifications import notify_user

# Always push (even if SSE connected) — important alerts
await notify_user(user_id, "message", title="Alert", push_notify=True)

# Never push — transient UI updates
await notify_user(user_id, "typing", title="Typing...", push_notify=False)

# Auto (default) — push only if no SSE connection
await notify_user(user_id, "generic", title="Update")
```

## Automatic Hook Integration

`setup_push_hook()` registers a `NOTIFICATION_SENT` action hook so all user-scoped notifications automatically trigger push fallback:

```python
from skrift.lib.push import setup_push_hook

# Called during app startup in asgi.py
setup_push_hook(db_config.get_session)
```

Fires at priority 50, checks SSE connectivity, sends push only when user has no active connections.

## Subscription Management

```python
from skrift.lib.push import save_subscription, remove_subscription

await save_subscription(db_session, user_id, endpoint, p256dh, auth)
removed = await remove_subscription(db_session, endpoint)  # returns True if found
```

## Controller Endpoints

`PushController` at `/push`:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/push/vapid-key` | Returns `{"publicKey": "..."}` |
| `POST` | `/push/subscribe` | Registers browser subscription (requires auth) |
| `POST` | `/push/unsubscribe` | Removes subscription by endpoint |
| `GET` | `/sw.js` | Serves service worker at root scope |

Subscribe/unsubscribe are CSRF-exempt.

## Client-Side JavaScript

### Service Worker (`sw.js`)

Handles `push` and `notificationclick` events. Push payload: `{"title", "body", "tag", "url"}`. On click: focuses existing window or opens new.

Uses `skipWaiting()` + `clients.claim()` for immediate activation on update.

### Push Client (`push.js`)

```javascript
// Subscribe to push notifications
await window.__skriftPush.subscribe();

// Unsubscribe
await window.__skriftPush.unsubscribe();

// Check subscription status
const subscribed = await window.__skriftPush.isSubscribed();
```

Include in your template:

```html
<script src="{{ static_url('js/push.js') }}"></script>
<script nonce="{{ csp_nonce() }}">
  navigator.serviceWorker.register("/sw.js", { scope: "/" });
</script>
```

### Client-Side Push Filtering

Suppress or modify push notifications based on current view:

```javascript
window.__skriftPush.onFilter(function(payload) {
  if (payload.tag === "chat:" + currentChatId && document.hasFocus()) {
    return { cancel: true };
  }
  return payload;
});
```

The service worker queries focused tabs before showing (200ms timeout).

## Integration in `asgi.py`

- `PushController` is user-enabled via `app.yaml` controllers list
- Service worker route auto-expanded when `PushController` is loaded
- `/push/subscribe` and `/push/unsubscribe` added to CSRF exclude list only when push is enabled
- `setup_push_hook()` called in `on_startup` only when push is enabled and `pywebpush` is importable

## Key Files

| File | Purpose |
|------|---------|
| `skrift/lib/push.py` | VAPID keys, subscription CRUD, `send_push()`, `notify()`, `setup_push_hook()` |
| `skrift/db/models/push_subscription.py` | `PushSubscription` model |
| `skrift/controllers/push.py` | REST endpoints + service worker route |
| `skrift/static/js/push.js` | `SkriftPush` client library |
| `skrift/static/sw.js` | Service worker for push display |
