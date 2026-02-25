# Notifications

Skrift includes a real-time notification system delivered via Server-Sent Events (SSE). Notifications appear as toast popups in the browser and can be scoped to a session, a user, or broadcast to all connections.

## Sending Notifications

Use the convenience functions in `skrift.lib.notifications`:

```python
from skrift.lib.notifications import (
    notify_session, notify_user, notify_broadcast, _ensure_nid
)

# Session-scoped — stored, replayed on reconnect
nid = _ensure_nid(request)
await notify_session(nid, "generic", title="Saved", message="Your draft was saved.")

# User-scoped — stored, delivered to all sessions of a user
await notify_user(str(user.id), "generic", title="New reply", message="Someone replied.")

# Broadcast — ephemeral, not stored, all active connections
await notify_broadcast("new_tweet", tweet_id="...", content_html="...")
```

| Function | Stored? | Target | Use case |
|----------|---------|--------|----------|
| `notify_session(nid, type, **payload)` | Yes | Single session | Transient feedback (saves, errors) |
| `notify_user(user_id, type, **payload)` | Yes | All sessions of user | Cross-device (replies, likes) |
| `notify_broadcast(type, **payload)` | No | All connections | Feed updates (new posts) |

Stored notifications replay on reconnect. Broadcast notifications are ephemeral.

## Notification Modes

Every notification has a **mode** that controls storage, replay, and dismiss behavior. Pass the `mode` keyword to any convenience function:

```python
from skrift.lib.notifications import notify_session, NotificationMode, _ensure_nid

nid = _ensure_nid(request)

# Queued (default) — stored, replayed on reconnect, user dismisses manually
await notify_session(nid, "generic", title="New comment", message="...")

# Timeseries — stored, replayed via ?since=, auto-clears after 8s, not dismissible
await notify_session(
    nid, "generic",
    mode=NotificationMode.TIMESERIES,
    title="CPU: 82%", message="Spike detected",
)

# Ephemeral — not stored, auto-clears after 5s, not dismissible
await notify_session(
    nid, "generic",
    mode=NotificationMode.EPHEMERAL,
    title="Ping", message="pong",
)
```

| Mode | Stored? | Replay | Auto-Clear | Dismissible |
|------|---------|--------|------------|-------------|
| **queued** (default) | Yes | On reconnect | No | Yes |
| **timeseries** | Yes | Via `?since=` timestamp | 8 seconds | No |
| **ephemeral** | No | Never | 5 seconds | No |

### Timeseries Replay

When the client reconnects, it appends `?since=<timestamp>` to the SSE URL using the most recent `created_at` it has seen. The server then replays only timeseries notifications created after that timestamp, in addition to the normal queued-notification flush.

### Attempting to Dismiss Non-Queued Notifications

Sending a `DELETE /notifications/{id}` for a timeseries notification returns **HTTP 409** with `{"error": "notification is not dismissible"}`. This is raised via `NotDismissibleError`.

### TTL and Cleanup

DB-backed backends (Redis, PgNotify) run a background cleanup task every 10 minutes:

- **Queued** notifications: deleted after **24 hours**
- **Timeseries** notifications: deleted after **7 days**

The InMemory backend does not run cleanup (notifications are lost on restart).

## Group Keys

All three functions accept an optional `group` keyword. A new notification with the same group key automatically replaces the previous one:

```python
nid = _ensure_nid(request)

# Progress updates — each replaces the previous toast
await notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 1/3")
await notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 2/3")
await notify_session(nid, "generic", group="deploy", title="Deployed!", message="Done")
```

### Dismissing by Group Key

```python
from skrift.lib.notifications import dismiss_session_group, dismiss_user_group

# Dismiss the active "deploy" notification without knowing its UUID
await dismiss_session_group(nid, "deploy")

# Dismiss from a user's queue (pushes dismissed event to all their sessions)
await dismiss_user_group(str(user.id), "upload-status")
```

## Backend Configuration

The notification service uses a pluggable backend system for storage and cross-replica fanout. Configure in `app.yaml`:

=== "InMemory (default)"

    ```yaml
    # No configuration needed — used automatically
    ```

    Dict-based storage, no cross-replica fanout. Suitable for single-process deployments.

