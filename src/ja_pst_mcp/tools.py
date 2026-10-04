"""MCP tool definitions."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ja_pst_mcp.context import AppContext
from ja_pst_mcp.database import (
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
)

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_MESSAGE = "The database is currently unavailable."
DATABASE_OPERATION_MESSAGE = "The database operation failed."


class QueryOutput(BaseModel):
    """Structured result of a read-only query."""

    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def _tool_error(tool_name: str, exc: DatabaseError) -> ToolError:
    """Log the database error for diagnostics and return a safe client error."""
    logger.warning("database error while running tool %s", tool_name, exc_info=True)
    if isinstance(exc, InvalidQueryError):
        return ToolError(str(exc))
    message = (
        DATABASE_UNAVAILABLE_MESSAGE
        if isinstance(exc, DatabaseConnectionError)
        else DATABASE_OPERATION_MESSAGE
    )
    return ToolError(message)


async def db_health(ctx: Context[AppContext]) -> dict[str, str]:
    """Check that the server can reach PostgreSQL. Read-only."""
    database = ctx.request_context.lifespan_context.database
    try:
        await database.ping()
    except DatabaseError as exc:
        raise _tool_error("db_health", exc) from exc
    return {"status": "ok"}


async def db_run_read_only_query(ctx: Context[AppContext], sql: str) -> QueryOutput:
    """Run a single read-only SQL statement (SELECT/aggregations) and return rows.

    Use it to answer business questions on the connected database; discover the
    schema first with the discovery tools. Writing is impossible (read-only).
    """
    database = ctx.request_context.lifespan_context.database
    try:
        result = await database.fetch_rows(sql)
    except DatabaseError as exc:
        raise _tool_error("db_run_read_only_query", exc) from exc
    return QueryOutput(
        columns=list(result.columns),
        rows=[list(row) for row in result.rows],
        truncated=result.truncated,
    )


def register_tools(server: MCPServer[AppContext]) -> None:
    """Register every MCP tool on ``server``."""
    server.tool(
        title="Database health",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )(db_health)
    server.tool(
        title="Run read-only query",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )(db_run_read_only_query)
