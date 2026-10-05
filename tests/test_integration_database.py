"""Integration tests against a real PostgreSQL.

A throwaway PostgreSQL 15 is provisioned automatically by testcontainers for
the test session, so these tests always run (Docker is required).
"""

from __future__ import annotations

import asyncio
import pathlib
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
PROBE_MATVIEW = "ja_pst_probe_mv"
PROBE_PARTITIONED = "ja_pst_probe_part"
PROBE_PARTITION = "ja_pst_probe_part_p1"

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
            await cursor.execute("SELECT count(*) FROM pg_cursors WHERE name LIKE 'ja_pst_%'")
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
                await cursor.execute("SELECT count(*) FROM pg_cursors WHERE name LIKE 'ja_pst_%'")
                row = await cursor.fetchone()
                assert row is not None
                return row[0]

    return await asyncio.gather(*[one() for _ in range(count)])


@pytest.fixture
async def probe_table(db_settings: DatabaseSettings) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(f"DROP TABLE IF EXISTS {PROBE_TABLE}")
        await connection.execute(f"CREATE TABLE {PROBE_TABLE} (id int PRIMARY KEY, note text)")
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
async def probe_view(db_settings: DatabaseSettings, probe_table: str) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(
            f"CREATE OR REPLACE VIEW {PROBE_VIEW} AS SELECT id, note FROM {probe_table}"
        )
        await connection.commit()
        yield PROBE_VIEW
    finally:
        try:
            await connection.execute(f"DROP VIEW IF EXISTS {PROBE_VIEW}")
            await connection.commit()
        finally:
            await connection.close()


@pytest.fixture
async def probe_matview(db_settings: DatabaseSettings, probe_table: str) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(
            f"CREATE MATERIALIZED VIEW {PROBE_MATVIEW} AS SELECT id, note FROM {probe_table}"
        )
        await connection.commit()
        yield PROBE_MATVIEW
    finally:
        try:
            await connection.execute(f"DROP MATERIALIZED VIEW IF EXISTS {PROBE_MATVIEW}")
            await connection.commit()
        finally:
            await connection.close()


@pytest.fixture
async def probe_partitioned(db_settings: DatabaseSettings) -> AsyncIterator[str]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(f"DROP TABLE IF EXISTS {PROBE_PARTITIONED} CASCADE")
        await connection.execute(
            f"CREATE TABLE {PROBE_PARTITIONED} (id int, note text) PARTITION BY RANGE (id)"
        )
        await connection.execute(
            f"CREATE TABLE {PROBE_PARTITION} PARTITION OF {PROBE_PARTITIONED} "
            "FOR VALUES FROM (0) TO (100)"
        )
        await connection.commit()
        yield PROBE_PARTITIONED
    finally:
        try:
            await connection.execute(f"DROP TABLE IF EXISTS {PROBE_PARTITIONED} CASCADE")
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
    result = await database.fetch_rows("SELECT 1 AS id, 'x'::text AS name WHERE false")

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
            *[database.fetch_rows("SELECT generate_series(1, 50) AS n") for _ in range(6)]
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


async def test_write_attempts_leave_data_unchanged(database: Database, probe_table: str) -> None:
    result = await database.fetch_rows(f"SELECT id, note FROM {probe_table}")

    assert result.rows == ((1, "original"),)


async def test_params_are_not_interpolated(database: Database, probe_table: str) -> None:
    payload = "1; DROP TABLE ja_pst_probe"
    result = await database.fetch_rows("SELECT %(value)s AS v", {"value": payload})

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
    assert result.structured_content["next_cursor"] is None
    assert result.structured_content["row_count"] == len(names)


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