=== "Redis"

    ```yaml
    redis:
      url: $REDIS_URL
      prefix: "myapp"

    notifications:
      backend: "skrift.lib.notification_backends:RedisBackend"
    ```

    Database storage + Redis pub/sub for cross-replica fanout. Requires `pip install 'skrift[redis]'`.

=== "PgNotify"

    ```yaml
    notifications:
      backend: "skrift.lib.notification_backends:PgNotifyBackend"
    ```

    Database storage + PostgreSQL `LISTEN`/`NOTIFY` for cross-replica fanout. Uses your existing database connection — no extra infrastructure.

All DB-backed backends persist notifications in the `stored_notifications` table and share a `_DatabaseStorageMixin` that provides store, remove, group replacement, and background cleanup.

## Client-Side JavaScript

The `notifications.js` script auto-initializes on page load and manages the SSE connection.

### Notification Events

Every incoming notification dispatches a cancelable `sk:notification` CustomEvent:

```javascript
document.addEventListener('sk:notification', (e) => {
    const data = e.detail;  // { type, id, mode, ...payload }
    if (data.type === 'my_custom_type') {
        // Handle custom notification — build your own UI
        e.preventDefault();  // Prevents default generic toast
    }
});
```

Only `"generic"` type notifications render the built-in toast UI. All other types must be handled via event listeners.

### Connection Status Events

```javascript
document.addEventListener('sk:notification-status', (e) => {
    console.log(e.detail.status);
    // "connecting" | "connected" | "disconnected" | "reconnecting" | "suspended"
});
```

### Configuring Mode Defaults

Override auto-clear times and dismiss behavior per mode:

```javascript
window.__skriftNotifications.configure({
    timeseries: { autoClear: 12000 },  // 12s instead of default 8s
    ephemeral:  { autoClear: 3000 },   // 3s instead of default 5s
});
```

#### Persistent Connection

By default the SSE connection disconnects on `window.blur` and reconnects on `window.focus`. To keep the connection alive while the tab is backgrounded (useful for dashboards, chat, etc.):

```javascript
window.__skriftNotifications.configure({
    persistConnection: true,
});
```

Default mode configurations:

| Mode | `dismiss` | `autoClear` |
|------|-----------|-------------|
| queued | `"server"` | `false` |
| timeseries | `false` | `8000` ms |
| ephemeral | `false` | `5000` ms |

### Last Seen Timestamp

The `lastSeen` property exposes the timestamp used for timeseries replay on reconnect. The client updates it automatically as notifications arrive, but you can read or set it manually:

```javascript
// Read the current value (Unix timestamp or null)
const ts = window.__skriftNotifications.lastSeen;

// Set to "now" — skip old timeseries notifications on next reconnect
window.__skriftNotifications.lastSeen = Date.now() / 1000;

// Persist across page loads
localStorage.setItem("lastSeen", window.__skriftNotifications.lastSeen);
// ...on next page load:
const saved = localStorage.getItem("lastSeen");
if (saved) window.__skriftNotifications.lastSeen = parseFloat(saved);
```

### Connection Behavior

- Auto-connects on page load and `window.focus`, disconnects on `window.blur` (opt out with `persistConnection: true`)
- Reconnects after 5 seconds on error
- Deduplicates via internal ID set
- Max visible toasts: 3 (desktop) / 2 (mobile); excess queued
- Global instance: `window.__skriftNotifications`

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `notifications.backend` | `str` | `""` | Backend class path (`module:ClassName`). Empty = InMemory |
| `redis.url` | `str` | `""` | Redis connection URL (RedisBackend only) |
| `redis.prefix` | `str` | `""` | Key prefix for Redis keys |

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/notifications/stream` | SSE stream (auto-connected by client JS) |
| `GET` | `/notifications/stream?since=<ts>` | SSE stream with timeseries replay |
| `DELETE` | `/notifications/{id}` | Dismiss by notification UUID |
| `DELETE` | `/notifications/group/{group}` | Dismiss by group key |

## See Also

- [Hooks and Filters](hooks-and-filters.md) — `NOTIFICATION_SENT` and `NOTIFICATION_DISMISSED` hook constants
- [Custom Controllers](custom-controllers.md) — building controllers that send notifications
