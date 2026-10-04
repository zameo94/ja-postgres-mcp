"""Integration tests against a real PostgreSQL (opt-in via ``JA_PST_DB_*``).

Skipped unless the database variables are present in the process environment,
so a plain ``pytest`` run never touches a real database by accident.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from ja_pst_mcp.config import DatabaseSettings, load_settings
from ja_pst_mcp.database import Database, DatabaseConnectionError, DatabaseError

pytestmark = pytest.mark.integration


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    if "JA_PST_DB_HOST" not in os.environ:
        pytest.skip("set JA_PST_DB_* in the environment to run integration tests")
    settings = load_settings(os.environ)
    database = Database(settings.database)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


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
    assert result.rows == ({"n": 1, "label": "x"},)
    assert result.truncated is False


async def test_fetch_rows_caps_rows(database: Database) -> None:
    result = await database.fetch_rows("SELECT generate_series(1, 201) AS n")

    assert len(result.rows) == 200
    assert result.truncated is True


async def test_fetch_rows_rejects_writes(database: Database) -> None:
    with pytest.raises(DatabaseError):
        await database.fetch_rows("CREATE TABLE mvp_read_only_guard (id int)")


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