async def test_db_list_schemas_traverses_pages(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    created_schemas = [f"ja_pst_page_{index}" for index in range(5)]
    try:
        for schema in created_schemas:
            await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.commit()

        settings = Settings(
            database=db_settings,
            server=ServerSettings(),
            query=QuerySettings(discovery_page_size=2, discovery_max_page_size=10),
        )
        server = create_server(settings)

        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        async with Client(server, raise_exceptions=True) as client:
            while True:
                arguments: dict[str, object] = {"page_size": 2}
                if cursor is not None:
                    arguments["cursor"] = cursor
                result = await client.call_tool("db_list_schemas", arguments)
                content = result.structured_content
                assert len(content["schemas"]) <= 2
                seen.extend(schema["name"] for schema in content["schemas"])
                pages += 1
                cursor = content["next_cursor"]
                if cursor is None:
                    break

        assert pages >= 3
        assert set(created_schemas).issubset(set(seen))
        assert len(seen) == len(set(seen))
    finally:
        for schema in created_schemas:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.commit()
        await connection.close()


@pytest.fixture
async def limited_settings(db_settings: DatabaseSettings) -> AsyncIterator[DatabaseSettings]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS ja_pst_sales CASCADE")
        await connection.execute("DROP SCHEMA IF EXISTS ja_pst_hidden CASCADE")
        await connection.execute("DROP ROLE IF EXISTS ja_pst_limited")
        await connection.execute("CREATE SCHEMA ja_pst_sales")
        await connection.execute("CREATE SCHEMA ja_pst_hidden")
        await connection.execute("CREATE TABLE ja_pst_sales.items (id int, label text)")
        await connection.execute("INSERT INTO ja_pst_sales.items VALUES (1, 'a')")
        await connection.execute("CREATE ROLE ja_pst_limited LOGIN PASSWORD 'limited'")
        await connection.execute("GRANT USAGE ON SCHEMA ja_pst_sales TO ja_pst_limited")
        await connection.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA ja_pst_sales TO ja_pst_limited"
        )
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


async def test_least_privilege_role_can_read(
    limited_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(limited_settings)
    try:
        cursor = await connection.execute("SELECT id, label FROM ja_pst_sales.items")
        assert await cursor.fetchall() == [(1, "a")]
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO ja_pst_sales.items (id) VALUES (2)",
        "UPDATE ja_pst_sales.items SET label = 'x'",
        "DELETE FROM ja_pst_sales.items",
        "CREATE TABLE ja_pst_sales.new_table (id int)",
        "DROP TABLE ja_pst_sales.items",
        "CREATE TABLE public.should_not_exist (id int)",
    ],
)
async def test_least_privilege_role_cannot_write(
    limited_settings: DatabaseSettings, statement: str
) -> None:
    connection = await _connect_writable(limited_settings)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await connection.execute(statement)
        await connection.rollback()
    finally:
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
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {"schema": "public", "kind": "table"})

    tables = result.structured_content["tables"]
    assert any(table["name"] == probe_table for table in tables)
    assert all(table["schema_name"] == "public" and table["kind"] == "table" for table in tables)
    assert result.structured_content["next_cursor"] is None


async def test_db_list_tables_traverses_pages(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    created_tables = [f"ja_pst_page_{index}" for index in range(5)]
    try:
        for table in created_tables:
            await connection.execute(f"CREATE TABLE {table} (id int)")
        await connection.commit()

        settings = Settings(
            database=db_settings,
            server=ServerSettings(),
            query=QuerySettings(discovery_page_size=2, discovery_max_page_size=10),
        )
        server = create_server(settings)

        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        async with Client(server, raise_exceptions=True) as client:
            while True:
                arguments: dict[str, object] = {"schema": "public", "page_size": 2}
                if cursor is not None:
                    arguments["cursor"] = cursor
                result = await client.call_tool("db_list_tables", arguments)
                content = result.structured_content
                assert len(content["tables"]) <= 2
                seen.extend(table["name"] for table in content["tables"])
                pages += 1
                cursor = content["next_cursor"]
                if cursor is None:
                    break

        assert pages >= 3
        assert set(created_tables).issubset(set(seen))
        assert len(seen) == len(set(seen))
    finally:
        for table in created_tables:
            await connection.execute(f"DROP TABLE IF EXISTS {table}")
        await connection.commit()
        await connection.close()


async def test_db_list_tables_rejects_cursor_from_other_scope(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("CREATE TABLE ja_pst_scope_a (id int)")
        await connection.execute("CREATE TABLE ja_pst_scope_b (id int)")
        await connection.commit()

        settings = Settings(
            database=db_settings,
            server=ServerSettings(),
            query=QuerySettings(discovery_page_size=1, discovery_max_page_size=10),
        )
        server = create_server(settings)

        async with Client(server, raise_exceptions=True) as client:
            first = await client.call_tool("db_list_tables", {"schema": "public", "page_size": 1})
            cursor = first.structured_content["next_cursor"]
            assert cursor is not None
            mismatch = await client.call_tool(
                "db_list_tables",
                {"schema": "information_schema", "page_size": 1, "cursor": cursor},
            )

        assert mismatch.is_error is True
        assert "does not match" in mismatch.content[0].text
    finally:
        await connection.execute("DROP TABLE IF EXISTS ja_pst_scope_a")
        await connection.execute("DROP TABLE IF EXISTS ja_pst_scope_b")
        await connection.commit()
        await connection.close()


async def test_db_list_tables_filters_by_kind(
    db_settings: DatabaseSettings, probe_table: str, probe_view: str
) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
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


async def test_db_describe_table_tool_end_to_end(
    db_settings: DatabaseSettings, probe_table: str
) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_describe_table", {"table": probe_table, "schema": "public"}
        )

    content = result.structured_content
    assert content["schema_name"] == "public"
    assert content["name"] == probe_table
    assert content["kind"] == "table"
    assert [column["name"] for column in content["columns"]] == ["id", "note"]
    assert content["primary_key"] == ["id"]
    id_column = content["columns"][0]
    assert id_column["data_type"] == "integer"
    assert id_column["nullable"] is False
    assert id_column["is_primary_key"] is True


async def test_db_describe_table_resolves_schema(
    db_settings: DatabaseSettings, probe_table: str
) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": probe_table})

    assert result.structured_content["schema_name"] == "public"


async def test_db_describe_table_reports_missing_table(
    db_settings: DatabaseSettings,
) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "ja_pst_does_not_exist"})

    assert result.is_error is True
    assert "not found" in result.content[0].text


