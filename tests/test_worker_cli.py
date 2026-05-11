"""Tests for worker process CLI commands."""

from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
import pytest
from click.testing import CliRunner

import skrift.config as config_mod
from skrift.cli import cli
from skrift.config import clear_settings_cache, set_config_path


def teardown_function():
    config_mod._config_path_override = None
    clear_settings_cache()


def test_worker_cli_help_exposes_process_commands():
    runner = CliRunner()

    result = runner.invoke(cli, ["workers", "--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "persister" in result.output
    assert "prune" in result.output
    assert "queues" in result.output
    assert "jobs" in result.output
    assert "dlq" in result.output

    result = runner.invoke(cli, ["workers", "run", "--help"])
    assert result.exit_code == 0
    assert "--queue" in result.output

    result = runner.invoke(cli, ["workers", "persister", "--help"])
    assert result.exit_code == 0
    assert "--stream" in result.output
    assert "--once" in result.output

    result = runner.invoke(cli, ["persister", "--help"])
    assert result.exit_code != 0


def test_persister_fails_closed_with_memory_backends():
    runner = CliRunner()

    with patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False):
        result = runner.invoke(cli, ["workers", "persister", "--once"])

    assert result.exit_code != 0
    assert "Memory backends configured" in result.output


def test_worker_operability_help_exposes_phase_7_commands():
    runner = CliRunner()

    for command in (
        ["workers", "queues", "list", "--help"],
        ["workers", "jobs", "inspect", "--help"],
        ["workers", "dlq", "list", "--help"],
        ["workers", "dlq", "inspect", "--help"],
        ["workers", "dlq", "retry", "--help"],
        ["workers", "dlq", "discard", "--help"],
        ["workers", "dlq", "export", "--help"],
    ):
        result = runner.invoke(cli, command)
        assert result.exit_code == 0, result.output


def test_worker_queues_list_allows_memory_for_local_testing():
    runner = CliRunner()

    with patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False):
        result = runner.invoke(
            cli,
            ["workers", "queues", "list", "--allow-memory-backends"],
        )

    assert result.exit_code == 0
    assert "Queue" in result.output
    assert "default" in result.output


def test_worker_jobs_inspect_prints_job_state():
    from skrift.workers.models import JobEnvelope, JobState, JobStatus

    runner = CliRunner()
    runtime = MagicMock()
    runtime.get_job_state = AsyncMock(
        return_value=JobState(
            job=JobEnvelope(id="job-1", type="demo.job", payload={"name": "Ada"}),
            status=JobStatus.COMPLETED,
            result={"ok": True},
        )
    )
    runtime.lifecycle_events_for_job = AsyncMock(
        return_value=[
            (0, {"job_id": "job-1", "type": "job_submitted", "attempt": 0}),
        ]
    )
    db_config = MagicMock()
    db_config.get_session = MagicMock()
    db_config.get_engine.return_value.dispose = AsyncMock()

    with (
        patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False),
        patch("skrift.cli._build_db_config", return_value=db_config),
        patch("skrift.cli._configure_worker_runtime", return_value=runtime),
    ):
        result = runner.invoke(
            cli,
            ["workers", "jobs", "inspect", "job-1", "--allow-memory-backends"],
        )

    assert result.exit_code == 0
    assert "Job: job-1" in result.output
    assert "demo.job" in result.output
    assert "job_submitted" in result.output


def test_worker_dlq_list_prints_entries():
    from skrift.workers.models import DeadJobEntry, DeadLetterCause, JobEnvelope

    runner = CliRunner()
    entry = DeadJobEntry(
        id="entry-1",
        job=JobEnvelope(id="job-1", type="demo.job", payload={"name": "Ada"}),
        queue="default",
        job_type="demo.job",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
        latest_error="boom",
    )
    runtime = MagicMock()
    runtime.inspect_dlq = AsyncMock(return_value=[entry])
    db_config = MagicMock()
    db_config.get_session = MagicMock()
    db_config.get_engine.return_value.dispose = AsyncMock()

    with (
        patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False),
        patch("skrift.cli._build_db_config", return_value=db_config),
        patch("skrift.cli._configure_worker_runtime", return_value=runtime),
    ):
        result = runner.invoke(
            cli,
            ["workers", "dlq", "list", "--allow-memory-backends"],
        )

    assert result.exit_code == 0
    assert "entry-1" in result.output
    assert "demo.job" in result.output
    assert "retries_exhausted" in result.output


