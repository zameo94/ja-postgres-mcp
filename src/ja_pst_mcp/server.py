"""MCP server entry point served over the Streamable HTTP transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server import MCPServer

from ja_pst_mcp.config import DatabaseSettings, Settings, load_settings
from ja_pst_mcp.context import AppContext
from ja_pst_mcp.database import Database
from ja_pst_mcp.tools import register_tools

SERVER_NAME = "ja-pst-mcp"
MCP_TRANSPORT = "streamable-http"

DatabaseFactory = Callable[[DatabaseSettings], Database]


def create_server(
    settings: Settings, *, database_factory: DatabaseFactory = Database
) -> MCPServer:
    """Build the MCP server and register its tools.

    The database pool is not opened here; it is opened and closed by the
    lifespan when the server starts and stops.
    """
    server = MCPServer(
        name=SERVER_NAME,
        log_level=settings.log_level,
        lifespan=_make_lifespan(settings, database_factory),
    )
    register_tools(server)
    return server


def run_server(server: MCPServer, settings: Settings) -> None:
    """Serve the MCP server over Streamable HTTP (blocking)."""
    server.run(
        transport=MCP_TRANSPORT,
        host=settings.server.host,
        port=settings.server.port,
    )


def main() -> None:
    """Console-script entry point."""
    settings = load_settings()
    run_server(create_server(settings), settings)


def _make_lifespan(settings: Settings, database_factory: DatabaseFactory):
    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        database = database_factory(settings.database)
        await database.open()
        try:
            yield AppContext(database=database)
        finally:
            await database.close()

    return lifespan
