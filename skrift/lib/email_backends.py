"""Pluggable email backends.

Built-in backends:
- :class:`NullEmailBackend` — drops messages. Default when unconfigured; used
  by tests that don't assert on delivery.
- :class:`ConsoleEmailBackend` — logs the outbound message via ``logging``.
  Useful in development so operators can see what would have been sent.
- :class:`SMTPEmailBackend` — ``aiosmtplib``-backed for production.

Select a backend via ``settings.email.backend`` using a ``module:ClassName``
import string. Empty string falls back to :class:`NullEmailBackend`.
"""

from __future__ import annotations

import importlib
import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from skrift.config import EmailConfig

logger = logging.getLogger(__name__)


def load_backend(spec: str) -> type:
    """Import a backend class from a ``module:ClassName`` string."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid backend spec '{spec}': must be in format 'module:ClassName'"
        )
    module_path, class_name = spec.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


@runtime_checkable
class EmailBackend(Protocol):
    """Interface for sending transactional email.

    Implementations may be synchronous internally but must expose ``async``
    methods so the caller does not block the event loop. Failure modes are
    implementation-specific — callers should treat ``send_email`` as
    best-effort and never depend on delivery for security (the link token is
    the source of truth).
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_email(
        self,
        to: str,
        subject: str,
        text_body: str,
        *,
        html_body: str | None = None,
        from_address: str | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...


def _build_message(
    *,
    to: str,
    subject: str,
    text_body: str,
    from_address: str,
    html_body: str | None,
    reply_to: str | None,
    headers: dict[str, str] | None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    if headers:
        for name, value in headers.items():
            msg[name] = value
    msg.set_content(text_body)
    if html_body is not None:
        msg.add_alternative(html_body, subtype="html")
    return msg


class NullEmailBackend:
    """Default backend — silently drops every message."""

    def __init__(self, config: "EmailConfig | None" = None, **_: Any) -> None:
        self._config = config

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_email(
        self,
        to: str,
        subject: str,
        text_body: str,
        *,
        html_body: str | None = None,
        from_address: str | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        return None


class ConsoleEmailBackend:
    """Development backend — logs message headers + body via ``logging.INFO``."""

    def __init__(self, config: "EmailConfig | None" = None, **_: Any) -> None:
        self._config = config

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_email(
        self,
        to: str,
        subject: str,
        text_body: str,
        *,
        html_body: str | None = None,
        from_address: str | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        sender = from_address or (self._config.from_address if self._config else "")
        logger.info(
            "ConsoleEmailBackend: would send email\n"
            "From: %s\nTo: %s\nSubject: %s\n\n%s",
            sender,
            to,
            subject,
            text_body,
        )


class SMTPEmailBackend:
    """SMTP backend using ``aiosmtplib``.

    Connection details come from :class:`skrift.config.EmailConfig`. The
    ``aiosmtplib`` dependency is imported lazily so environments that pin a
    different backend never pay for the import.
    """

    def __init__(self, config: "EmailConfig", **_: Any) -> None:
        if config is None:  # pragma: no cover — defensive
            raise ValueError("SMTPEmailBackend requires an EmailConfig")
        self._config = config

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_email(
        self,
        to: str,
        subject: str,
        text_body: str,
        *,
        html_body: str | None = None,
        from_address: str | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        import aiosmtplib

        cfg = self._config
        sender = from_address or cfg.from_address
        if not sender:
            raise ValueError(
                "SMTPEmailBackend: no sender address — set email.from_address "
                "or pass from_address explicitly"
            )

        msg = _build_message(
            to=to,
            subject=subject,
            text_body=text_body,
            from_address=sender,
            html_body=html_body,
            reply_to=reply_to or cfg.reply_to or None,
            headers=headers,
        )

        await aiosmtplib.send(
            msg,
            hostname=cfg.smtp_host,
            port=cfg.smtp_port,
            start_tls=cfg.smtp_starttls,
            username=cfg.smtp_username or None,
            password=cfg.smtp_password or None,
            timeout=cfg.smtp_timeout,
        )


def build_email_backend(config: "EmailConfig") -> EmailBackend:
    """Construct the configured email backend, defaulting to the Null backend."""
    spec = (config.backend or "").strip()
    if not spec:
        return NullEmailBackend(config)
    cls = load_backend(spec)
    return cls(config=config)
