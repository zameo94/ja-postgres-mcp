"""MCP tool definitions."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from ja_pst_mcp.context import AppContext


async def database_health(ctx: Context[AppContext]) -> dict[str, str]:
    """Check that the server can reach PostgreSQL. Read-only."""
    database = ctx.request_context.lifespan_context.database
    await database.ping()
    return {"status": "ok"}


def register_tools(server: MCPServer[AppContext]) -> None:
    """Register every MCP tool on ``server``."""
    server.tool(
        title="Database health",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )(database_health)