async def test_db_describe_table_view(db_settings: DatabaseSettings, probe_view: str) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_describe_table", {"table": probe_view, "schema": "public"}
        )

    content = result.structured_content
    assert content["kind"] == "view"
    assert [column["name"] for column in content["columns"]] == ["id", "note"]
    assert content["primary_key"] == []


async def test_db_describe_table_matview(db_settings: DatabaseSettings, probe_matview: str) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_describe_table", {"table": probe_matview, "schema": "public"}
        )

    content = result.structured_content
    assert content["kind"] == "matview"
    assert [column["name"] for column in content["columns"]] == ["id", "note"]


async def test_db_describe_table_partitioned(
    db_settings: DatabaseSettings, probe_partitioned: str
) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
    server = create_server(settings)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_describe_table", {"table": probe_partitioned, "schema": "public"}
        )

    content = result.structured_content
    assert content["kind"] == "table"
    assert [column["name"] for column in content["columns"]] == ["id", "note"]


async def test_db_list_tables_excludes_system_schemas(
    db_settings: DatabaseSettings, probe_table: str
) -> None:
    settings = Settings(database=db_settings, server=ServerSettings(), query=QuerySettings())
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
        result = await client.call_tool("db_run_read_only_query", {"sql": "SELECT 1; SELECT 2"})

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


@pytest.fixture
async def probe_graph(db_settings: DatabaseSettings) -> AsyncIterator[None]:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("DROP VIEW IF EXISTS ja_pst_child_view")
        await connection.execute("DROP TABLE IF EXISTS ja_pst_child CASCADE")
        await connection.execute("DROP TABLE IF EXISTS ja_pst_parent CASCADE")
        await connection.execute(
            "CREATE TABLE ja_pst_parent (id int PRIMARY KEY, label text UNIQUE)"
        )
        await connection.execute(
            "CREATE TABLE ja_pst_child (id int PRIMARY KEY, "
            "parent_id int REFERENCES ja_pst_parent(id), note text CHECK (note <> ''))"
        )
        await connection.execute("CREATE INDEX ja_pst_child_note_idx ON ja_pst_child (note)")
        await connection.execute(
            "CREATE VIEW ja_pst_child_view AS SELECT id, note FROM ja_pst_child"
        )
        await connection.commit()
        yield
    finally:
        await connection.execute("DROP VIEW IF EXISTS ja_pst_child_view")
        await connection.execute("DROP TABLE IF EXISTS ja_pst_child CASCADE")
        await connection.execute("DROP TABLE IF EXISTS ja_pst_parent CASCADE")
        await connection.commit()
        await connection.close()


def _settings(db_settings: DatabaseSettings, **query: object) -> Settings:
    return Settings(
        database=db_settings,
        server=ServerSettings(),
        query=QuerySettings(**query),
    )


async def test_db_list_constraints_tool(db_settings: DatabaseSettings, probe_graph: None) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_list_constraints", {"schema": "public", "table": "ja_pst_child"}
        )

    kinds = {constraint["kind"] for constraint in result.structured_content["constraints"]}
    assert {"primary_key", "foreign_key", "check"}.issubset(kinds)


async def test_db_list_constraints_reports_unique(
    db_settings: DatabaseSettings, probe_graph: None
) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_constraints", {"table": "ja_pst_parent"})

    kinds = {constraint["kind"] for constraint in result.structured_content["constraints"]}
    assert "unique" in kinds
    assert "primary_key" in kinds


