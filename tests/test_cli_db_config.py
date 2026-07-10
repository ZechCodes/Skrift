"""Tests for _build_db_config engine wiring (pgbouncer transaction mode)."""

from __future__ import annotations

from types import SimpleNamespace

from advanced_alchemy.utils.dataclass import Empty
from sqlalchemy.pool import NullPool

from skrift.cli import _build_db_config
from skrift.config import DatabaseConfig


def _settings(**db_overrides):
    return SimpleNamespace(db=DatabaseConfig(**db_overrides))


def test_default_postgres_emits_no_connect_args_or_poolclass():
    config = _build_db_config(_settings(url="postgresql+asyncpg://u:p@h/db"))

    engine_config = config.engine_config
    assert engine_config.connect_args is Empty
    assert engine_config.poolclass is Empty


def test_pgbouncer_transaction_mode_disables_prepared_statements_and_pool():
    config = _build_db_config(
        _settings(
            url="postgresql+asyncpg://u:p@h/db",
            pgbouncer_transaction_mode=True,
        )
    )

    engine_config = config.engine_config
    assert engine_config.connect_args == {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }
    assert engine_config.poolclass is NullPool


def test_statement_cache_size_zero_disables_prepared_statements_and_pool():
    config = _build_db_config(
        _settings(url="postgresql+asyncpg://u:p@h/db", statement_cache_size=0)
    )

    engine_config = config.engine_config
    assert engine_config.connect_args == {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }
    assert engine_config.poolclass is NullPool


def test_positive_statement_cache_size_threads_only_that_value():
    config = _build_db_config(
        _settings(url="postgresql+asyncpg://u:p@h/db", statement_cache_size=200)
    )

    engine_config = config.engine_config
    assert engine_config.connect_args == {"statement_cache_size": 200}
    assert engine_config.poolclass is Empty


def test_default_sqlite_url_gets_no_asyncpg_connect_args():
    config = _build_db_config(_settings())

    engine_config = config.engine_config
    assert engine_config.connect_args is Empty
    assert engine_config.poolclass is Empty


def test_database_config_new_fields_round_trip_defaults():
    db = DatabaseConfig()
    assert db.statement_cache_size is None
    assert db.pgbouncer_transaction_mode is False
