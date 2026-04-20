"""Email backend abstraction tests."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from skrift.config import EmailConfig
from skrift.lib.email_backends import (
    ConsoleEmailBackend,
    NullEmailBackend,
    SMTPEmailBackend,
    build_email_backend,
    load_backend,
)


@pytest.mark.asyncio
async def test_null_backend_is_no_op():
    backend = NullEmailBackend(EmailConfig())
    await backend.start()
    await backend.send_email(to="a@b.com", subject="x", text_body="y")
    await backend.stop()
    # No assertion needed — calling without raising is the contract.


@pytest.mark.asyncio
async def test_console_backend_logs_outbound_message(caplog):
    backend = ConsoleEmailBackend(EmailConfig(from_address="noreply@test"))
    await backend.start()
    with caplog.at_level(logging.INFO, logger="skrift.lib.email_backends"):
        await backend.send_email(
            to="user@example.com", subject="Hello", text_body="world"
        )
    await backend.stop()

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "user@example.com" in joined
    assert "Hello" in joined
    assert "world" in joined
    assert "noreply@test" in joined


@pytest.mark.asyncio
async def test_smtp_backend_dispatches_via_aiosmtplib():
    cfg = EmailConfig(
        backend="skrift.lib.email_backends:SMTPEmailBackend",
        from_address="noreply@test",
        smtp_host="smtp.test",
        smtp_port=2525,
        smtp_starttls=False,
    )
    backend = SMTPEmailBackend(cfg)

    send_mock = AsyncMock(return_value=None)
    with patch("aiosmtplib.send", send_mock):
        await backend.send_email(
            to="user@example.com", subject="hi", text_body="hello"
        )

    assert send_mock.await_count == 1
    call_args, call_kwargs = send_mock.await_args.args, send_mock.await_args.kwargs
    msg = call_args[0]
    assert msg["To"] == "user@example.com"
    assert msg["Subject"] == "hi"
    assert msg["From"] == "noreply@test"
    assert call_kwargs["hostname"] == "smtp.test"
    assert call_kwargs["port"] == 2525
    assert call_kwargs["start_tls"] is False


@pytest.mark.asyncio
async def test_smtp_backend_requires_sender_address():
    cfg = EmailConfig(smtp_host="smtp.test")
    backend = SMTPEmailBackend(cfg)
    with pytest.raises(ValueError):
        await backend.send_email(to="u@x", subject="s", text_body="b")


def test_load_backend_rejects_bad_specs():
    with pytest.raises(ValueError):
        load_backend("no-colon-here")


def test_build_email_backend_defaults_to_null_when_unset():
    backend = build_email_backend(EmailConfig())
    assert isinstance(backend, NullEmailBackend)


def test_build_email_backend_loads_spec():
    backend = build_email_backend(
        EmailConfig(backend="skrift.lib.email_backends:ConsoleEmailBackend")
    )
    assert isinstance(backend, ConsoleEmailBackend)