async def test_db_list_constraints_skips_partition_clones(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("DROP TABLE IF EXISTS ja_pst_part CASCADE")
        await connection.execute(
            "CREATE TABLE ja_pst_part (id int PRIMARY KEY, note text) PARTITION BY RANGE (id)"
        )
        await connection.execute(
            "CREATE TABLE ja_pst_part_p1 PARTITION OF ja_pst_part FOR VALUES FROM (0) TO (100)"
        )
        await connection.execute(
            "CREATE TABLE ja_pst_part_p2 PARTITION OF ja_pst_part FOR VALUES FROM (100) TO (200)"
        )
        await connection.commit()

        server = create_server(_settings(db_settings))

        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("db_list_constraints", {})

        tables = {item["table_name"] for item in result.structured_content["constraints"]}
        assert "ja_pst_part" in tables
        assert "ja_pst_part_p1" not in tables
        assert "ja_pst_part_p2" not in tables
    finally:
        await connection.execute("DROP TABLE IF EXISTS ja_pst_part CASCADE")
        await connection.commit()
        await connection.close()


async def test_db_list_relationships_tool(db_settings: DatabaseSettings, probe_graph: None) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_relationships", {"table": "ja_pst_child"})

    relationships = result.structured_content["relationships"]
    assert any(item["target_table"] == "ja_pst_parent" for item in relationships)


async def test_db_list_indexes_tool(db_settings: DatabaseSettings, probe_graph: None) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_indexes", {"table": "ja_pst_child"})

    indexes = result.structured_content["indexes"]
    note_index = next(item for item in indexes if item["name"] == "ja_pst_child_note_idx")
    assert note_index["columns"] == ["note"]
    assert note_index["method"] == "btree"


async def test_db_get_view_definition_tool(
    db_settings: DatabaseSettings, probe_graph: None
) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_get_view_definition",
            {"view": "ja_pst_child_view", "schema": "public"},
        )

    assert result.structured_content["kind"] == "view"
    assert "ja_pst_child" in result.structured_content["definition"]


async def test_db_preview_table_tool(db_settings: DatabaseSettings, probe_graph: None) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(
            "INSERT INTO ja_pst_child (id, parent_id, note) VALUES (1, NULL, 'a'), (2, NULL, 'b')"
        )
        await connection.commit()
    finally:
        await connection.close()

    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_preview_table", {"table": "ja_pst_child", "schema": "public"}
        )

    content = result.structured_content
    assert content["columns"] == ["id", "parent_id", "note"]
    assert content["rows"] == [[1, None, "a"], [2, None, "b"]]
    assert content["next_cursor"] is None


