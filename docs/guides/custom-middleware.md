# Custom Middleware

<span class="skill-badge advanced">:material-star::material-star::material-star: Advanced</span>

Learn how to add custom middleware to process requests and responses in your Skrift application.

## Overview

Middleware in Skrift are ASGI middleware that wrap the application to intercept and process requests before they reach your controllers, and responses before they're sent to clients. Middleware is loaded dynamically from `app.yaml`, similar to controllers.

## Creating Middleware

### 1. Create the Middleware File

**`middleware/logging.py`**

```python
from litestar.types import ASGIApp, Receive, Scope, Send
import time
import logging

logger = logging.getLogger(__name__)

def create_logging_middleware(app: ASGIApp) -> ASGIApp:
    """Middleware factory that logs request timing."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        start_time = time.perf_counter()
        path = scope.get("path", "")
        method = scope.get("method", "")

        await app(scope, receive, send)

        duration = time.perf_counter() - start_time
        logger.info(f"{method} {path} completed in {duration:.3f}s")

    return middleware
```

### 2. Register in app.yaml

```yaml
middleware:
  - middleware.logging:create_logging_middleware
```

### 3. Restart the Application

```bash
python -m skrift
```

All requests will now be logged with timing information.

## Middleware with Configuration

For middleware that needs configuration, use the dict format with `kwargs`:

### 1. Create Configurable Middleware

**`middleware/rate_limit.py`**

```python
from litestar.types import ASGIApp, Receive, Scope, Send
from collections import defaultdict
import time

def create_rate_limit_middleware(
    app: ASGIApp,
    requests_per_minute: int = 60,
    burst_limit: int = 10
) -> ASGIApp:
    """Rate limiting middleware with configurable limits."""

    request_counts: dict[str, list[float]] = defaultdict(list)

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        # Get client IP
        client = scope.get("client", ("unknown", 0))
        client_ip = client[0] if client else "unknown"

        # Check rate limit
        now = time.time()
        minute_ago = now - 60

        # Clean old requests
        request_counts[client_ip] = [
            t for t in request_counts[client_ip]
            if t > minute_ago
        ]

        if len(request_counts[client_ip]) >= requests_per_minute:
            # Rate limited - return 429
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({
                "type": "http.response.body",
                "body": b"Rate limit exceeded",
            })
            return

        request_counts[client_ip].append(now)
        await app(scope, receive, send)

    return middleware
```

### 2. Register with Configuration

```yaml
middleware:
  - factory: middleware.rate_limit:create_rate_limit_middleware
    kwargs:
      requests_per_minute: 100
      burst_limit: 20
```

## Middleware Class Pattern

You can also use a class-based approach:

**`middleware/auth_header.py`**

```python
from litestar.types import ASGIApp, Receive, Scope, Send

class ApiKeyMiddleware:
    """Middleware that validates API keys for /api routes."""

    def __init__(self, app: ASGIApp, api_keys: list[str] | None = None):
        self.app = app
        self.api_keys = set(api_keys or [])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Only check API routes
        if not path.startswith("/api"):
            await self.app(scope, receive, send)
            return

        # Extract API key from headers
        headers = dict(scope.get("headers", []))
        api_key = headers.get(b"x-api-key", b"").decode()

        if api_key not in self.api_keys:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error": "Invalid API key"}',
            })
            return

        await self.app(scope, receive, send)
```

Register with kwargs:

```yaml
middleware:
  - factory: middleware.auth_header:ApiKeyMiddleware
    kwargs:
      api_keys:
        - secret-key-1
        - secret-key-2
```

## Configuration Formats

### Simple Format (No Arguments)

For middleware factories that don't need configuration:

```yaml
middleware:
  - myapp.middleware:simple_middleware
```

### Dict Format (With Arguments)

For middleware that needs configuration:

```yaml
middleware:
  - factory: myapp.middleware:configurable_middleware
    kwargs:
      option1: value1
      option2: value2
```

You can also use the dict format without kwargs (equivalent to simple format):

```yaml
middleware:
  - factory: myapp.middleware:simple_middleware
```

## Middleware Order

Middleware is applied in a specific order:

1. **Security headers middleware** (built-in, outermost) - Injects security response headers
2. **Session middleware** (built-in) - Handles encrypted session cookies
3. **Your middleware** - Applied in the order listed in `app.yaml`

