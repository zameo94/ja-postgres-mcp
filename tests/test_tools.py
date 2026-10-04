"""Tests for the MCP tools and their wiring (no real database)."""

from __future__ import annotations

from types import SimpleNamespace

from mcp import Client

from ja_pst_mcp.config import DatabaseSettings, ServerSettings, Settings
from ja_pst_mcp.server import create_server
from ja_pst_mcp.tools import database_health


class FakeDatabase:
    def __init__(self, settings: DatabaseSettings | None = None) -> None:
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


def make_settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            host="db", port=5432, name="japst", user="alice", password="s3cret"
        ),
        server=ServerSettings(),
        log_level="INFO",
    )


class FakeContext:
    def __init__(self, database: FakeDatabase) -> None:
        self.request_context = SimpleNamespace(
            lifespan_context=SimpleNamespace(database=database)
        )


async def test_database_health_pings_and_reports_ok() -> None:
    database = FakeDatabase()

    result = await database_health(FakeContext(database))

    assert result == {"status": "ok"}
    assert database.ping_count == 1


async def test_database_health_tool_over_in_memory_client() -> None:
    created: dict[str, FakeDatabase] = {}

    def factory(settings: DatabaseSettings) -> FakeDatabase:
        created["database"] = FakeDatabase(settings)
        return created["database"]

    server = create_server(make_settings(), database_factory=factory)

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        assert "database_health" in [tool.name for tool in listing.tools]

        await client.call_tool("database_health", {})
        assert created["database"].ping_count == 1

    assert created["database"].opened is True
    assert created["database"].closed is True