def test_worker_dlq_retry_replays_multiple_ids():
    runner = CliRunner()
    runtime = MagicMock()
    runtime.retry_dlq_entry = AsyncMock(side_effect=[
        MagicMock(id="job-a"),
        MagicMock(id="job-b"),
    ])
    db_config = MagicMock()
    db_config.get_session = MagicMock()
    db_config.get_engine.return_value.dispose = AsyncMock()

    with (
        patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False),
        patch("skrift.cli._build_db_config", return_value=db_config),
        patch("skrift.cli._configure_worker_runtime", return_value=runtime),
    ):
        result = runner.invoke(
            cli,
            ["workers", "dlq", "retry", "entry-a", "entry-b", "--allow-memory-backends"],
        )

    assert result.exit_code == 0, result.output
    assert "Replayed entry-a to job job-a" in result.output
    assert "Replayed entry-b to job job-b" in result.output
    runtime.retry_dlq_entry.assert_any_await("entry-a", force=False)
    runtime.retry_dlq_entry.assert_any_await("entry-b", force=False)


def test_worker_dlq_discard_filters_entries_with_since():
    from skrift.workers.models import DeadJobEntry, DeadLetterCause, JobEnvelope

    runner = CliRunner()
    entry = DeadJobEntry(
        id="entry-filtered",
        job=JobEnvelope(id="job-1", type="demo.job", payload={"name": "Ada"}),
        queue="default",
        job_type="demo.job",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
    )
    runtime = MagicMock()
    runtime.inspect_dlq = AsyncMock(return_value=[entry])
    runtime.discard_dlq_entry = AsyncMock(return_value=entry)
    db_config = MagicMock()
    db_config.get_session = MagicMock()
    db_config.get_engine.return_value.dispose = AsyncMock()

    with (
        patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False),
        patch("skrift.cli._build_db_config", return_value=db_config),
        patch("skrift.cli._configure_worker_runtime", return_value=runtime),
    ):
        result = runner.invoke(
            cli,
            [
                "workers",
                "dlq",
                "discard",
                "--queue",
                "default",
                "--cause",
                "retries_exhausted",
                "--since",
                "1h",
                "--reason",
                "bad deploy",
                "--allow-memory-backends",
            ],
        )

    assert result.exit_code == 0, result.output
    filters = runtime.inspect_dlq.await_args.kwargs
    assert filters["queue"] == "default"
    assert filters["cause"] == "retries_exhausted"
    assert filters["state"] == "open"
    assert isinstance(filters["created_after"], datetime)
    assert datetime.now(timezone.utc) - filters["created_after"] < timedelta(hours=2)
    runtime.discard_dlq_entry.assert_awaited_once_with(
        "entry-filtered",
        reason="bad deploy",
    )


def test_worker_dlq_bulk_action_requires_ids_or_filters():
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["workers", "dlq", "retry", "--allow-memory-backends"],
    )

    assert result.exit_code != 0
    assert "Pass at least one ENTRY_ID or a DLQ filter" in result.output


