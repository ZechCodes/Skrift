"""Safe SQLAlchemy async session provider with CancelledError handling.

This module provides a custom session configuration that properly handles
connection cleanup when HTTP requests are cancelled (client disconnect, timeout).
Without this, CancelledError can prevent session cleanup, leading to connection
pool leaks.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Callable, cast

from advanced_alchemy._listeners import set_async_context
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig
from advanced_alchemy.extensions.litestar._utils import (
    delete_aa_scope_state,
    get_aa_scope_state,
    set_aa_scope_state,
)
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from litestar.datastructures import State
    from litestar.types import Scope


class SafeSQLAlchemyAsyncConfig(SQLAlchemyAsyncConfig):
    """SQLAlchemy async config with safe session cleanup on request cancellation.

    This subclass overrides `provide_session` to use an async generator that
    catches CancelledError and ensures sessions are properly closed, preventing
    connection pool leaks when HTTP requests are cancelled.

    The standard advanced_alchemy session management relies on ASGI events
    (http.response.body, http.disconnect) to trigger cleanup via before_send_handler.
    However, CancelledError can prevent these events from firing, leaving sessions
    in an unclosed state.

    By using an async generator, Litestar's dependency injection system ensures
    cleanup runs even when CancelledError is raised.
    """

    async def provide_session(
        self,
        state: "State",
        scope: "Scope",
    ) -> AsyncGenerator[AsyncSession, None]:
        """Provide a database session with proper cleanup on cancellation.

        This async generator wraps session creation to ensure that
        CancelledError (raised when an HTTP request is cancelled) doesn't
        prevent session cleanup, which would leak connections.

        Args:
            state: The application state
            scope: The ASGI scope

        Yields:
            AsyncSession: The database session
        """
        # Check if we already have a session in scope
        session = cast(
            "AsyncSession | None",
            get_aa_scope_state(scope, self.session_scope_key),
        )

        if session is None:
            # Create a new session
            session_maker = cast(
                "Callable[[], AsyncSession]",
                state[self.session_maker_app_state_key],
            )
            session = session_maker()
            # Store in scope for reuse within this request
            set_aa_scope_state(scope, self.session_scope_key, session)

        set_async_context(True)

        try:
            yield session
        except asyncio.CancelledError:
            # Request was cancelled - ensure we clean up the session
            # This prevents connection pool leaks
            await session.close()
            # Remove the session from scope state to prevent double-close
            delete_aa_scope_state(scope, self.session_scope_key)
            raise


__all__ = ["SafeSQLAlchemyAsyncConfig"]
