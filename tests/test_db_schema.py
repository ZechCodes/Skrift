"""db.schema: app models referencing built-in tables resolve, queries land in the schema.

Skrift's own tables register on ``Base.metadata`` under bare keys at import
time, so the configured schema is applied by the engine's
``schema_translate_map`` rather than ``Base.metadata.schema`` (#116).
"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Column, ForeignKey, Table, Uuid, create_engine, event, insert, inspect, select

import skrift.asgi as asgi
from skrift.cli import _build_db_config
from skrift.config import DatabaseConfig, Settings
from skrift.db.base import Base
from skrift.db.models.user import User
from skrift.db.models.worker import WorkerQueueRecord
from skrift.workers import SQLAlchemyQueue
from skrift.workers.models import JobEnvelope

SCHEMA = "runhacks"
POSTGRES_URL = "postgresql+asyncpg://user:pass@localhost/skrift"


@pytest.fixture
def probe_tables():
    """Tables an app would define; removed from the shared metadata afterwards."""
    previous_schema = Base.metadata.schema
    tables: list[Table] = []
    yield tables
    for table in tables:
        Base.metadata.remove(table)
    Base.metadata.schema = previous_schema


def _define_app_model(tables: list[Table]) -> Table:
    table = Table(
        "schema_fk_probe",
        Base.metadata,
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, ForeignKey("users.id", ondelete="CASCADE")),
    )
    tables.append(table)
    return table


def _user_fk_target(table: Table):
    (foreign_key,) = table.c.user_id.foreign_keys
    return foreign_key.column


def test_create_app_with_schema_resolves_app_foreign_keys_to_builtin_tables(probe_tables):
    settings = Settings(
        secret_key="test-secret-key",
        db=DatabaseConfig(url=POSTGRES_URL, schema=SCHEMA),
    )

    def load_controllers():
        # App models are imported while create_app loads the controllers.
        _define_app_model(probe_tables)
        return []

    with (
        patch.object(asgi, "get_settings", return_value=settings),
        patch("skrift.config.get_settings", return_value=settings),
        patch.object(asgi, "load_controllers", load_controllers),
    ):
        asgi.create_app()

    assert _user_fk_target(probe_tables[0]) is User.__table__.c.id


def test_setup_app_with_schema_resolves_app_foreign_keys_to_builtin_tables(probe_tables):
    with (
        patch("skrift.setup.state.get_database_url_from_yaml", return_value=POSTGRES_URL),
        patch("skrift.setup.state.get_database_schema_from_yaml", return_value=SCHEMA),
    ):
        asgi.create_setup_app()

    table = _define_app_model(probe_tables)

    assert _user_fk_target(table) is User.__table__.c.id


def test_setup_engine_with_schema_leaves_metadata_unqualified(probe_tables):
    from skrift.setup.state import create_setup_engine

    with patch("skrift.setup.state.get_database_schema_from_yaml", return_value=SCHEMA):
        engine = create_setup_engine(POSTGRES_URL)

    assert engine.get_execution_options()["schema_translate_map"] == {None: SCHEMA}
    table = _define_app_model(probe_tables)
    assert _user_fk_target(table) is User.__table__.c.id


def test_schema_translate_map_routes_builtin_and_app_tables_to_the_schema(
    tmp_path, probe_tables
):
    engine_config = asgi._build_database_engine_config(
        DatabaseConfig(url=POSTGRES_URL, schema=SCHEMA)
    )
    # SQLite has no CREATE SCHEMA, but an attached database is addressed the
    # same way (``runhacks.users``), so the translate map can be exercised.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'main.db'}",
        execution_options=engine_config.execution_options,
    )

    @event.listens_for(engine, "connect")
    def attach_schema(dbapi_connection, _record):
        dbapi_connection.execute(f"ATTACH DATABASE '{tmp_path / 'schema.db'}' AS {SCHEMA}")

    probe = _define_app_model(probe_tables)
    users = User.__table__
    Base.metadata.create_all(engine, tables=[users, probe])

    user_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(users).values(id=user_id, name="Ada", email="ada@example.com")
        )
        connection.execute(insert(probe).values(id=user_id, user_id=user_id))
        assert connection.execute(select(probe.c.user_id)).scalar_one() == user_id

    inspector = inspect(engine)
    assert {"users", "schema_fk_probe"} <= set(inspector.get_table_names(schema=SCHEMA))
    assert inspector.get_table_names() == []
    engine.dispose()


def test_cli_engine_config_carries_the_schema_translate_map():
    settings = SimpleNamespace(db=DatabaseConfig(url=POSTGRES_URL, schema=SCHEMA))

    for config in (_build_db_config(settings), _build_db_config(settings, echo=False)):
        assert config.engine_config.execution_options == {
            "schema_translate_map": {None: SCHEMA}
        }


async def test_cli_engine_routes_worker_tables_to_the_schema(tmp_path):
    settings = SimpleNamespace(
        db=DatabaseConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'main.db'}", schema=SCHEMA)
    )
    db_config = _build_db_config(settings)
    engine = db_config.get_engine()

    @event.listens_for(engine.sync_engine, "connect")
    def attach_schema(dbapi_connection, _record):
        dbapi_connection.execute(f"ATTACH DATABASE '{tmp_path / 'schema.db'}' AS {SCHEMA}")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all, tables=[WorkerQueueRecord.__table__]
            )

        # `skrift workers run` hands this session factory to the worker backends.
        queue = SQLAlchemyQueue(session_maker=db_config.get_session)
        job = await queue.submit(JobEnvelope(type="schema_probe"))
        claimed = await queue.claim(["default"], visibility_timeout=60)
        assert claimed is not None and claimed.job.id == job.id

        def table_names(connection):
            inspector = inspect(connection)
            return inspector.get_table_names(schema=SCHEMA), inspector.get_table_names()

        async with engine.connect() as connection:
            in_schema, in_main = await connection.run_sync(table_names)
        assert in_schema == ["worker_queue"]
        assert in_main == []
    finally:
        await engine.dispose()
