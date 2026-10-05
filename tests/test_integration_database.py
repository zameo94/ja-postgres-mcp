"""Integration tests against a real PostgreSQL.

A throwaway PostgreSQL 15 is provisioned automatically by testcontainers for
the test session, so these tests always run (Docker is required).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import psycopg
import pytest
from mcp import Client
from psycopg.pq import TransactionStatus

from ja_pst_mcp.config import DatabaseSettings, QuerySettings, ServerSettings, Settings
from ja_pst_mcp.database import (
    Database,
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
)
from ja_pst_mcp.server import create_server

pytestmark = pytest.mark.integration

PROBE_TABLE = "ja_pst_probe"
PROBE_VIEW = "ja_pst_probe_view"

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
def db_settings(postgres_container) -> DatabaseSettings:
    return DatabaseSettings(
        host=postgres_container.get_container_host_ip(),
        port=postgres_container.get_exposed_port(5432),
        name=postgres_container.dbname,
        user=postgres_container.username,
        password=postgres_container.password,
    )


@pytest.fixture
async def database(db_settings: DatabaseSettings) -> AsyncIterator[Database]:
    database = Database(db_settings, QuerySettings())
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


async def _server_cursor_counts(database: Database, count: int) -> list[int]:
    async def one() -> int:
        async with database.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT count(*) FROM pg_cursors WHERE name LIKE 'ja_pst_%'"
                )
                row = await cursor.fetchone()
                assert row is not None
                return row[0]

    return await asyncio.gather(*[one() for _ in range(count)])


@pytest.fixture
async def probe_table(db_settings: DatabaseSettings) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        await connection.execute(f"CREATE TABLE {PROBE_TABLE} (id int, note text)")
        await connection.execute(f"INSERT INTO {PROBE_TABLE} VALUES (1, 'original')")
        await connection.commit()
        yield PROBE_TABLE
    finally:
        try:
            await connection.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
            await connection.commit()
        finally:
            await connection.close()


@pytest.fixture
async def probe_view(
    db_settings: DatabaseSettings, probe_table: str
) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(
            f"CREATE OR REPLACE VIEW {PROBE_VIEW} AS "
            f"SELECT id, note FROM {probe_table}"
        )
        await connection.commit()
        yield PROBE_VIEW
    finally:
        try:
            await connection.execute(f"DROP VIEW IF EXISTS {PROBE_VIEW}")
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
    db_settings: DatabaseSettings,
) -> None:
    database = Database(db_settings, QuerySettings(max_rows=5))
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


async def test_concurrent_queries_are_isolated(db_settings: DatabaseSettings) -> None:
    database = Database(db_settings, QuerySettings(max_rows=10))
    await database.open()
    try:
        results = await asyncio.gather(
            *[database.fetch_rows("SELECT %s::int AS n", (i,)) for i in range(8)]
        )
    finally:
        await database.close()

    assert [result.rows for result in results] == [((i,),) for i in range(8)]


async def test_concurrent_queries_have_independent_timeouts(
    db_settings: DatabaseSettings,
) -> None:
    slow = Database(db_settings, QuerySettings(statement_timeout_seconds=1))
    fast = Database(db_settings, QuerySettings(statement_timeout_seconds=10))
    await slow.open()
    await fast.open()
    try:
        slow_task = asyncio.create_task(slow.fetch_rows("SELECT pg_sleep(3)"))
        fast_result = await fast.fetch_rows("SELECT 1 AS n")
        with pytest.raises(DatabaseError):
            await slow_task
    finally:
        await slow.close()
        await fast.close()

    assert fast_result.rows == ((1,),)


async def test_concurrent_queries_leave_pool_clean(
    db_settings: DatabaseSettings,
) -> None:
    database = Database(db_settings, QuerySettings(max_rows=5))
    await database.open()
    try:
        await asyncio.gather(
            *[
                database.fetch_rows("SELECT generate_series(1, 50) AS n")
                for _ in range(6)
            ]
        )

        assert await _server_cursor_counts(database, 5) == [0, 0, 0, 0, 0]

        reused = await database.fetch_rows("SELECT 1 AS n")
        assert reused.rows == ((1,),)
        assert await _transaction_status(database) == TransactionStatus.IDLE
    finally:
        await database.close()


async def test_fetch_rows_rejects_multiple_statements(database: Database) -> None:
    with pytest.raises(InvalidQueryError, match="only a single statement is allowed"):
        await database.fetch_rows("SELECT 1; SELECT 2")


async def test_fetch_rows_caps_rows(db_settings: DatabaseSettings) -> None:
    database = Database(db_settings, QuerySettings(max_rows=5))
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


async def test_db_list_schemas_tool_end_to_end(db_settings: DatabaseSettings) -> None:
    settings = Settings(
        database=db_settings,
        server=ServerSettings(),
        query=QuerySettings(),
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_schemas", {})

    assert result.is_error is False
    names = [schema["name"] for schema in result.structured_content["schemas"]]
    assert "public" in names
    assert "information_schema" not in names
    assert not any(name.startswith("pg_") for name in names)
    assert result.structured_content["truncated"] is False


async def test_db_list_schemas_tool_with_allowlist(db_settings: DatabaseSettings) -> None:
    settings = Settings(
        database=db_settings,
        server=ServerSettings(),
        query=QuerySettings(allowed_schemas=("public",)),
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_schemas", {})

    names = [schema["name"] for schema in result.structured_content["schemas"]]
    assert names == ["public"]


@pytest.fixture
async def limited_settings(db_settings: DatabaseSettings) -> AsyncIterator[DatabaseSettings]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS ja_pst_sales CASCADE")
        await connection.execute("DROP SCHEMA IF EXISTS ja_pst_hidden CASCADE")
        await connection.execute("DROP ROLE IF EXISTS ja_pst_limited")
        await connection.execute("CREATE SCHEMA ja_pst_sales")
        await connection.execute("CREATE SCHEMA ja_pst_hidden")
        await connection.execute("CREATE ROLE ja_pst_limited LOGIN PASSWORD 'limited'")
        await connection.execute("GRANT USAGE ON SCHEMA ja_pst_sales TO ja_pst_limited")
        await connection.commit()
        yield DatabaseSettings(
            host=db_settings.host,
            port=db_settings.port,
            name=db_settings.name,
            user="ja_pst_limited",
            password="limited",
        )
    finally:
        await connection.execute("DROP SCHEMA IF EXISTS ja_pst_sales CASCADE")
        await connection.execute("DROP SCHEMA IF EXISTS ja_pst_hidden CASCADE")
        await connection.execute("DROP ROLE IF EXISTS ja_pst_limited")
        await connection.commit()
        await connection.close()


async def test_db_list_schemas_respects_least_privilege(
    limited_settings: DatabaseSettings,
) -> None:
    settings = Settings(
        database=limited_settings,
        server=ServerSettings(),
        query=QuerySettings(),
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_schemas", {})

    names = [schema["name"] for schema in result.structured_content["schemas"]]
    assert "ja_pst_sales" in names
    assert "ja_pst_hidden" not in names


async def test_db_list_tables_tool_end_to_end(
    db_settings: DatabaseSettings, probe_table: str
) -> None:
    settings = Settings(
        database=db_settings, server=ServerSettings(), query=QuerySettings()
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_list_tables", {"schema": "public", "kind": "table"}
        )

    tables = result.structured_content["tables"]
    assert any(table["name"] == probe_table for table in tables)
    assert all(
        table["schema_name"] == "public" and table["kind"] == "table" for table in tables
    )
    assert result.structured_content["truncated"] is False


async def test_db_list_tables_filters_by_kind(
    db_settings: DatabaseSettings, probe_table: str, probe_view: str
) -> None:
    settings = Settings(
        database=db_settings, server=ServerSettings(), query=QuerySettings()
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        tables_result = await client.call_tool(
            "db_list_tables", {"schema": "public", "kind": "table"}
        )
        views_result = await client.call_tool(
            "db_list_tables", {"schema": "public", "kind": "view"}
        )

    table_names = [table["name"] for table in tables_result.structured_content["tables"]]
    view_names = [table["name"] for table in views_result.structured_content["tables"]]
    assert probe_table in table_names
    assert probe_view not in table_names
    assert probe_view in view_names
    assert probe_table not in view_names


async def test_db_list_tables_excludes_system_schemas(
    db_settings: DatabaseSettings, probe_table: str
) -> None:
    settings = Settings(
        database=db_settings, server=ServerSettings(), query=QuerySettings()
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {})

    tables = result.structured_content["tables"]
    assert any(table["name"] == probe_table for table in tables)
    schemas = {table["schema_name"] for table in tables}
    assert "information_schema" not in schemas
    assert not any(schema.startswith("pg_") for schema in schemas)


async def test_db_list_tables_respects_allowlist(
    db_settings: DatabaseSettings, probe_table: str
) -> None:
    settings = Settings(
        database=db_settings,
        server=ServerSettings(),
        query=QuerySettings(allowed_schemas=("public",)),
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {})

    tables = result.structured_content["tables"]
    assert any(table["name"] == probe_table for table in tables)
    assert all(table["schema_name"] == "public" for table in tables)


async def test_db_run_read_only_query_reports_multiple_statements(
    db_settings: DatabaseSettings,
) -> None:
    settings = Settings(
        database=db_settings,
        server=ServerSettings(),
        query=QuerySettings(),
    )
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_run_read_only_query", {"sql": "SELECT 1; SELECT 2"}
        )

    assert result.is_error is True
    assert "only a single statement is allowed" in result.content[0].text


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
