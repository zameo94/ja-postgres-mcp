"""PostgreSQL access layer built on psycopg's async connection pool."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ja_pst_mcp.config import DatabaseSettings

_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5


class DatabaseError(RuntimeError):
    """Base error for database operations."""


class DatabaseConnectionError(DatabaseError):
    """Raised when a database connection cannot be established."""


class DatabaseProtocol(Protocol):
    """Minimal database contract used by the lifespan and tools."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> None: ...


def build_connection_kwargs(settings: DatabaseSettings) -> dict[str, Any]:
    """Map validated settings to psycopg connection keyword arguments."""
    return {
        "host": settings.host,
        "port": settings.port,
        "dbname": settings.name,
        "user": settings.user,
        "password": settings.password,
        "connect_timeout": settings.connect_timeout_seconds,
    }


class Database:
    """Owns the lifecycle of the PostgreSQL connection pool."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._pool = AsyncConnectionPool(
            conninfo="",
            kwargs=build_connection_kwargs(settings),
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            open=False,
        )

    async def open(self) -> None:
        """Open the pool, waiting until a connection is established."""
        try:
            await self._pool.open(
                wait=True, timeout=self._settings.connect_timeout_seconds
            )
        except Exception as exc:
            raise DatabaseConnectionError(
                "unable to connect to the database"
            ) from exc

    async def close(self) -> None:
        """Close the pool and release all connections."""
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Acquire a pooled connection for the duration of the context."""
        async with self._pool.connection() as connection:
            yield connection

    async def ping(self) -> None:
        """Run a lightweight query to verify connectivity."""
        async with self.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")

    async def __aenter__(self) -> "Database":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
