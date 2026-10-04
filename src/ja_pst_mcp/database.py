"""PostgreSQL access layer built on psycopg's async connection pool."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import AsyncConnection, Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ja_pst_mcp.config import DatabaseSettings, QuerySettings

_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5


class DatabaseError(RuntimeError):
    """An expected PostgreSQL/pool error (connection or query failure).

    Wrapped messages are generic; the original driver error is kept as
    ``__cause__`` for server-side diagnostics.
    """


class DatabaseConnectionError(DatabaseError):
    """A database connection could not be acquired or established."""


class InvalidQueryError(DatabaseError):
    """The requested query is not allowed (empty or multiple statements)."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Serializable result of a read-only query."""

    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    truncated: bool


class DatabaseProtocol(Protocol):
    """Minimal database contract used by the lifespan and tools."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> None: ...

    async def fetch_rows(
        self, query: str, params: Sequence[Any] | None = None
    ) -> QueryResult: ...


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

    def __init__(
        self, settings: DatabaseSettings, query: QuerySettings | None = None
    ) -> None:
        self._settings = settings
        self._query = query or QuerySettings()
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
        except PsycopgError as exc:
            raise DatabaseConnectionError("unable to connect to the database") from exc

    async def close(self) -> None:
        """Close the pool and release all connections."""
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Yield a pooled connection, wrapping driver errors.

        Errors acquiring the connection become ``DatabaseConnectionError``;
        driver errors raised while the caller uses it become
        ``DatabaseError``. Anything else propagates unchanged.
        """
        try:
            async with self._pool.connection() as connection:
                try:
                    yield connection
                except PsycopgError as exc:
                    raise DatabaseError("database operation failed") from exc
        except PsycopgError as exc:
            raise DatabaseConnectionError(
                "unable to acquire a database connection"
            ) from exc

    async def ping(self) -> None:
        """Run a lightweight query to verify connectivity."""
        async with self.connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")

    async def fetch_rows(
        self, query: str, params: Sequence[Any] | None = None
    ) -> QueryResult:
        """Run a single read-only query, capped at ``max_rows`` rows.

        The statement runs in a READ ONLY transaction with statement/lock
        timeouts; at most ``max_rows`` rows are returned and ``truncated``
        reports whether more were available.
        """
        _ensure_single_statement(query)
        max_rows = self._query.max_rows

        async with self.connection() as connection:
            await connection.set_read_only(True)
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self._query.statement_timeout_seconds * 1000),),
                )
                await connection.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (str(self._query.lock_timeout_seconds * 1000),),
                )
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, params)
                    fetched = await cursor.fetchmany(max_rows + 1)
                    columns = tuple(
                        column.name for column in cursor.description or ()
                    )

        truncated = len(fetched) > max_rows
        return QueryResult(
            columns=columns,
            rows=tuple(_serialize_row(row) for row in fetched[:max_rows]),
            truncated=truncated,
        )

    async def __aenter__(self) -> "Database":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def _ensure_single_statement(query: str) -> None:
    body = query.strip()
    if not body:
        raise InvalidQueryError("empty query")
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise InvalidQueryError("only a single statement is allowed")


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _jsonify(value) for key, value in row.items()}


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)
