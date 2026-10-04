"""Unit tests for the PostgreSQL access layer (no real database)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg import OperationalError

from ja_pst_mcp import database as database_module
from ja_pst_mcp.config import DatabaseSettings
from ja_pst_mcp.database import (
    Database,
    DatabaseConnectionError,
    DatabaseError,
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

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.executed.append((query, params))


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def cursor(self) -> FakeCursor:
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
