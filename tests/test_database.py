"""Unit tests for the PostgreSQL access layer (no real database)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
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
        self.row_factory: Any = None
        self.rows: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.executed.append((query, params))

    async def fetchmany(self, size: int) -> list[dict[str, Any]]:
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
        self.read_only: bool | None = None

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    async def set_read_only(self, value: bool | None) -> None:
        self.read_only = value

    def cursor(self, row_factory: Any = None) -> FakeCursor:
        self.cursor_instance.row_factory = row_factory
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
        open: bool,
    ) -> None:
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.min_size = min_size
        self.max_size = max_size
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
    }


def test_pool_is_configured_from_settings(pool_spy: PoolSpy) -> None:
    Database(SETTINGS)

    assert pool_spy.pool is not None
    assert pool_spy.pool.kwargs == build_connection_kwargs(SETTINGS)
    assert pool_spy.pool.min_size == 1
    assert pool_spy.pool.max_size == 5
    assert pool_spy.pool.auto_open is False


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


async def test_fetch_rows_runs_read_only_caps_and_serializes(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS, QuerySettings(max_rows=2))
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [SimpleNamespace(name="amount")]
    connection.cursor_instance.rows = [
        {"amount": Decimal("10.50")},
        {"amount": Decimal("20.00")},
        {"amount": Decimal("30.00")},
    ]

    result = await database.fetch_rows("SELECT amount FROM t")

    assert result.columns == ("amount",)
    assert result.rows == ({"amount": "10.50"}, {"amount": "20.00"})
    assert result.truncated is True
    assert connection.read_only is True
    assert connection.cursor_instance.executed == [("SELECT amount FROM t", None)]


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
    connection.cursor_instance.rows = [{"n": 1}]

    await database.fetch_rows("SELECT %s AS n", (1,))

    executed = dict(connection.executed)
    assert executed["SELECT set_config('statement_timeout', %s, true)"] == ("7000",)
    assert executed["SELECT set_config('lock_timeout', %s, true)"] == ("3000",)
    assert connection.cursor_instance.executed == [("SELECT %s AS n", (1,))]


async def test_fetch_rows_serializes_special_values(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)
    assert pool_spy.pool is not None
    connection = pool_spy.pool.connection_instance
    connection.cursor_instance.description = [
        SimpleNamespace(name="d"),
        SimpleNamespace(name="b"),
        SimpleNamespace(name="n"),
    ]
    connection.cursor_instance.rows = [
        {"d": date(2025, 1, 1), "b": b"\x01\x02", "n": None}
    ]

    result = await database.fetch_rows("SELECT d, b, n FROM t")

    assert result.rows == ({"d": "2025-01-01", "b": "0102", "n": None},)


async def test_fetch_rows_rejects_empty_and_multiple_statements(pool_spy: PoolSpy) -> None:
    database = Database(SETTINGS)

    with pytest.raises(InvalidQueryError):
        await database.fetch_rows("   ")

    with pytest.raises(InvalidQueryError):
        await database.fetch_rows("SELECT 1; SELECT 2")
