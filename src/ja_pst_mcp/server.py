"""MCP server entry point served over the Streamable HTTP transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from ja_pst_mcp.config import DatabaseSettings, ServerSettings, Settings, load_settings
from ja_pst_mcp.context import AppContext
from ja_pst_mcp.database import Database, DatabaseProtocol
from ja_pst_mcp.tools import register_tools

SERVER_NAME = "ja-pst-mcp"
MCP_TRANSPORT = "streamable-http"

DatabaseFactory = Callable[[DatabaseSettings], DatabaseProtocol]


def create_server(
    settings: Settings, *, database_factory: DatabaseFactory = Database
) -> MCPServer[AppContext]:
    """Build the MCP server and register its tools.

    The database pool is not opened here; it is opened and closed by the
    lifespan when the server starts and stops.
    """
    server: MCPServer[AppContext] = MCPServer(
        name=SERVER_NAME,
        log_level=settings.log_level,
        lifespan=_make_lifespan(settings, database_factory),
    )
    register_tools(server)
    return server


def run_server(server: MCPServer[AppContext], settings: Settings) -> None:
    """Serve the MCP server over Streamable HTTP (blocking)."""
    server.run(
        transport=MCP_TRANSPORT,
        host=settings.server.host,
        port=settings.server.port,
        transport_security=_transport_security(settings.server),
    )


def _transport_security(
    settings: ServerSettings,
) -> TransportSecuritySettings | None:
    """Return the SDK's DNS-rebinding settings for the configured allowlist.

    With no ``allowed_hosts`` this returns ``None``. For a localhost bind the
    SDK then enables its own localhost-only protection; for a non-localhost
    bind, :func:`load_settings` has already rejected the configuration with a
    ``ConfigurationError``, so the server never starts a non-localhost bind
    without protection.
    """
    if not settings.allowed_hosts:
        return None
    return TransportSecuritySettings(
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def main() -> None:
    """Console-script entry point."""
    settings = load_settings()
    run_server(create_server(settings), settings)


def _make_lifespan(
    settings: Settings, database_factory: DatabaseFactory
) -> Callable[[MCPServer[AppContext]], AbstractAsyncContextManager[AppContext]]:
    @asynccontextmanager
    async def lifespan(server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        database = database_factory(settings.database)
        await database.open()
        try:
            yield AppContext(database=database)
        finally:
            await database.close()

    return lifespan
