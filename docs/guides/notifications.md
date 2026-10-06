# Notifications

Skrift includes a real-time notification system delivered via Server-Sent Events (SSE). Notifications appear as toast popups in the browser and can be scoped to a session, a user, or broadcast to all connections.

## Sending Notifications

Use the convenience functions in `skrift.notifications`:

```python
from skrift.notifications import (
    notify_session, notify_user, notify_broadcast, ensure_nid
)

# Session-scoped — stored, replayed on reconnect
nid = ensure_nid(request)
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
from skrift.notifications import notify_session, NotificationMode, ensure_nid

nid = ensure_nid(request)

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

### Lifetime and Cleanup

Stored notifications have a lifetime, set under `notifications:` in `app.yaml`:

```yaml
notifications:
  queued_ttl_seconds: 21600       # 6 hours (default 86400, 24 hours)
  timeseries_ttl_seconds: 604800  # 7 days (the default)
```

Both must be positive, finite and at most 3,153,600,000 seconds (100 years); values under a second are fine. Every built-in backend (InMemory, Redis, PgNotify) honours them:

- **Replay is bounded.** A notification older than its lifetime is never returned by the queued replay on connect/reconnect or by the timeseries `?since=` replay, whenever the cleanup sweep last ran. The lifetime only filters replay: a notification is pushed live to connected clients (and to Web Push) as it is sent, without an age check.
- **Storage is swept.** Each running backend sweeps every `interval = min(600, max(1, min(queued_ttl_seconds, timeseries_ttl_seconds)))` seconds. While a backend is running and its sweeps succeed, a notification is deleted at an age of at most its mode's lifetime + `interval` + the time the sweep and event-loop scheduling take. A failed sweep is logged and retried at the next interval, so each failure postpones deletion by one more interval. Rows of DB-backed backends are not swept while no process runs the backend; the first sweep runs one interval after a backend starts. The InMemory backend loses everything on restart anyway.

The `QUEUED_TTL_HOURS` and `TIMESERIES_TTL_DAYS` constants in `skrift.lib.notification_backends` remain importable as the defaults.

!!! note "Custom backends"
    A custom backend owns its own retention. It receives `settings=` at construction and can read `settings.notifications.queued_ttl_seconds`, but the service does not filter what a custom backend returns.

### Clearing Queued Notifications

When the work a notification describes is finished, delete its queued notifications instead of waiting for the lifetime:

```python
from skrift.notifications import (
    clear_session_notifications,
    clear_source_notifications,
    clear_user_notifications,
)

removed = await clear_user_notifications(str(user.id))                     # everything queued for the user
removed = await clear_user_notifications(str(user.id), group="answer-42")  # just one group
removed = await clear_session_notifications(nid)
removed = await clear_source_notifications("blog:tech", group="draft")
```

Each returns how many notifications were removed. All three call `notifications.clear_queued(source_key, *, group=None)`.

- Only **queued** notifications stored on that exact source key are removed. Timeseries notifications are left alone, and so are other source keys: `clear_user_notifications` does not touch a notification sent to one of the user's sessions.
- Removal applies to **every subscriber**, like group replacement, and unlike `dismiss`, which hides a notification from one subscriber only. `NOTIFICATION_DISMISSED` is not fired.
- Connected clients on every replica receive a `dismissed` event for each removed notification. Redis and PgNotify carry it through the normal send fanout. The event is not stored, because a client that reconnects later drops any notification missing from the queued replay.
- A send's fanout can reach a replica after the notification was cleared there (the sending replica's publish was slow). Every process remembers ids removed for everyone (cleared, or replaced by a newer notification in the same group) for 10 minutes, up to 10,000 ids, both those it removed and those it heard about through a `dismissed` event, and drops a later delivery of one. The browser client likewise ignores a notification whose `dismissed` event it already received (last 1,000 ids). Not covered: a replica that never received the removal event, and a delivery delayed past that memory. A reconnect still corrects these, since the replay comes from storage.
- Custom backends may implement `clear_queued(source_key, group=None) -> list[UUID]`. Backends without it still work: the service lists the source's queued notifications with `get_queued_multi` and removes each one with `remove`.

## Group Keys

All three functions accept an optional `group` keyword. A new notification with the same group key automatically replaces the previous one:

```python
nid = ensure_nid(request)

# Progress updates — each replaces the previous toast
await notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 1/3")
await notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 2/3")
await notify_session(nid, "generic", group="deploy", title="Deployed!", message="Done")
```

### Dismissing by Group Key

```python
from skrift.notifications import dismiss_session_group, dismiss_user_group

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

### Backend Lifecycle Outside the ASGI App

The web app starts and stops the configured backend as part of its ASGI lifecycle, and `skrift workers run` does the same for standalone worker processes — notifications published from job handlers reach web replicas out of the box.

Processes with nonstandard lifecycles (scripts, custom entry points) can manage the backend through the public API on the `notifications` service singleton:

