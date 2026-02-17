# Observability with Logfire

Skrift integrates with [Pydantic Logfire](https://logfire.pydantic.dev/) for structured tracing and observability. The integration is optional — when not installed or not enabled, everything no-ops silently.

## Setup

### 1. Install the extra

```bash
pip install skrift[logfire]
```

### 2. Enable in app.yaml

```yaml
logfire:
  enabled: true
  service_name: my-site
  console: true  # prints spans to stdout
```

### 3. (Optional) Add a Logfire token

To send data to the Logfire dashboard, set the token in your environment:

```bash
export LOGFIRE_TOKEN=your-logfire-token
```

Without a token, spans still print to console when `console: true`.

## What Gets Traced Automatically

Once enabled, Skrift instruments:

- **HTTP requests** — every incoming request with method, path, status code, and duration
- **SQL queries** — every SQLAlchemy query with statement text
- **HTTP client calls** — all outgoing httpx requests (OAuth exchanges, external APIs)
- **Hook execution** — every `do_action()` and `apply_filters()` call
- **OAuth flows** — token exchange and user info fetch grouped in a span

## Adding Custom Spans

Use the `span()` context manager to trace your own operations:

```python
from skrift.lib.observability import span

async def process_order(order):
    with span("process_order", order_id=str(order.id)):
        await validate_payment(order)
        await ship_order(order)
```

When logfire is not available, `span()` yields `None` with zero overhead.

## Structured Logging

```python
from skrift.lib.observability import info, error, warning

info("Order processed", order_id=str(order.id), total=order.total)
warning("Slow query detected", query_time_ms=elapsed)
error("Payment failed", order_id=str(order.id), reason=str(e))
```

These are no-ops when logfire is not active.

## Custom Instrumentation via Hooks

The `logfire_configured` action fires after all built-in instrumentation is set up. Use it to add your own:

```python
from skrift.lib.hooks import action
from skrift.lib.observability import get_logfire

@action("logfire_configured")
async def add_celery_instrumentation():
    lf = get_logfire()
    if lf:
        lf.instrument_celery()
```

## Configuration Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | `bool` | `false` | Enable Logfire integration |
| `service_name` | `str` | `"skrift"` | Service name in dashboard |
| `environment` | `str \| null` | `null` (uses `SKRIFT_ENV`) | Environment label |
| `sample_rate` | `float` | `1.0` | Trace sampling rate (0.0–1.0) |
| `console` | `bool` | `false` | Print spans to stdout |

## Development vs Production

=== "Development"

    ```yaml
    logfire:
      enabled: true
      console: true  # See spans in terminal
    ```

    No `LOGFIRE_TOKEN` needed — spans print to console.

=== "Production"

    ```yaml
    logfire:
      enabled: true
      service_name: my-site
      sample_rate: 0.5  # Sample 50% of traces
    ```

    ```bash
    export LOGFIRE_TOKEN=your-production-token
    ```

## See Also

- [Configuration](../core-concepts/configuration.md) - Full config reference
- [Hooks and Filters](hooks-and-filters.md) - Hook system for extensibility
