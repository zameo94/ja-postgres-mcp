"""Integration tests against a real PostgreSQL (opt-in via ``JA_PST_DB_*``).

Skipped unless the database variables are present in the process environment,
so a plain ``pytest`` run never touches a real database by accident.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from ja_pst_mcp.config import DatabaseSettings, QuerySettings, Settings, load_settings
from ja_pst_mcp.database import Database, DatabaseConnectionError, DatabaseError

pytestmark = pytest.mark.integration

PROBE_TABLE = "ja_pst_probe"

# Statements that must be impossible through the tool.
WRITE_AND_DDL_STATEMENTS = [
    "INSERT INTO ja_pst_probe (id) VALUES (2)",
    "UPDATE ja_pst_probe SET note = 'hacked'",
    "DELETE FROM ja_pst_probe",
    "TRUNCATE ja_pst_probe",
    "ALTER TABLE ja_pst_probe ADD COLUMN extra int",
    "DROP TABLE ja_pst_probe",
    "CREATE TABLE ja_pst_should_not_exist (id int)",
    "CREATE SCHEMA ja_pst_should_not_exist",
    "DROP SCHEMA ja_pst_should_not_exist",
    "GRANT SELECT ON ja_pst_probe TO PUBLIC",
    "REVOKE SELECT ON ja_pst_probe FROM PUBLIC",
    "CALL ja_pst_noop()",
]

# Valid cursor queries that attempt a write; only READ ONLY stops these.
DATA_MODIFYING_CTES = [
    "WITH x AS (INSERT INTO ja_pst_probe (id) VALUES (2) RETURNING id) SELECT * FROM x",
    "WITH x AS (UPDATE ja_pst_probe SET note = 'hacked' RETURNING id) SELECT * FROM x",
    "WITH x AS (DELETE FROM ja_pst_probe RETURNING id) SELECT * FROM x",
]


@pytest.fixture
def db_settings() -> Settings:
    if "JA_PST_DB_HOST" not in os.environ:
        pytest.skip("set JA_PST_DB_* in the environment to run integration tests")
    return load_settings(os.environ)


@pytest.fixture
async def database(db_settings: Settings) -> AsyncIterator[Database]:
    database = Database(db_settings.database, db_settings.query)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


async def _connect_writable(settings: DatabaseSettings) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.name,
        user=settings.user,
        password=settings.password,
    )


async def _server_cursor_count(database: Database) -> int:
    async with database.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM pg_cursors WHERE name LIKE 'ja_pst_%'"
            )
            row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _transaction_status(database: Database) -> TransactionStatus:
    async with database.connection() as connection:
        return connection.pgconn.transaction_status


@pytest.fixture
async def probe_table(db_settings: Settings) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings.database)
    try:
        try:
            await connection.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
            await connection.execute(f"CREATE TABLE {PROBE_TABLE} (id int, note text)")
            await connection.execute(
                f"INSERT INTO {PROBE_TABLE} VALUES (1, 'original')"
            )
            await connection.commit()
        except psycopg.Error:
            await connection.rollback()
            pytest.skip("integration role cannot create a probe table")
        yield PROBE_TABLE
    finally:
        try:
            await connection.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
            await connection.commit()
        finally:
            await connection.close()


async def test_ping_against_real_postgres(database: Database) -> None:
    await database.ping()


async def test_connection_runs_a_query(database: Database) -> None:
    async with database.connection() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            row = await cursor.fetchone()

    assert row == (1,)


async def test_fetch_rows_reads_and_serializes(database: Database) -> None:
    result = await database.fetch_rows("SELECT 1 AS n, 'x' AS label")

    assert result.columns == ("n", "label")
    assert result.rows == ((1, "x"),)
    assert result.row_count == 1
    assert result.truncated is False


async def test_fetch_rows_keeps_duplicate_columns(database: Database) -> None:
    result = await database.fetch_rows("SELECT 1 AS id, 2 AS id")

    assert result.columns == ("id", "id")
    assert result.rows == ((1, 2),)


async def test_fetch_rows_allows_semicolons_in_literals(database: Database) -> None:
    result = await database.fetch_rows("SELECT ';' AS sep, $$a;b$$ AS dq")

    assert result.rows == ((";", "a;b"),)


async def test_fetch_rows_empty_result_keeps_columns(database: Database) -> None:
    result = await database.fetch_rows(
        "SELECT 1 AS id, 'x'::text AS name WHERE false"
    )

    assert result.columns == ("id", "name")
    assert result.rows == ()
    assert result.row_count == 0
    assert result.truncated is False


async def test_fetch_rows_positional_params(database: Database) -> None:
    result = await database.fetch_rows("SELECT %s::int AS n", (7,))

    assert result.rows == ((7,),)


async def test_fetch_rows_named_params(database: Database) -> None:
    result = await database.fetch_rows("SELECT %(value)s::int AS n", {"value": 7})

    assert result.rows == ((7,),)


async def test_no_residual_server_cursor_after_query(database: Database) -> None:
    await database.fetch_rows("SELECT generate_series(1, 3) AS n")

    assert await _server_cursor_count(database) == 0


async def test_no_residual_server_cursor_after_truncation(
    db_settings: Settings,
) -> None:
    database = Database(db_settings.database, QuerySettings(max_rows=5))
    await database.open()
    try:
        result = await database.fetch_rows("SELECT generate_series(1, 10) AS n")

        assert result.truncated is True
        assert await _server_cursor_count(database) == 0
        assert await _transaction_status(database) == TransactionStatus.IDLE
    finally:
        await database.close()


async def test_no_residual_server_cursor_after_error(database: Database) -> None:
    with pytest.raises(DatabaseError):
        await database.fetch_rows("SELECT 1 / 0")

    assert await _server_cursor_count(database) == 0
    assert await _transaction_status(database) == TransactionStatus.IDLE


async def test_pooled_connection_reuse_has_no_side_effects(database: Database) -> None:
    first = await database.fetch_rows("SELECT 1 AS n")
    second = await database.fetch_rows("SELECT 2 AS n")

    assert first.rows == ((1,),)
    assert second.rows == ((2,),)
    assert await _transaction_status(database) == TransactionStatus.IDLE


async def test_fetch_rows_rejects_multiple_statements(database: Database) -> None:
    with pytest.raises(DatabaseError):
        await database.fetch_rows("SELECT 1; SELECT 2")


async def test_fetch_rows_caps_rows(db_settings: Settings) -> None:
    database = Database(db_settings.database, QuerySettings(max_rows=5))
    await database.open()
    try:
        result = await database.fetch_rows("SELECT generate_series(1, 10) AS n")
    finally:
        await database.close()

    assert len(result.rows) == 5
    assert result.row_count == 5
    assert result.truncated is True


async def test_fetch_rows_recovers_after_failed_statement(database: Database) -> None:
    with pytest.raises(DatabaseError):
        await database.fetch_rows("SELECT 1 / 0")

    result = await database.fetch_rows("SELECT 1 AS n")

    assert result.rows == ((1,),)


async def test_fetch_rows_rejects_statement_without_result_set(
    database: Database,
) -> None:
    with pytest.raises(DatabaseError):
        await database.fetch_rows("SET LOCAL statement_timeout = 1000")


@pytest.mark.parametrize("statement", WRITE_AND_DDL_STATEMENTS + DATA_MODIFYING_CTES)
async def test_write_operations_are_impossible(
    database: Database, probe_table: str, statement: str
) -> None:
    with pytest.raises(DatabaseError):
        await database.fetch_rows(statement)


async def test_write_attempts_leave_data_unchanged(
    database: Database, probe_table: str
) -> None:
    result = await database.fetch_rows(f"SELECT id, note FROM {probe_table}")

    assert result.rows == ((1, "original"),)


async def test_params_are_not_interpolated(database: Database, probe_table: str) -> None:
    payload = "1; DROP TABLE ja_pst_probe"
    result = await database.fetch_rows(
        "SELECT %(value)s AS v", {"value": payload}
    )

    assert result.rows == ((payload,),)


async def test_open_wraps_unreachable_database() -> None:
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=1,
        name="nope",
        user="nope",
        password="nope",
        connect_timeout_seconds=2,
    )
    database = Database(settings)

    with pytest.raises(DatabaseConnectionError):
        await database.open()