```python
from skrift.notifications import notifications

# Idempotently load and start the backend configured in app.yaml.
# Returns True when a configured backend is running, False when
# notifications.backend is not set (in-process fallback stays in effect).
await notifications.ensure_backend_started(settings=settings, session_maker=session_maker)

# True once a backend was explicitly started and not yet stopped;
# False while relying on the lazy in-process InMemoryBackend fallback.
notifications.backend_started

# Stop the backend on shutdown.
await notifications.stop_backend()
```

Without a started backend, `notify_user()` and friends fall back to a process-local `InMemoryBackend` — notifications are delivered within the process but never reach other replicas.

## Client-Side JavaScript

The `notifications.js` script auto-initializes on page load and manages the SSE connection.

### Notification Events

Every incoming notification dispatches a cancelable `sk:notification` CustomEvent:

```javascript
document.addEventListener('sk:notification', (e) => {
    const data = e.detail;  // { type, id, mode, created_at, group, payload }
    if (data.type === 'my_custom_type') {
        // Handle custom notification — build your own UI
        console.log(data.payload.title);
        e.preventDefault();  // Prevents default generic toast
    }
});
```

Only `"generic"` type notifications render the built-in toast UI. All other types must be handled via event listeners.

!!! warning "Payload keys moved under `payload`"

    Payload entries used to sit directly on the notification next to the
    envelope fields, so a payload key named `type`, `id`, `mode`, `created_at`,
    or `group` silently overwrote the envelope value of the same name. Nesting
    the payload makes that collision impossible.

    Reading a payload key off the envelope — `notification.title` instead of
    `notification.payload.title` — still returns the value and logs a
    `console.error` naming the key and its new path. The fallback will be
    removed in a later release. Keys absent from the payload return `undefined`
    as before, so `if (notification.someOptionalKey)` is unaffected.

    The five envelope names are reserved: `notification.group` always means the
    envelope's group, never a payload entry of the same name. Reach those
    through `notification.payload.group`.

### Reactive Watchers

For DOM-local notification handling, add `skrift:watch-for` and `skrift:render` to an element:

```html
<div
    id="notification-panel"
    skrift:watch-for="^notification(?:[:.].+)?$"
    skrift:render="App.Notifications.render">
</div>
```

`skrift:watch-for` is a JavaScript regular expression matched against `notification.type`.
`skrift:render` is a global function path; inline expressions are not evaluated.

```javascript
window.App = {
    Notifications: {
        render(element, notification, event) {
            event.preventDefault();  // Optional: suppresses the generic toast
            element.textContent = notification.payload.message || "";
        },
    },
};
```

The render function receives `(element, notification, event)` and owns all DOM updates. Return values are ignored.

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

When `persistConnection` is enabled, the client automatically performs a health check when the page becomes visible again. On mobile devices, the OS may silently kill background connections even though the browser still reports them as open. If the page was hidden for more than 30 seconds, the client force-reconnects to guarantee liveness. For shorter background periods, it trusts the browser's connection state.

The client listens for both `visibilitychange` (reliable on mobile) and `focus`/`blur` (catches desktop window switches) to detect page visibility.

#### Status Indicator

The built-in status indicator shows connection state as a colored dot with a label. You can customize it or disable it entirely:

```javascript
window.__skriftNotifications.configure({
    statusIndicator: {
        enabled: false,  // Disable the indicator entirely (default: true)
    },
});
```

Point the indicator at your own DOM element instead of the auto-created one:

```javascript
window.__skriftNotifications.configure({
    statusIndicator: {
        element: "#my-status",  // CSS selector or HTMLElement
    },
});
```

The element will have `.sk-status-dot` and `.sk-status-label` spans injected if they don't already exist.

Customize the text shown for each connection state:

```javascript
window.__skriftNotifications.configure({
    statusIndicator: {
        labels: {
            connected: "Live",
            suspended: "Paused",
            connecting: "Connecting…",
            disconnected: "Offline",
        },
    },
});
```

Partial overrides are supported — only the labels you specify are changed, the rest keep their defaults.

The `sk:notification-status` event fires regardless of whether the indicator is enabled.

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

- Auto-connects on page load and on `visibilitychange`/`focus`, disconnects on `blur` (opt out with `persistConnection: true`)
- When `persistConnection` is enabled, force-reconnects if the page was hidden for more than 30 seconds (handles silently killed mobile connections)
- On server shutdown, active clients receive a `disconnecting` notification and immediately start a reconnect attempt
- Reconnects after 5 seconds on error
- Deduplicates via internal ID set
- Max visible toasts: 3 (desktop) / 2 (mobile); excess queued
- Global instance: `window.__skriftNotifications`

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `notifications.backend` | `str` | `""` | Backend class path (`module:ClassName`). Empty = InMemory |
| `notifications.queued_ttl_seconds` | `float` | `86400` | Lifetime of queued notifications (> 0, finite, ≤ 3153600000) |
| `notifications.timeseries_ttl_seconds` | `float` | `604800` | Lifetime of timeseries notifications (> 0, finite, ≤ 3153600000) |
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

- [Web Push Notifications](web-push.md) — offline push delivery for users without active SSE connections
- [Hooks and Filters](hooks-and-filters.md) — `NOTIFICATION_SENT` and `NOTIFICATION_DISMISSED` hook constants
- [Custom Controllers](custom-controllers.md) — building controllers that send notifications
