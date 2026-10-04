"""MCP server entry point served over the Streamable HTTP transport."""

from __future__ import annotations

from mcp.server import MCPServer

from ja_pst_mcp.config import Settings, load_settings

SERVER_NAME = "ja-pst-mcp"
MCP_TRANSPORT = "streamable-http"


def create_server(settings: Settings) -> MCPServer:
    """Build the MCP server.

    Only what the server *is* is configured here; transport options belong to
    :func:`run_server`.
    """
    return MCPServer(name=SERVER_NAME, log_level=settings.log_level)


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
