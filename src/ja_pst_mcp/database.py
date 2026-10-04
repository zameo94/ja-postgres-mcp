"""PostgreSQL access layer built on psycopg's async connection pool."""

from __future__ import annotations

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

QueryParams = Sequence[Any] | Mapping[str, Any]


class DatabaseError(RuntimeError):
    """An expected PostgreSQL/pool error (connection or query failure).

    Wrapped messages are generic; the original driver error is kept as
    ``__cause__`` for server-side diagnostics.
    """


class DatabaseConnectionError(DatabaseError):
    """A database connection could not be acquired or established."""


class InvalidQueryError(DatabaseError):
    """The query is rejected by policy (empty, multiple statements, no result)."""


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Serializable result of a read-only query.

    ``rows`` are value tuples aligned with ``columns`` (positional), so
    duplicate column names in a query never collapse or drop data.

    Serialization convention for each value:
    ``null``/``str``/``int``/``float``/``bool`` pass through; ``json``/``jsonb``
    become ``dict``/``list``; ``bytes`` become a hex string; any other type
    (``Decimal``, ``date``/``datetime``, ``UUID``, ...) becomes ``str``.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
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

        Read-only is enforced at connection level (see ``build_connection_kwargs``);
        statement/lock timeouts are set locally for this transaction. At most
        ``max_rows`` rows are returned; ``truncated`` reports whether more were
        available. A statement that produced no result set is rejected.
        """
        _ensure_single_statement(query)
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
                async with connection.cursor() as cursor:
                    await cursor.execute(query, params)
                    if cursor.description is None:
                        raise InvalidQueryError("query did not return a result set")
                    fetched = await cursor.fetchmany(max_rows + 1)
                    columns = tuple(column.name for column in cursor.description)

        truncated = len(fetched) > max_rows
        rows = tuple(
            tuple(_jsonify(value) for value in row) for row in fetched[:max_rows]
        )
        return QueryResult(columns=columns, rows=rows, truncated=truncated)

    async def __aenter__(self) -> "Database":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def _ensure_single_statement(query: str) -> None:
    """Best-effort guard against obviously multiple statements.

    This is UX, **not** a security boundary: the read-only connection is what
    prevents writes. It scans semicolons outside string literals and comments
    so that valid queries containing ``;`` inside a literal are not rejected.
    """
    body = _without_literals_and_comments(query).strip()
    if not body:
        raise InvalidQueryError("empty query")
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise InvalidQueryError("only a single statement is allowed")


def _without_literals_and_comments(sql: str) -> str:
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in ("'", '"'):
            out.append(" ")
            index = _skip_quoted(sql, index, char)
        elif char == "$":
            end = _skip_dollar_quoted(sql, index)
            if end is None:
                out.append(char)
                index += 1
            else:
                out.append(" ")
                index = end
        elif sql.startswith("--", index):
            out.append(" ")
            index = _skip_line_comment(sql, index)
        elif sql.startswith("/*", index):
            out.append(" ")
            index = _skip_block_comment(sql, index)
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _skip_line_comment(sql: str, start: int) -> int:
    index = start + 2
    length = len(sql)
    while index < length and sql[index] != "\n":
        index += 1
    return index


def _skip_block_comment(sql: str, start: int) -> int:
    index = start + 2
    length = len(sql)
    depth = 1
    while index < length and depth:
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def _skip_dollar_quoted(sql: str, start: int) -> int | None:
    index = start + 1
    length = len(sql)
    while index < length and (sql[index].isalnum() or sql[index] == "_"):
        index += 1
    if index >= length or sql[index] != "$":
        return None
    tag = sql[start : index + 1]
    end = sql.find(tag, index + 1)
    return length if end == -1 else end + len(tag)


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)
