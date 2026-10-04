"""MCP tool definitions."""

from __future__ import annotations

import logging

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ja_pst_mcp.context import AppContext
from ja_pst_mcp.database import DatabaseConnectionError, DatabaseError

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_MESSAGE = "The database is currently unavailable."
DATABASE_OPERATION_MESSAGE = "The database operation failed."


def _database_tool_error(tool_name: str, exc: DatabaseError) -> ToolError:
    """Log the database error for diagnostics and return a safe client error."""
    logger.warning("database error while running tool %s", tool_name, exc_info=True)
    message = (
        DATABASE_UNAVAILABLE_MESSAGE
        if isinstance(exc, DatabaseConnectionError)
        else DATABASE_OPERATION_MESSAGE
    )
    return ToolError(message)


async def database_health(ctx: Context[AppContext]) -> dict[str, str]:
    """Check that the server can reach PostgreSQL. Read-only."""
    database = ctx.request_context.lifespan_context.database
    try:
        await database.ping()
    except DatabaseError as exc:
        raise _database_tool_error("database_health", exc) from exc
    return {"status": "ok"}


def register_tools(server: MCPServer[AppContext]) -> None:
    """Register every MCP tool on ``server``."""
    server.tool(
        title="Database health",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )(database_health)