def test_worker_dlq_retry_dry_run_does_not_mutate():
    from skrift.workers.models import DeadJobEntry, DeadLetterCause, JobEnvelope

    runner = CliRunner()
    entry = DeadJobEntry(
        id="entry-dry-run",
        job=JobEnvelope(id="job-1", type="demo.job", payload={}),
        queue="default",
        job_type="demo.job",
        cause=DeadLetterCause.PERMANENT_FAILURE,
    )
    runtime = MagicMock()
    runtime.inspect_dlq = AsyncMock(return_value=[entry])
    runtime.retry_dlq_entry = AsyncMock()
    db_config = MagicMock()
    db_config.get_session = MagicMock()
    db_config.get_engine.return_value.dispose = AsyncMock()

    with (
        patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False),
        patch("skrift.cli._build_db_config", return_value=db_config),
        patch("skrift.cli._configure_worker_runtime", return_value=runtime),
    ):
        result = runner.invoke(
            cli,
            [
                "workers",
                "dlq",
                "retry",
                "--cause",
                "permanent_failure",
                "--dry-run",
                "--allow-memory-backends",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "entry-dry-run" in result.output
    runtime.retry_dlq_entry.assert_not_awaited()


def test_worker_dlq_retry_reports_force_errors_as_json():
    runner = CliRunner()
    runtime = MagicMock()
    runtime.retry_dlq_entry = AsyncMock(side_effect=PermissionError("Permanent failures require force retry"))
    db_config = MagicMock()
    db_config.get_session = MagicMock()
    db_config.get_engine.return_value.dispose = AsyncMock()

    with (
        patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False),
        patch("skrift.cli._build_db_config", return_value=db_config),
        patch("skrift.cli._configure_worker_runtime", return_value=runtime),
    ):
        result = runner.invoke(
            cli,
            [
                "workers",
                "dlq",
                "retry",
                "entry-permanent",
                "--json",
                "--allow-memory-backends",
            ],
        )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["changed"] == []
    assert payload["errors"][0]["entry_id"] == "entry-permanent"
    assert "Permanent failures require force retry" in payload["errors"][0]["error"]


def test_worker_config_accepts_out_of_process_and_persistence(tmp_path):
    config_file = tmp_path / "app.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "workers": {
                    "enabled": True,
                    "execution": "out_of_process",
                    "imports": ["example.handlers"],
                    "persistence": {
                        "streams": ["workers:lifecycle", "demo"],
                        "stream_prefixes": ["agents:run"],
                        "batch_size": 25,
                        "snapshot_keys": ["workers:queue_wait_history"],
                    },
                }
            }
        )
    )
    set_config_path(config_file)
    with patch.dict(os.environ, {"SECRET_KEY": "test-secret"}, clear=False):
        settings = config_mod.get_settings()

    assert settings.workers.execution == "out_of_process"
    assert settings.workers.imports == ["example.handlers"]
    assert settings.workers.persistence.streams == ["workers:lifecycle", "demo"]
    assert settings.workers.persistence.stream_prefixes == ["agents:run"]
    assert settings.workers.persistence.batch_size == 25


def test_worker_presets_select_backend_defaults():
    from skrift.config import WorkersConfig

    local = WorkersConfig(preset="local")
    assert local.execution == "inline"
    assert ".memory:" in local.backends.queue

    single_node = WorkersConfig(preset="single_node")
    assert single_node.execution == "in_process"
    assert ".sqlalchemy:" in single_node.backends.queue

    distributed = WorkersConfig(preset="distributed")
    assert distributed.execution == "out_of_process"
    assert ".redis:" in distributed.backends.state_store
    assert ".redis:" in distributed.backends.queue
    assert ".sqlalchemy:" in distributed.backends.archive

    overridden = WorkersConfig(
        preset="distributed",
        execution="in_process",
        backends={"queue": "skrift.workers.memory:InMemoryQueue"},
    )
    assert overridden.execution == "in_process"
    assert overridden.backends.queue == "skrift.workers.memory:InMemoryQueue"
    assert ".redis:" in overridden.backends.state_store


def test_worker_config_validation_rejects_invalid_process_backends():
    from skrift.config import WorkersConfig, validate_worker_runtime_config

    with pytest.raises(ValueError, match="out_of_process workers require shared"):
        validate_worker_runtime_config(
            WorkersConfig(enabled=True, execution="out_of_process"),
            context="web",
        )

    with pytest.raises(ValueError, match="Standalone worker processes require shared"):
        validate_worker_runtime_config(WorkersConfig(), context="worker")

    validate_worker_runtime_config(WorkersConfig(preset="distributed"), context="web")
    validate_worker_runtime_config(
        WorkersConfig(),
        context="worker",
        allow_memory_backends=True,
    )