```yaml
middleware:
  - middleware.logging:create_logging_middleware    # Runs third
  - middleware.rate_limit:create_rate_limit_middleware  # Runs fourth
  - middleware.cors:create_cors_middleware          # Runs fifth
```

!!! info "Middleware Execution"
    Middleware wraps the application in reverse order. The first middleware listed is the outermost wrapper, meaning it sees requests first and responses last. The security headers middleware is always outermost, so it adds headers to every response including error pages.

## Common Middleware Patterns

### CORS Middleware

```python
def create_cors_middleware(
    app: ASGIApp,
    allowed_origins: list[str] | None = None
) -> ASGIApp:
    """Add CORS headers to responses."""

    origins = set(allowed_origins or ["*"])

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        # Handle preflight
        if scope.get("method") == "OPTIONS":
            headers = [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS"),
                (b"access-control-allow-headers", b"*"),
            ]
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": headers,
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # Wrap send to add CORS headers
        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"access-control-allow-origin", b"*"))
                message = {**message, "headers": headers}
            await send(message)

        await app(scope, receive, send_with_cors)

    return middleware
```

### Request ID Middleware

```python
import uuid

def create_request_id_middleware(app: ASGIApp) -> ASGIApp:
    """Add unique request ID to each request."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request_id = str(uuid.uuid4())
            scope["state"]["request_id"] = request_id

        await app(scope, receive, send)

    return middleware
```

Access in controllers:

```python
@get("/")
async def index(self, request: Request) -> dict:
    request_id = request.state.get("request_id")
    return {"request_id": request_id}
```

### Error Handling Middleware

```python
import logging
import traceback

logger = logging.getLogger(__name__)

def create_error_middleware(app: ASGIApp) -> ASGIApp:
    """Catch and log unhandled errors."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        try:
            await app(scope, receive, send)
        except Exception as e:
            logger.error(f"Unhandled error: {e}\n{traceback.format_exc()}")
            await send({
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({
                "type": "http.response.body",
                "body": b"Internal Server Error",
            })

    return middleware
```

## Best Practices

### 1. Check Request Type

Always check `scope["type"]` before processing:

```python
async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] != "http":
        # Pass through WebSocket, lifespan, etc.
        await app(scope, receive, send)
        return
    # Process HTTP requests
```

### 2. Keep Middleware Focused

Each middleware should do one thing well:

```python
# Good - single responsibility
def create_timing_middleware(app): ...
def create_logging_middleware(app): ...

# Avoid - multiple responsibilities
def create_timing_and_logging_and_auth_middleware(app): ...
```

### 3. Use Factory Functions

Always use factory functions, not bare middleware functions:

```python
# Good - factory pattern
def create_middleware(app: ASGIApp) -> ASGIApp:
    async def middleware(scope, receive, send):
        await app(scope, receive, send)
    return middleware

# Avoid - bare function
async def middleware(scope, receive, send):
    ...  # No access to app!
```

### 4. Handle Errors Gracefully

Don't let middleware errors crash the application:

```python
async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
    try:
        # Middleware logic
        await app(scope, receive, send)
    except MiddlewareSpecificError:
        # Handle known errors
        await send_error_response(send, 400, "Bad request")
    # Let other errors propagate to error handlers
```

### 5. Document Configuration

Add docstrings explaining kwargs:

```python
def create_rate_limit_middleware(
    app: ASGIApp,
    requests_per_minute: int = 60,
    burst_limit: int = 10
) -> ASGIApp:
    """Rate limiting middleware.

    Args:
        app: The ASGI application to wrap
        requests_per_minute: Maximum requests per minute per client
        burst_limit: Maximum burst of requests allowed
    """
```

## Testing Middleware

```python
import pytest
from litestar import Litestar, get
from litestar.testing import TestClient

from middleware.logging import create_logging_middleware

@pytest.fixture
def app():
    @get("/")
    async def index() -> dict:
        return {"status": "ok"}

    return Litestar(
        route_handlers=[index],
        middleware=[create_logging_middleware]
    )

@pytest.fixture
def client(app):
    return TestClient(app)

def test_middleware_passes_request(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

## Next Steps

- [Custom Controllers](custom-controllers.md) - Add routes and endpoints
- [Configuration](../core-concepts/configuration.md) - Full config reference
- [Litestar Middleware](https://docs.litestar.dev/latest/usage/middleware/) - Framework documentation
