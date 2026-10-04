"""Unit tests for the PostgreSQL access layer (no real database)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import psycopg
from psycopg import OperationalError

from ja_pst_mcp import database as database_module
from ja_pst_mcp.config import DatabaseSettings, QuerySettings
from ja_pst_mcp.database import (
    Database,
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
    build_connection_kwargs,
)

SETTINGS = DatabaseSettings(
    host="db.example",
    port=6543,
    name="japst",
    user="alice",
    password="s3cret",
    connect_timeout_seconds=7,
)


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.raise_on_execute: BaseException | None = None
        self.description: list[SimpleNamespace] | None = None
        self.rows: list[tuple[Any, ...]] = []
        self.name: Any = None

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.executed.append((query, params))

    async def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self.rows[:size]


class FakeTransaction:
    async def __aenter__(self) -> "FakeTransaction":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    def cursor(self, name: Any = None) -> FakeCursor:
        self.cursor_instance.name = name
        return self.cursor_instance

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeAsyncPool:
    def __init__(
        self,
        *,
        conninfo: str,
        kwargs: dict[str, Any],
        min_size: int,
        max_size: int,
        timeout: float,
        open: bool,
    ) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout
        self.auto_open = open
        self.open_calls: list[tuple[bool, float | None]] = []
        self.close_calls = 0
        self.raise_on_open: BaseException | None = None
        self.raise_on_connection: BaseException | None = None
        self.connection_instance = FakeConnection()

    async def open(self, wait: bool = False, timeout: float | None = None) -> None:
        if self.raise_on_open is not None:
            raise self.raise_on_open
        self.open_calls.append((wait, timeout))

    async def close(self) -> None:
        self.close_calls += 1

    @asynccontextmanager
    async def connection(self):
        if self.raise_on_connection is not None:
            raise self.raise_on_connection
        yield self.connection_instance


class PoolSpy:
    def __init__(self) -> None:
        self.pool: FakeAsyncPool | None = None

    def factory(self, **kwargs: Any) -> FakeAsyncPool:
        self.pool = FakeAsyncPool(**kwargs)
        return self.pool


@pytest.fixture
def pool_spy(monkeypatch: pytest.MonkeyPatch) -> PoolSpy:
    spy = PoolSpy()
    monkeypatch.setattr(database_module, "AsyncConnectionPool", spy.factory)
    return spy


def test_build_connection_kwargs_maps_settings() -> None:
    assert build_connection_kwargs(SETTINGS) == {
        "host": "db.example",
        "port": 6543,
        "dbname": "japst",
        "user": "alice",
        "password": "s3cret",
        "connect_timeout": 7,
        "options": "-c default_transaction_read_only=on",
    }


def test_pool_is_configured_from_settings(pool_spy: PoolSpy) -> None:
    Database(SETTINGS)

    assert pool_spy.pool is not None
    assert pool_spy.pool.kwargs == build_connection_kwargs(SETTINGS)
    assert pool_spy.pool.min_size == 1
    assert pool_spy.pool.max_size == 5
    assert pool_spy.pool.timeout == 30
    assert pool_spy.pool.auto_open is False


def test_pool_sizes_come_from_settings(pool_spy: PoolSpy) -> None:
    settings = replace(
        SETTINGS, pool_min_size=2, pool_max_size=9, pool_timeout_seconds=12
    )

    Database(settings)

    assert pool_spy.pool is not None
    assert pool_spy.pool.min_size == 2
    assert pool_spy.pool.max_size == 9
    assert pool_spy.pool.timeout == 12


def test_database_does_not_open_pool_on_construction(pool_spy: PoolSpy) -> None:
    Database(SETTINGS)

    assert pool_spy.pool is not None
    assert pool_spy.pool.open_calls == []


async def test_open_waits_with_configured_timeout(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)

    await database.open()

    assert pool_spy.pool is not None
    assert pool_spy.pool.open_calls == [(True, 7)]


async def test_open_wraps_driver_errors(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    driver_error = OperationalError('connection to server at "db.example" failed')
    pool_spy.pool.raise_on_open = driver_error

    with pytest.raises(DatabaseConnectionError) as excinfo:
        await database.open()

    assert excinfo.value.__cause__ is driver_error


async def test_open_does_not_mask_unexpected_errors(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    pool_spy.pool.raise_on_open = TypeError("a bug, not a database error")

    with pytest.raises(TypeError):
        await database.open()


async def test_close_closes_the_pool(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)

    await database.close()

    assert pool_spy.pool is not None
    assert pool_spy.pool.close_calls == 1


async def test_connection_yields_pooled_connection(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)

    async with database.connection() as connection:
        assert pool_spy.pool is not None
        assert connection is pool_spy.pool.connection_instance


async def test_ping_executes_select_one(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)

    await database.ping()

    assert pool_spy.pool is not None
    assert pool_spy.pool.connection_instance.cursor_instance.executed == [
        ("SELECT 1", None)
    ]


async def test_async_context_manager_opens_and_closes(pool_spy: PoolSpy) -> None:
    async with Database(SETTINGS):
        pass

    assert pool_spy.pool is not None
    assert pool_spy.pool.open_calls == [(True, 7)]
    assert pool_spy.pool.close_calls == 1


async def test_ping_wraps_driver_errors(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    driver_error = OperationalError(
        'connection to server at "db.example" (10.0.0.1), port 5432 failed: '
        "Connection refused"
    )
    pool_spy.pool.connection_instance.cursor_instance.raise_on_execute = driver_error

    with pytest.raises(DatabaseError) as excinfo:
        await database.ping()

    assert not isinstance(excinfo.value, DatabaseConnectionError)
    assert "database operation failed" in str(excinfo.value)
    assert "db.example" not in str(excinfo.value)
    assert "10.0.0.1" not in str(excinfo.value)
    assert excinfo.value.__cause__ is driver_error


async def test_connection_acquisition_error_is_connection_error(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    driver_error = OperationalError("couldn't get a connection after 7.00 sec")
    pool_spy.pool.raise_on_connection = driver_error

    with pytest.raises(DatabaseConnectionError) as excinfo:
        async with database.connection():
            pass

    assert "unable to acquire a database connection" in str(excinfo.value)
    assert excinfo.value.__cause__ is driver_error


async def test_connection_operation_error_is_database_error(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    driver_error = OperationalError("statement timeout")
    pool_spy.pool.connection_instance.cursor_instance.raise_on_execute = driver_error

    with pytest.raises(DatabaseError) as excinfo:
        async with database.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")

    assert not isinstance(excinfo.value, DatabaseConnectionError)
    assert "database operation failed" in str(excinfo.value)
    assert excinfo.value.__cause__ is driver_error


async def test_fetch_rows_caps_and_serializes(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS, QuerySettings(max_rows=2))
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [SimpleNamespace(name="amount")]
    connection.cursor_instance.rows = [
        (Decimal("10.50"),),
        (Decimal("20.00"),),
        (Decimal("30.00"),),
    ]

    result = await database.fetch_rows("SELECT amount FROM t")

    assert result.columns == ("amount",)
    assert result.rows == (("10.50",), ("20.00",))
    assert result.row_count == 2
    assert result.truncated is True
    assert connection.cursor_instance.executed == [("SELECT amount FROM t", None)]
    assert connection.cursor_instance.name.startswith("ja_pst_")


async def test_fetch_rows_reports_columns_for_empty_result(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [
        SimpleNamespace(name="n"),
        SimpleNamespace(name="label"),
    ]
    connection.cursor_instance.rows = []

    result = await database.fetch_rows("SELECT n, label FROM t WHERE false")

    assert result.columns == ("n", "label")
    assert result.rows == ()
    assert result.row_count == 0
    assert result.truncated is False


async def test_fetch_rows_keeps_duplicate_column_names(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [
        SimpleNamespace(name="id"),
        SimpleNamespace(name="id"),
    ]
    connection.cursor_instance.rows = [(1, 2)]

    result = await database.fetch_rows("SELECT c.id, o.id FROM c JOIN o ON true")

    assert result.columns == ("id", "id")
    assert result.rows == ((1, 2),)
    assert result.row_count == 1


async def test_fetch_rows_sets_local_timeouts_and_passes_params(
    pool_spy: PoolSpy,
) -> None:
    database = Database(
        SETTINGS,
        QuerySettings(statement_timeout_seconds=7, lock_timeout_seconds=3),
    )
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [SimpleNamespace(name="n")]
    connection.cursor_instance.rows = [(1,)]

    await database.fetch_rows("SELECT %s AS n", (1,))

    executed = dict(connection.executed)
    assert executed["SELECT set_config('statement_timeout', %s, true)"] == ("7000",)
    assert executed["SELECT set_config('lock_timeout', %s, true)"] == ("3000",)
    assert connection.cursor_instance.executed == [("SELECT %s AS n", (1,))]


async def test_fetch_rows_passes_named_params(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [SimpleNamespace(name="n")]
    connection.cursor_instance.rows = [(1,)]

    await database.fetch_rows("SELECT %(value)s AS n", {"value": 1})

    assert connection.cursor_instance.executed == [
        ("SELECT %(value)s AS n", {"value": 1})
    ]


async def test_fetch_rows_serializes_special_values(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [
        SimpleNamespace(name="d"),
        SimpleNamespace(name="b"),
        SimpleNamespace(name="payload"),
        SimpleNamespace(name="n"),
    ]
    connection.cursor_instance.rows = [
        (date(2025, 1, 1), b"\x01\x02", {"a": [1, 2]}, None)
    ]

    result = await database.fetch_rows("SELECT d, b, payload, n FROM t")

    assert result.rows == (
        ("2025-01-01", "0102", {"a": [1, 2]}, None),
    )
    assert result.row_count == 1


async def test_fetch_rows_rejects_statement_without_result_set(
    pool_spy: PoolSpy,
) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = None

    with pytest.raises(InvalidQueryError):
        await database.fetch_rows("SET LOCAL statement_timeout = 1000")


async def test_fetch_rows_rejects_empty_query(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)

    with pytest.raises(InvalidQueryError):
        await database.fetch_rows("   ")


async def test_fetch_rows_translates_multiple_statements_error(
    pool_spy: PoolSpy,
) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.raise_on_execute = psycopg.errors.SyntaxError(
        "cannot insert multiple commands into a prepared statement"
    )

    with pytest.raises(InvalidQueryError, match="only a single statement is allowed"):
        await database.fetch_rows("SELECT 1; SELECT 2")


async def test_fetch_rows_keeps_other_sql_errors_generic(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.raise_on_execute = psycopg.errors.SyntaxError(
        "syntax error at or near SELECT"
    )

    with pytest.raises(DatabaseError) as excinfo:
        await database.fetch_rows("SELECT bad")

    assert not isinstance(excinfo.value, InvalidQueryError)
