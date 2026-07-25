"""Tests for the ``skrift serve`` CLI command."""

from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from skrift.cli import cli


def _invoke_serve(*args):
    """Invoke ``skrift serve`` with both hypercorn entry points mocked out."""
    process_manager_run = MagicMock(return_value=0)
    in_process_serve = AsyncMock(return_value=[])
    with (
        patch("hypercorn.run.run", process_manager_run),
        patch("hypercorn.asyncio.serve", in_process_serve),
    ):
        result = CliRunner().invoke(cli, ["serve", *args])
    return result, process_manager_run, in_process_serve


def test_serve_with_multiple_workers_uses_hypercorn_process_manager():
    result, process_manager_run, in_process_serve = _invoke_serve(
        "--workers", "4", "--host", "0.0.0.0", "--port", "9000"
    )

    assert result.exit_code == 0, result.output
    in_process_serve.assert_not_called()
    process_manager_run.assert_called_once()

    config = process_manager_run.call_args.args[0]
    assert config.workers == 4
    assert config.application_path == "skrift.asgi:app"
    assert config.bind == ["0.0.0.0:9000"]
    assert not config.use_reloader


def test_serve_with_single_worker_runs_the_app_in_process():
    result, process_manager_run, in_process_serve = _invoke_serve("--workers", "1")

    assert result.exit_code == 0, result.output
    process_manager_run.assert_not_called()
    in_process_serve.assert_awaited_once()

    served_app, config = in_process_serve.await_args.args
    assert served_app is not None
    assert config.workers == 1
    assert "shutdown_trigger" in in_process_serve.await_args.kwargs


def test_serve_defaults_to_a_single_in_process_worker():
    result, process_manager_run, in_process_serve = _invoke_serve()

    assert result.exit_code == 0, result.output
    process_manager_run.assert_not_called()
    in_process_serve.assert_awaited_once()

    _, config = in_process_serve.await_args.args
    assert config.workers == 1
    assert config.bind == ["127.0.0.1:8080"]


def test_serve_with_reload_uses_the_reloader_with_one_worker():
    result, process_manager_run, in_process_serve = _invoke_serve("--reload", "--workers", "4")

    assert result.exit_code == 0, result.output
    in_process_serve.assert_not_called()
    process_manager_run.assert_called_once()

    config = process_manager_run.call_args.args[0]
    assert config.use_reloader is True
    assert config.workers == 1


def test_serve_rejects_worker_counts_below_one():
    result, process_manager_run, in_process_serve = _invoke_serve("--workers", "0")

    assert result.exit_code != 0
    assert "--workers must be at least 1" in result.output
    process_manager_run.assert_not_called()
    in_process_serve.assert_not_called()
