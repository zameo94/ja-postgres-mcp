"""Tests for the MCP tools and their wiring (no real database)."""

from __future__ import annotations

from mcp import Client

from ja_pst_mcp.config import DatabaseSettings, Settings
from ja_pst_mcp.server import create_server


class FakeDatabase:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings
        self.opened = False
        self.closed = False
        self.ping_count = 0

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> None:
        self.ping_count += 1


async def test_database_health_tool_over_in_memory_client(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}

    def factory(database_settings: DatabaseSettings) -> FakeDatabase:
        created["database"] = FakeDatabase(database_settings)
        return created["database"]

    server = create_server(settings, database_factory=factory)

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
