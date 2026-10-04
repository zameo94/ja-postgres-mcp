"""PostgreSQL access layer built on psycopg's async connection pool."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import AsyncConnection, Error as PsycopgError
from psycopg_pool import AsyncConnectionPool

from ja_pst_mcp.config import DatabaseSettings, QuerySettings

_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5
# Every pooled connection is read-only: the server is read-only by design.
_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
# Prefix for the server-side cursor name (unique per query).
_CURSOR_NAME_PREFIX = "ja_pst_"

QueryParams = Sequence[Any] | Mapping[str, Any]


class DatabaseError(RuntimeError):
    """An expected PostgreSQL/pool error (connection or query failure).

    Wrapped messages are generic; the original driver error is kept as
    ``__cause__`` for server-side diagnostics.
    """


class DatabaseConnectionError(DatabaseError):
    """A database connection could not be acquired or established."""


class InvalidQueryError(DatabaseError):
    """The query is rejected by policy (empty query or without a result set)."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Serializable result of a read-only query.

    ``rows`` are value tuples aligned with ``columns`` (positional), so
    duplicate column names in a query never collapse or drop data.
    ``row_count`` is the number of rows actually returned (not the total the
    query would produce when ``truncated`` is ``True``).

    Serialization convention for each value:
    ``null``/``str``/``int``/``float``/``bool`` pass through; ``json``/``jsonb``
    become ``dict``/``list``; ``bytes`` become a hex string; any other type
    (``Decimal``, ``date``/``datetime``, ``UUID``, ...) becomes ``str``.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool


class DatabaseProtocol(Protocol):
    """Minimal database contract used by the lifespan and tools."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> None: ...

    async def fetch_rows(
        self, query: str, params: QueryParams | None = None
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
        "options": _READ_ONLY_OPTIONS,
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
        self, query: str, params: QueryParams | None = None
    ) -> QueryResult:
        """Run one read-only query, capped at ``max_rows`` rows.

        The query runs through a **server-side cursor** (`DECLARE ... CURSOR`),
        which uses the extended protocol and therefore makes multiple statements
        structurally impossible, fetches rows in batches from the server (bounded
        client memory) and reports columns even for an empty result set. The
        transaction is READ ONLY (enforced at connection level) with statement
        and lock timeouts set locally. At most ``max_rows`` rows are returned.
        """
        if not query.strip():
            raise InvalidQueryError("empty query")
        max_rows = self._query.max_rows

        async with self.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self._query.statement_timeout_seconds * 1000),),
                )
                await connection.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (str(self._query.lock_timeout_seconds * 1000),),
                )
                name = _CURSOR_NAME_PREFIX + uuid.uuid4().hex
                async with connection.cursor(name=name) as cursor:
                    await cursor.execute(query, params)
                    if cursor.description is None:
                        raise InvalidQueryError("query did not return a result set")
                    fetched = await cursor.fetchmany(max_rows + 1)
                    columns = tuple(column.name for column in cursor.description)

        truncated = len(fetched) > max_rows
        rows = tuple(
            tuple(_jsonify(value) for value in row) for row in fetched[:max_rows]
        )
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )

    async def __aenter__(self) -> "Database":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)
