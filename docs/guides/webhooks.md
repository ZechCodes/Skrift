# Outbound Webhooks

Skrift's outbound webhook framework stores webhook calls in the database before
workers send them. This gives webhook enqueue the same transaction boundary as
your application writes.

## Configure Profiles

Enable workers and define one or more webhook caller profiles in `app.yaml`:

```yaml
workers:
  enabled: true
  preset: single_node
  queues:
    - default
    - webhooks

webhooks:
  enabled: true
  reconcile_interval_seconds: 60
  profiles:
    crm:
      url: https://crm.example.com/skrift/webhook
      queue: webhooks
      signing_secret: $CRM_WEBHOOK_SECRET
      timeout_seconds: 10
      max_attempts: 12
      dead_letter_after_seconds: 86400
      backoff:
        initial_seconds: 5
        factor: 2
        max_seconds: 900
        jitter_seconds: 3
      retention:
        succeeded_seconds: 2592000
        dead_seconds: 7776000
```

Each profile has its own endpoint, queue, timeout, retry thresholds, exponential
backoff, signing secret, and retention policy.

## Enqueue In Your Transaction

Pass the active SQLAlchemy `AsyncSession` to `enqueue_webhook`. The function
inserts a `webhook_deliveries` row and never commits.

```python
from sqlalchemy.ext.asyncio import AsyncSession

import skrift


async def publish_page(db_session: AsyncSession, page) -> None:
    page.is_published = True

    await skrift.enqueue_webhook(
        db_session,
        profile="crm",
        event_type="page.published",
        idempotency_key=f"page:{page.id}:published:{page.updated_at.isoformat()}",
        payload={"page_id": str(page.id), "slug": page.slug},
    )

    await db_session.commit()
```

If the transaction rolls back, no delivery row exists and no webhook is sent. On
commit, Skrift best-effort submits the worker job. If that nudge fails, the
`webhooks.reconcile` worker job scans durable delivery rows and submits missing
jobs later.

Use `enqueue_webhook_standalone(...)` only when you do not already have a
transaction; it opens its own session and commits.

## Idempotency

`profile + idempotency_key` is unique. Repeating the same enqueue returns the
existing delivery. Reusing the same key with a different payload raises
`WebhookIdempotencyConflict`.

Every HTTP request includes stable receiver-side idempotency headers:

| Header | Purpose |
|--------|---------|
| `Idempotency-Key` | Caller-provided idempotency key |
| `X-Skrift-Delivery-Id` | Durable delivery row id |
| `X-Skrift-Attempt` | Delivery attempt number |
| `X-Skrift-Event-Type` | Application event type |
| `X-Skrift-Timestamp` | Unix timestamp used for signing |
| `X-Skrift-Signature` | `v1=` HMAC-SHA256 signature when a secret is configured |

Webhook delivery is durable at-least-once. A receiver can process a request and
then the worker can crash before recording success, so receivers should dedupe
by `Idempotency-Key` or `X-Skrift-Delivery-Id`.

## Retry And Dead States

HTTP `2xx` marks the delivery `succeeded`. Configured permanent failure statuses
mark it `dead`. Retry statuses and network errors schedule the next attempt with
profile-specific exponential backoff until `max_attempts` or
`dead_letter_after_seconds` is reached.

Worker DLQ entries still represent infrastructure failures in the worker itself.
Remote endpoint failures are represented by `webhook_deliveries.status = "dead"`
so operators can distinguish delivery exhaustion from worker runtime failure.
