# Web Push Notifications

Skrift can send browser push notifications to users who aren't actively connected via SSE. This provides offline notification delivery — users receive notifications even when they've closed the tab.

Web Push requires the `push` extra and must be explicitly enabled in your `app.yaml`.

## How It Works

1. You enable the `PushController` in your `app.yaml`
2. VAPID keys are auto-generated on first use and stored in the database
3. Users subscribe via the browser Push API (your frontend calls `window.__skriftPush.subscribe()`)
4. When a notification is sent and the user has no active SSE connection, Skrift sends a Web Push notification instead

This is automatic when using the `NOTIFICATION_SENT` hook. You can also use the `notify()` function or `send_push()` directly.

## Quick Start

### 1. Install the dependency

```bash
pip install 'skrift[push]'
```

### 2. Enable the PushController

Add to your `app.yaml`:

```yaml
controllers:
  - "skrift.controllers.push:PushController"
```

This registers the `/push/*` endpoints and `/sw.js` service worker route. The push fallback hook is also activated automatically.

### 3. Register the service worker in your template

Add to your base template:

```html
<script src="/static/js/push.js"></script>
<script>
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" });
  }
</script>
```

### 4. Add a subscribe button

```html
<button id="push-subscribe">Enable notifications</button>

<script>
  document.getElementById("push-subscribe").addEventListener("click", async () => {
    const ok = await window.__skriftPush.subscribe();
    if (ok) {
      alert("Push notifications enabled!");
    }
  });
</script>
```

That's it. Skrift handles VAPID key generation, subscription storage, and push delivery automatically.

## Sending Notifications

### Automatic (recommended)

If you're already using `notify_user()` for SSE notifications, push fallback happens automatically via the `NOTIFICATION_SENT` hook. No code changes needed — users without an SSE connection receive a push notification instead.

```python
from skrift.lib.notifications import notify_user

# This automatically triggers a push notification if the user has no SSE connection
await notify_user(str(user.id), "generic", title="New reply", message="Someone replied.")
```

### Controlling Push per Notification

Pass `push_notify` to control whether a specific notification triggers a push:

```python
# Always push (even if user has SSE connected) — for important messages
await notify_user(str(user.id), "message", title="New message", push_notify=True)

# Never push — for transient status updates
await notify_user(str(user.id), "typing", title="User is typing", push_notify=False)

# Auto (default) — push only if no active SSE connection
await notify_user(str(user.id), "generic", title="Update")
```

| Value | Behavior |
|-------|----------|
| `True` | Always send push, even if user has active SSE |
| `False` | Never send push, SSE only |
| `None` (default) | Send push only if user has no active SSE connection |

### Unified `notify()`

For more control over push content separately from SSE:

```python
from skrift.lib.push import notify

await notify(
    db_session,
    user_id=str(user.id),
    event="new_message",
    data={"title": "New message", "body": "You have a reply", "url": "/chat"},
    push_fallback=True,    # send push if no SSE connection (default)
    group="chat",          # SSE group key for replacement
)
```

Override push content independently:

```python
await notify(
    db_session,
    user_id=str(user.id),
    event="comment",
    data={"comment_id": "...", "html": "<p>...</p>"},  # rich SSE payload
    push_title="New comment",                           # simpler push content
    push_body="Someone commented on your post",
    push_url="/posts/123#comments",
)
```

### Direct `send_push()`

For push-only delivery (no SSE):

```python
from skrift.lib.push import send_push

count = await send_push(
    db_session,
    user_id=str(user.id),
    title="Reminder",
    body="Your session expires in 5 minutes",
    url="/dashboard",
    tag="session-expiry",    # replaces previous notification with same tag
)
```

Returns the number of notifications successfully sent. Expired subscriptions (HTTP 404/410) are automatically cleaned up.

## Client-Side API

The `push.js` script exposes `window.__skriftPush`:

```javascript
// Subscribe — requests permission, registers with push service, sends to server
const success = await window.__skriftPush.subscribe();

// Unsubscribe — removes from push service and server
await window.__skriftPush.unsubscribe();

// Check current status
const active = await window.__skriftPush.isSubscribed();
```

### Client-Side Push Filtering

Register a filter callback to suppress or modify notifications based on what the user is currently viewing:

```javascript
window.__skriftPush.onFilter(function(payload) {
  // Suppress notification if user is viewing this chat
  if (payload.tag === "chat:" + currentChatId) {
    return { cancel: true };
  }
  // Or modify the notification
  return payload;
});
```

When a push arrives, the service worker asks focused tabs if the notification should be shown. If the filter returns `{ cancel: true }`, the notification is suppressed. If no filter is registered or no tab responds within 200ms, the notification shows normally.

## Service Worker

The included service worker (`sw.js`) handles push display and notification clicks:

- **Push event**: Shows a browser notification with the payload's `title`, `body`, and optional `tag`
- **Click event**: Opens or focuses a window at the payload's `url` (defaults to `/`)

Push payload format:

```json
{
  "title": "Notification Title",
  "body": "Notification body text",
  "tag": "optional-group-tag",
  "url": "/optional/click/url"
}
```

## VAPID Keys

VAPID (Voluntary Application Server Identification) keys authenticate your server with push services. Skrift generates an ECDSA P-256 keypair automatically on first use and stores it in the `settings` table:

- `webpush:vapid_private` — DER/PKCS8-encoded private key (base64url)
- `webpush:vapid_public` — X962 uncompressed point public key (base64url)

Keys are cached in memory after first load.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/push/vapid-key` | Returns VAPID public key for browser subscription |
| `POST` | `/push/subscribe` | Registers a browser push subscription |
| `POST` | `/push/unsubscribe` | Removes a subscription |
| `GET` | `/sw.js` | Serves the service worker at root scope |

## See Also

- [Notifications](notifications.md) — SSE notification system that Web Push extends
- [Hooks and Filters](hooks-and-filters.md) — `NOTIFICATION_SENT` hook that triggers push fallback