async def test_db_list_indexes_handles_expression_and_include(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("CREATE TABLE ja_pst_idx (a int, b text, c int)")
        await connection.execute(
            "CREATE INDEX ja_pst_idx_expr ON ja_pst_idx (a, lower(b)) INCLUDE (c)"
        )
        await connection.commit()

        server = create_server(_settings(db_settings))

        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("db_list_indexes", {"table": "ja_pst_idx"})

        index = next(
            item
            for item in result.structured_content["indexes"]
            if item["name"] == "ja_pst_idx_expr"
        )
        assert index["columns"] == ["a", "lower(b)"]
    finally:
        await connection.execute("DROP TABLE IF EXISTS ja_pst_idx")
        await connection.commit()
        await connection.close()


async def test_db_preview_table_requires_primary_key_end_to_end(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("CREATE TABLE ja_pst_no_pk (a int)")
        await connection.commit()

        server = create_server(_settings(db_settings))

        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("db_preview_table", {"table": "ja_pst_no_pk"})

        assert result.is_error is True
        assert "primary key" in result.content[0].text
    finally:
        await connection.execute("DROP TABLE IF EXISTS ja_pst_no_pk")
        await connection.commit()
        await connection.close()


async def test_db_preview_table_traverses_pages_with_bool_pk(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute("CREATE TABLE ja_pst_bool_pk (flag bool PRIMARY KEY, note text)")
        await connection.execute("INSERT INTO ja_pst_bool_pk VALUES (false, 'f'), (true, 't')")
        await connection.commit()

        server = create_server(
            _settings(db_settings, discovery_page_size=1, discovery_max_page_size=10)
        )

        seen: list[str] = []
        cursor: str | None = None
        async with Client(server, raise_exceptions=True) as client:
            while True:
                arguments: dict[str, object] = {
                    "table": "ja_pst_bool_pk",
                    "schema": "public",
                    "page_size": 1,
                }
                if cursor is not None:
                    arguments["cursor"] = cursor
                result = await client.call_tool("db_preview_table", arguments)
                content = result.structured_content
                assert content["row_count"] <= 1
                seen.extend(row[1] for row in content["rows"])
                cursor = content["next_cursor"]
                if cursor is None:
                    break

        assert sorted(seen) == ["f", "t"]
        assert len(seen) == len(set(seen))
    finally:
        await connection.execute("DROP TABLE IF EXISTS ja_pst_bool_pk")
        await connection.commit()
        await connection.close()


async def test_db_preview_table_quotes_identifiers_end_to_end(
    db_settings: DatabaseSettings,
) -> None:
    connection = await _connect_writable(db_settings)
    try:
        await connection.execute(
            'CREATE TABLE "Weird Table" ("select" int PRIMARY KEY, "a""b" text)'
        )
        await connection.execute("INSERT INTO \"Weird Table\" VALUES (1, 'x')")
        await connection.commit()

        server = create_server(_settings(db_settings))

        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "db_preview_table", {"table": "Weird Table", "schema": "public"}
            )

        content = result.structured_content
        assert content["columns"] == ["select", 'a"b']
        assert content["rows"] == [[1, "x"]]
    finally:
        await connection.execute('DROP TABLE IF EXISTS "Weird Table"')
        await connection.commit()
        await connection.close()


@pytest.fixture(scope="session")
def demo_seed(postgres_container) -> None:
    seed = pathlib.Path("db/demo/seed.sql").read_text(encoding="utf-8")
    with psycopg.connect(
        host=postgres_container.get_container_host_ip(),
        port=postgres_container.get_exposed_port(5432),
        dbname=postgres_container.dbname,
        user=postgres_container.username,
        password=postgres_container.password,
        autocommit=True,
    ) as connection:
        connection.execute(seed)


async def test_demo_seed_supports_business_query(
    db_settings: DatabaseSettings, demo_seed: None
) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_run_read_only_query",
            {"sql": "SELECT count(*) AS overdue FROM demo.orders WHERE status = 'pending'"},
        )

    assert result.is_error is False
    assert result.structured_content["rows"][0][0] == 60


async def test_demo_seed_discovery(db_settings: DatabaseSettings, demo_seed: None) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        tables = (await client.call_tool("db_list_tables", {"schema": "demo"})).structured_content[
            "tables"
        ]
        relationships = (
            await client.call_tool("db_list_relationships", {"schema": "demo"})
        ).structured_content["relationships"]

    names = {table["name"] for table in tables}
    assert {"customers", "products", "orders", "order_items", "payments"}.issubset(names)
    assert any(
        item["source_table"] == "orders" and item["target_table"] == "customers"
        for item in relationships
    )


async def test_demo_seed_view_definition(db_settings: DatabaseSettings, demo_seed: None) -> None:
    server = create_server(_settings(db_settings))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_get_view_definition", {"view": "monthly_revenue", "schema": "demo"}
        )

    assert result.structured_content["kind"] == "view"
    assert "payments" in result.structured_content["definition"]


async def test_demo_seed_prices_are_coherent(
    db_settings: DatabaseSettings, demo_seed: None
) -> None:
    server = create_server(_settings(db_settings))

    violations = [
        # The payment must equal the order total.
        """SELECT o.id
           FROM demo.orders AS o
           JOIN demo.order_items AS oi ON oi.order_id = o.id
           JOIN demo.payments AS p ON p.order_id = o.id
           GROUP BY o.id, p.amount
           HAVING sum(oi.quantity * oi.unit_price) <> p.amount""",
        # The line price must equal the catalogue price.
        """SELECT oi.id
           FROM demo.order_items AS oi
           JOIN demo.products AS p ON p.id = oi.product_id
           WHERE oi.unit_price <> p.unit_price""",
        # An order must contain more than one distinct product, otherwise the
        # generator is degenerate and order_items carries no information.
        """SELECT order_id
           FROM demo.order_items
           GROUP BY order_id
           HAVING count(DISTINCT product_id) < 2""",
    ]

    async with Client(server, raise_exceptions=True) as client:
        results = [
            await client.call_tool("db_run_read_only_query", {"sql": sql}) for sql in violations
        ]

    for sql, result in zip(violations, results, strict=True):
        assert result.is_error is False, sql
        assert result.structured_content["rows"] == [], sql
