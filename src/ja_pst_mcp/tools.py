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
    QueryResult,
)
from ja_pst_mcp.discovery import (
    SchemaInfo,
    SchemaListOutput,
    TableInfo,
    TableKind,
    TableListOutput,
    build_list_schemas_query,
    build_list_tables_query,
)

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_MESSAGE = "The database is currently unavailable."
DATABASE_OPERATION_MESSAGE = "The database operation failed."

_READ_ONLY_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

_DB_HEALTH_DESCRIPTION = "Check that the server can reach PostgreSQL (read-only)."
_DB_RUN_QUERY_DESCRIPTION = (
    "Run a single read-only SQL statement (SELECT/aggregations) and return rows. "
    "Bind dynamic values with params; never interpolate them into the SQL."
)
_DB_LIST_SCHEMAS_DESCRIPTION = (
    "List database schemas visible to the server, excluding system schemas "
    "(read-only)."
)
_DB_LIST_TABLES_DESCRIPTION = (
    "List tables, views, materialized views and foreign tables, excluding system "
    "schemas (read-only). estimated_rows is planner statistics from the last "
    "ANALYZE: it can be stale and is null when the relation was never analyzed."
)


class QueryOutput(BaseModel):
    """Structured result of a read-only query."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
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


def _ensure_schema_allowed(schema: str, allowed_schemas: tuple[str, ...]) -> None:
    if allowed_schemas and schema not in allowed_schemas:
        raise InvalidQueryError(f"schema {schema!r} is not in the allowed schemas")


async def db_health(ctx: Context[AppContext]) -> dict[str, str]:
    """Check that the server can reach PostgreSQL (read-only)."""
    database = ctx.request_context.lifespan_context.database
    try:
        await database.ping()
    except DatabaseError as exc:
        raise _tool_error("db_health", exc) from exc
    return {"status": "ok"}


async def db_run_read_only_query(
    ctx: Context[AppContext],
    sql: str,
    params: dict[str, Any] | list[Any] | None = None,
) -> QueryOutput:
    """Run a single read-only SQL statement and return rows."""
    database = ctx.request_context.lifespan_context.database
    try:
        result = await database.fetch_rows(sql, params)
    except DatabaseError as exc:
        raise _tool_error("db_run_read_only_query", exc) from exc
    return QueryOutput(
        columns=list(result.columns),
        rows=[list(row) for row in result.rows],
        row_count=result.row_count,
        truncated=result.truncated,
    )


async def db_list_schemas(ctx: Context[AppContext]) -> SchemaListOutput:
    """List database schemas visible to the server (read-only)."""
    context = ctx.request_context.lifespan_context
    query, params = build_list_schemas_query(context.query.allowed_schemas)
    try:
        result = await context.database.fetch_rows(query, params)
    except DatabaseError as exc:
        raise _tool_error("db_list_schemas", exc) from exc
    return SchemaListOutput(
        schemas=_schema_infos(result),
        truncated=result.truncated,
    )


def _schema_infos(result: QueryResult) -> list[SchemaInfo]:
    if result.columns != ("name", "owner"):
        raise RuntimeError("unexpected schema listing result shape")
    return [SchemaInfo(name=row[0], owner=row[1]) for row in result.rows]


async def db_list_tables(
    ctx: Context[AppContext],
    schema: str | None = None,
    kind: TableKind | None = None,
) -> TableListOutput:
    """List tables, views, materialized views and foreign tables (read-only)."""
    context = ctx.request_context.lifespan_context
    try:
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        query, params = build_list_tables_query(
            context.query.allowed_schemas, schema, kind
        )
        result = await context.database.fetch_rows(query, params)
    except DatabaseError as exc:
        raise _tool_error("db_list_tables", exc) from exc
    return TableListOutput(tables=_table_infos(result), truncated=result.truncated)


def _table_infos(result: QueryResult) -> list[TableInfo]:
    if result.columns != ("schema_name", "name", "kind", "estimated_rows"):
        raise RuntimeError("unexpected table listing result shape")
    return [
        TableInfo(
            schema_name=row[0],
            name=row[1],
            kind=row[2],
            estimated_rows=row[3],
        )
        for row in result.rows
    ]


def register_tools(server: MCPServer[AppContext]) -> None:
    """Register every MCP tool on ``server``."""
    server.tool(
        title="Database health",
        description=_DB_HEALTH_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_health)
    server.tool(
        title="Run read-only query",
        description=_DB_RUN_QUERY_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_run_read_only_query)
    server.tool(
        title="List schemas",
        description=_DB_LIST_SCHEMAS_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_list_schemas)
    server.tool(
        title="List tables",
        description=_DB_LIST_TABLES_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_list_tables)
