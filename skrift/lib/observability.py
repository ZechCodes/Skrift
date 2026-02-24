"""Observability facade wrapping Pydantic Logfire.

Provides structured tracing, request instrumentation, SQL query visibility,
and HTTP client tracking. Gracefully no-ops when logfire is not installed
or not enabled in configuration.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skrift.config import Settings

_logfire = None
_configured = False


def is_available() -> bool:
    return _logfire is not None and _configured


def get_logfire():
    """Return the raw logfire module, or None if unavailable."""
    return _logfire if _configured else None


def configure(settings: Settings) -> None:
    """Initialize logfire from LogfireConfig settings.

    No-ops if logfire is not installed or not enabled.
    """
    global _logfire, _configured

    if not settings.logfire.enabled:
        return

    try:
        import logfire as lf
    except ImportError:
        return

    kwargs: dict[str, Any] = {
        "service_name": settings.logfire.service_name,
        "send_to_logfire": "if-token-present",
    }
    if settings.logfire.environment:
        kwargs["environment"] = settings.logfire.environment
    if settings.logfire.sample_rate != 1.0:
        kwargs["trace_sample_rate"] = settings.logfire.sample_rate
    if settings.logfire.console:
        kwargs["console"] = lf.ConsoleOptions()

    lf.configure(**kwargs)
    _logfire = lf
    _configured = True


def instrument_app(app):
    """Wrap an ASGI app with logfire instrumentation. Returns the app unchanged if unavailable."""
    if not is_available():
        return app
    return _logfire.instrument_asgi(app)


def instrument_sqlalchemy(engine) -> None:
    """Instrument a SQLAlchemy engine."""
    if is_available():
        _logfire.instrument_sqlalchemy(engine=engine)


def instrument_httpx() -> None:
    """Instrument httpx globally."""
    if is_available():
        _logfire.instrument_httpx()


@contextmanager
def span(name: str, **attrs: Any):
    """Context manager that yields a logfire span, or None if unavailable."""
    if is_available():
        with _logfire.span(name, **attrs) as s:
            yield s
    else:
        yield None


def info(msg: str, **kwargs: Any) -> None:
    if is_available():
        _logfire.info(msg, **kwargs)


def error(msg: str, **kwargs: Any) -> None:
    if is_available():
        _logfire.error(msg, **kwargs)


def warning(msg: str, **kwargs: Any) -> None:
    if is_available():
        _logfire.warn(msg, **kwargs)


def exception(msg: str, **kwargs: Any) -> bool:
    """Log an exception with traceback via logfire. Returns True if logged, False if unavailable."""
    if is_available():
        _logfire.exception(msg, **kwargs)
        return True
    return False
