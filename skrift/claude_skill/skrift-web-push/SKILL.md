---
name: skrift-web-push
description: "Skrift Web Push notifications — VAPID key management, browser subscription CRUD, push delivery with SSE fallback, and automatic hook integration."
---

# Skrift Web Push

Web Push notification support with VAPID authentication. Sends browser push notifications to users who don't have an active SSE connection, providing offline notification delivery.

Requires `pip install 'skrift[push]'` (`pywebpush` dependency). Gracefully no-ops if not installed.

## VAPID Key Management

Keys are auto-generated on first use and stored in the settings table:

```python
from skrift.lib.push import get_vapid_public_key

# Returns base64url-encoded public key (unpadded) for the browser Push API
public_key = await get_vapid_public_key(db_session)
```

Settings keys: `webpush:vapid_private`, `webpush:vapid_public`. Keys use ECDSA P-256 (SECP256R1). Private key is DER/PKCS8-encoded, public key is X962 uncompressed point format. Both are cached in-memory after first load.

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

Returns the number of notifications successfully sent. Automatically removes expired/unsubscribed endpoints (HTTP 404/410 responses).

### Unified — `notify()`

Always sends SSE; optionally falls back to push when user has no active SSE connection:

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

SSE connection detection checks both the user key (`user:{id}`) and downstream session children in the `SourceRegistry` DAG.

Override push content independently from SSE payload:

```python
await notify(
    db_session,
    user_id=user_id,
    event="comment",
    data={"comment_id": "...", "html": "<p>...</p>"},  # SSE payload
    push_title="New comment",    # push-specific overrides
    push_body="Someone commented on your post",
    push_url="/posts/123#comments",
    push_tag="comment",
)
```

### Automatic Hook — `setup_push_hook()`

Registers a `NOTIFICATION_SENT` action hook so **all** user-scoped notifications automatically trigger push fallback. Called during app startup in `asgi.py`:

```python
from skrift.lib.push import setup_push_hook

setup_push_hook(db_config.get_session)
```

The hook fires at priority 50, checks SSE connectivity, and sends push only when the user has no active connections.

## Subscription Management

```python
from skrift.lib.push import save_subscription, remove_subscription

# Save/update a browser subscription
await save_subscription(db_session, user_id, endpoint, p256dh, auth)

# Remove by endpoint URL — returns True if found
removed = await remove_subscription(db_session, endpoint)
```

## PushSubscription Model

```python
class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    user_id: Mapped[str]            # FK to user
    endpoint: Mapped[str]           # Push service URL (unique)
    key_p256dh: Mapped[str]         # Browser public key
    key_auth: Mapped[str]           # Auth secret
    last_used_at: Mapped[datetime | None]
```

Indexes: `(user_id)`, unique on `(endpoint)`.

## Controller Endpoints

`PushController` at `/push`:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/push/vapid-key` | Returns `{"publicKey": "..."}` |
| `POST` | `/push/subscribe` | Registers browser subscription (requires session auth) |
| `POST` | `/push/unsubscribe` | Removes subscription by endpoint |
| `GET` | `/sw.js` | Serves service worker at root scope |

Subscribe/unsubscribe endpoints are CSRF-exempt (configured in `asgi.py`).

## Client-Side JavaScript

### Service Worker (`sw.js`)

Handles `push` and `notificationclick` events. Push payload format:

```json
{"title": "...", "body": "...", "tag": "...", "url": "/..."}
```

On notification click: focuses an existing window matching the URL, or opens a new one.

### Push Client (`push.js`)

```javascript
// Subscribe to push notifications
await window.__skriftPush.subscribe();

// Unsubscribe
await window.__skriftPush.unsubscribe();

// Check subscription status
const subscribed = await window.__skriftPush.isSubscribed();
```

Include both scripts in your template:

```html
<script src="/static/js/push.js"></script>
<script>
  navigator.serviceWorker.register("/sw.js", { scope: "/" });
</script>
```

## Integration in `asgi.py`

- `PushController` and `service_worker` route registered automatically
- `/push/subscribe` and `/push/unsubscribe` added to CSRF exclude list
- `setup_push_hook()` called in `on_startup` if `pywebpush` is importable

## Key Files

| File | Purpose |
|------|---------|
| `skrift/lib/push.py` | VAPID keys, subscription CRUD, `send_push()`, `notify()`, `setup_push_hook()` |
| `skrift/db/models/push_subscription.py` | `PushSubscription` model |
| `skrift/controllers/push.py` | REST endpoints + service worker route |
| `skrift/static/js/push.js` | `SkriftPush` client library |
| `skrift/static/sw.js` | Service worker for push display |
| `skrift/alembic/versions/20260308_add_push_subscriptions.py` | Migration |
| `tests/test_push.py` | Full test suite |
