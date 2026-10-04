"""Tests for the MCP tools and their wiring (no real database)."""

from __future__ import annotations

from collections.abc import Callable

from mcp import Client
from psycopg import OperationalError

from ja_pst_mcp.config import DatabaseSettings, QuerySettings, Settings
from ja_pst_mcp.database import DatabaseConnectionError, DatabaseError
from ja_pst_mcp.server import create_server
from ja_pst_mcp.tools import DATABASE_OPERATION_MESSAGE, DATABASE_UNAVAILABLE_MESSAGE


class FakeDatabase:
    def __init__(
        self, settings: DatabaseSettings, error: BaseException | None = None
    ) -> None:
        self.settings = settings
        self._error = error
        self.opened = False
        self.closed = False
        self.ping_count = 0

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> None:
        if self._error is not None:
            raise self._error
        self.ping_count += 1


def make_factory(
    created: dict[str, FakeDatabase], error: BaseException | None = None
) -> Callable[[DatabaseSettings, QuerySettings], FakeDatabase]:
    def factory(
        database_settings: DatabaseSettings, query_settings: QuerySettings
    ) -> FakeDatabase:
        created["database"] = FakeDatabase(database_settings, error)
        return created["database"]

    return factory


async def test_database_health_tool_over_in_memory_client(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(settings, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "database_health")
        assert tool.title == "Database health"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False

        result = await client.call_tool("database_health", {})

        assert result.is_error is False
        assert result.structured_content == {"status": "ok"}
        assert created["database"].settings == settings.database
        assert created["database"].ping_count == 1

    assert created["database"].opened is True
    assert created["database"].closed is True


async def test_database_health_maps_connection_failure(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseConnectionError(
        'connection to server at "db.example" (10.0.0.1), port 5432 failed'
    )
    server = create_server(settings, database_factory=make_factory(created, error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("database_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_UNAVAILABLE_MESSAGE in text
    assert "db.example" not in text
    assert "10.0.0.1" not in text


async def test_database_health_maps_operation_failure(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseError("serialization failure")
    server = create_server(settings, database_factory=make_factory(created, error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("database_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_OPERATION_MESSAGE in text
    assert "serialization failure" not in text


async def test_database_health_does_not_mask_unexpected_errors(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = OperationalError('connection to server at "db.example", port 5432 failed')
    server = create_server(settings, database_factory=make_factory(created, error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("database_health", {})

    assert result.is_error is True
    assert result.content[0].text == "Error executing tool database_health"
    assert "db.example" not in result.content[0].text
