"""Tests for the MCP tools and their wiring (no real database)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp import Client
from psycopg import OperationalError

from ja_pst_mcp.config import DatabaseSettings, QuerySettings, Settings
from ja_pst_mcp.database import (
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
    QueryResult,
)
from ja_pst_mcp.server import create_server
from ja_pst_mcp.tools import (
    DATABASE_OPERATION_MESSAGE,
    DATABASE_UNAVAILABLE_MESSAGE,
)


class FakeDatabase:
    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        ping_error: BaseException | None = None,
        query_result: QueryResult | None = None,
        query_error: BaseException | None = None,
    ) -> None:
        self.settings = settings
        self._ping_error = ping_error
        self._query_result = query_result
        self._query_error = query_error
        self.opened = False
        self.closed = False
        self.ping_count = 0
        self.queries: list[str] = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> None:
        if self._ping_error is not None:
            raise self._ping_error
        self.ping_count += 1

    async def fetch_rows(
        self, query: str, params: Any = None
    ) -> QueryResult:
        self.queries.append(query)
        if self._query_error is not None:
            raise self._query_error
        assert self._query_result is not None
        return self._query_result


def make_factory(
    created: dict[str, FakeDatabase], **database_kwargs: Any
) -> Callable[[DatabaseSettings, QuerySettings], FakeDatabase]:
    def factory(
        database_settings: DatabaseSettings, query_settings: QuerySettings
    ) -> FakeDatabase:
        created["database"] = FakeDatabase(database_settings, **database_kwargs)
        return created["database"]

    return factory


async def test_db_health_tool_over_in_memory_client(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(settings, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "db_health")
        assert tool.title == "Database health"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False

        result = await client.call_tool("db_health", {})

        assert result.is_error is False
        assert result.structured_content == {"status": "ok"}
        assert created["database"].settings == settings.database
        assert created["database"].ping_count == 1

    assert created["database"].opened is True
    assert created["database"].closed is True


async def test_db_health_maps_connection_failure(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseConnectionError(
        'connection to server at "db.example" (10.0.0.1), port 5432 failed'
    )
    server = create_server(settings, database_factory=make_factory(created, ping_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_UNAVAILABLE_MESSAGE in text
    assert "db.example" not in text
    assert "10.0.0.1" not in text


async def test_db_health_maps_operation_failure(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseError("serialization failure")
    server = create_server(settings, database_factory=make_factory(created, ping_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_OPERATION_MESSAGE in text
    assert "serialization failure" not in text


async def test_db_health_does_not_mask_unexpected_errors(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = OperationalError('connection to server at "db.example", port 5432 failed')
    server = create_server(settings, database_factory=make_factory(created, ping_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_health", {})

    assert result.is_error is True
    assert result.content[0].text == "Error executing tool db_health"
    assert "db.example" not in result.content[0].text


async def test_db_run_read_only_query_returns_structured_result(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    query_result = QueryResult(
        columns=("n", "label"), rows=((1, "x"),), truncated=False
    )
    server = create_server(
        settings, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(
            item for item in listing.tools if item.name == "db_run_read_only_query"
        )
        assert tool.title == "Run read-only query"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

        result = await client.call_tool(
            "db_run_read_only_query", {"sql": "SELECT 1 AS n, 'x' AS label"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "columns": ["n", "label"],
        "rows": [[1, "x"]],
        "truncated": False,
    }
    assert created["database"].queries == ["SELECT 1 AS n, 'x' AS label"]


async def test_db_run_read_only_query_reports_invalid_query(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = InvalidQueryError("only a single statement is allowed")
    server = create_server(settings, database_factory=make_factory(created, query_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_run_read_only_query", {"sql": "SELECT 1; SELECT 2"}
        )

    assert result.is_error is True
    assert "only a single statement is allowed" in result.content[0].text


async def test_db_run_read_only_query_maps_database_error(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseError("statement timeout")
    server = create_server(settings, database_factory=make_factory(created, query_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_run_read_only_query", {"sql": "SELECT 1"})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_OPERATION_MESSAGE in text
    assert "statement timeout" not in text
