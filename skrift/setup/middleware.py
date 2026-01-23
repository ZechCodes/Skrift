"""Setup wizard middleware for redirecting users to the setup wizard."""

from litestar.response import Redirect
from litestar.types import ASGIApp, Receive, Scope, Send

# Paths that should be accessible during setup
SETUP_ALLOWED_PATHS = (
    "/setup",
    "/static",
    "/auth",  # Needed for OAuth callbacks during admin creation
)


class SetupMiddleware:
    """ASGI middleware to redirect users to setup wizard if not configured."""

    def __init__(self, app: ASGIApp, setup_complete: bool = False, use_app_state: bool = False) -> None:
        """Initialize the middleware.

        Args:
            app: The ASGI application
            setup_complete: Whether setup has been completed (static mode)
            use_app_state: Whether to check app.state.setup_complete at runtime
        """
        self.app = app
        self._setup_complete = setup_complete
        self._use_app_state = use_app_state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Process the request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Check setup complete status - either from app state (dynamic) or instance var (static)
        setup_complete = self._setup_complete
        if self._use_app_state and "app" in scope:
            litestar_app = scope["app"]
            setup_complete = getattr(litestar_app.state, "setup_complete", self._setup_complete)

        # If setup is complete, block access to /setup
        if setup_complete:
            if path.startswith("/setup"):
                # Redirect setup routes to home after setup is complete
                response = Redirect(path="/")
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # Setup not complete - check if path is allowed
        if any(path.startswith(allowed) for allowed in SETUP_ALLOWED_PATHS):
            await self.app(scope, receive, send)
            return

        # Redirect to setup wizard
        response = Redirect(path="/setup")
        await response(scope, receive, send)


def create_setup_middleware_factory(setup_complete: bool):
    """Create a middleware factory for the setup middleware (static mode).

    Args:
        setup_complete: Whether setup has been completed

    Returns:
        Middleware factory function
    """

    def factory(app: ASGIApp) -> SetupMiddleware:
        return SetupMiddleware(app, setup_complete=setup_complete)

    return factory


def create_dynamic_setup_middleware_factory():
    """Create a middleware factory that checks app state at runtime.

    Returns:
        Middleware factory function
    """

    def factory(app: ASGIApp) -> SetupMiddleware:
        return SetupMiddleware(app, setup_complete=False, use_app_state=True)

    return factory
