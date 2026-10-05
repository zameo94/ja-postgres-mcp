"""MCP tool definitions."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ja_pst_mcp.config import MAX_DISCOVERY_PAGE_SIZE, QuerySettings
from ja_pst_mcp.context import AppContext
from ja_pst_mcp.database import (
    AmbiguousTableError,
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
    QueryResult,
    TableNotFoundError,
)
from ja_pst_mcp.discovery import (
    ColumnInfo,
    DescribeTableOutput,
    SchemaInfo,
    SchemaListOutput,
    TableInfo,
    TableKind,
    TableListOutput,
    build_describe_columns_query,
    build_list_schemas_query,
    build_list_tables_query,
    build_resolve_table_query,
)
from ja_pst_mcp.pagination import cursor_scope, decode_cursor, encode_cursor

logger = logging.getLogger(__name__)

DATABASE_UNAVAILABLE_MESSAGE = "The database is currently unavailable."
DATABASE_OPERATION_MESSAGE = "The database operation failed."

_READ_ONLY_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# PostgreSQL allows at most 1600 columns per relation; this is comfortably above.
_DESCRIBE_MAX_COLUMNS = 2000

_DB_HEALTH_DESCRIPTION = "Check that the server can reach PostgreSQL (read-only)."
_DB_RUN_QUERY_DESCRIPTION = (
    "Run a single read-only SQL statement (SELECT/aggregations) and return rows. "
    "Bind dynamic values with params; never interpolate them into the SQL."
)
_DB_LIST_SCHEMAS_DESCRIPTION = (
    "List database schemas visible to the server, excluding system schemas "
    "(read-only). Paginated: pass next_cursor back to fetch the next page; "
    "next_cursor is null on the last page."
)
_DB_LIST_TABLES_DESCRIPTION = (
    "List tables, views, materialized views and foreign tables, excluding system "
    "schemas (read-only). Paginated via next_cursor. estimated_rows is planner "
    "statistics from the last ANALYZE: it can be stale and is null when the "
    "relation was never analyzed."
)
_DB_DESCRIBE_TABLE_DESCRIPTION = (
    "Describe a table or view: columns (type, nullability, default, comment, "
    "primary-key flag), kind and primary key (read-only). attnum is PostgreSQL's "
    "column number (gaps after dropped columns). Resolves the relation by name; "
    "if the name exists in several schemas, specify schema."
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


def _resolve_page_size(requested: int | None, settings: QuerySettings) -> int:
    max_page = min(settings.discovery_max_page_size, MAX_DISCOVERY_PAGE_SIZE)
    page_size = settings.discovery_page_size if requested is None else requested
    if page_size < 1 or page_size > max_page:
        raise InvalidQueryError(f"page_size must be between 1 and {max_page}")
    return page_size


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


async def db_list_schemas(
    ctx: Context[AppContext],
    page_size: int | None = None,
    cursor: str | None = None,
) -> SchemaListOutput:
    """List database schemas visible to the server (read-only, paginated)."""
    context = ctx.request_context.lifespan_context
    page = _resolve_page_size(page_size, context.query)
    scope = cursor_scope("db_list_schemas", {})
    key = decode_cursor(cursor, 1, scope) if cursor is not None else None
    try:
        query, params = build_list_schemas_query(
            context.query.allowed_schemas, page, key
        )
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except DatabaseError as exc:
        raise _tool_error("db_list_schemas", exc) from exc

    schemas = _schema_infos(result)
    has_more = len(schemas) > page
    items = schemas[:page]
    next_cursor = encode_cursor((items[-1].name,), scope) if has_more else None
    return SchemaListOutput(
        schemas=items, next_cursor=next_cursor, row_count=len(items)
    )


def _schema_infos(result: QueryResult) -> list[SchemaInfo]:
    if result.columns != ("name", "owner"):
        raise RuntimeError("unexpected schema listing result shape")
    return [SchemaInfo(name=row[0], owner=row[1]) for row in result.rows]


async def db_list_tables(
    ctx: Context[AppContext],
    schema: str | None = None,
    kind: TableKind | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> TableListOutput:
    """List tables, views, materialized views and foreign tables (paginated)."""
    context = ctx.request_context.lifespan_context
    page = _resolve_page_size(page_size, context.query)
    if schema is not None:
        _ensure_schema_allowed(schema, context.query.allowed_schemas)
    scope = cursor_scope("db_list_tables", {"schema": schema, "kind": kind})
    key = decode_cursor(cursor, 2, scope) if cursor is not None else None
    try:
        query, params = build_list_tables_query(
            context.query.allowed_schemas, page, schema=schema, kind=kind, cursor=key
        )
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except DatabaseError as exc:
        raise _tool_error("db_list_tables", exc) from exc

    tables = _table_infos(result)
    has_more = len(tables) > page
    items = tables[:page]
    next_cursor = (
        encode_cursor((items[-1].schema_name, items[-1].name), scope)
        if has_more
        else None
    )
    return TableListOutput(tables=items, next_cursor=next_cursor, row_count=len(items))


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


async def db_describe_table(
    ctx: Context[AppContext],
    table: str,
    schema: str | None = None,
) -> DescribeTableOutput:
    """Describe a table or view (columns, types, nullability, defaults, PK)."""
    context = ctx.request_context.lifespan_context
    if schema is not None:
        _ensure_schema_allowed(schema, context.query.allowed_schemas)
    try:
        resolve_query, resolve_params = build_resolve_table_query(
            context.query.allowed_schemas, table, schema
        )
        resolved = await context.database.fetch_rows(resolve_query, resolve_params)
        schema_name, name, kind = _resolved_relation(table, resolved)
        columns_query, columns_params = build_describe_columns_query(schema_name, name)
        columns_result = await context.database.fetch_rows(
            columns_query, columns_params, max_rows=_DESCRIBE_MAX_COLUMNS
        )
        if columns_result.truncated:
            raise InvalidQueryError("relation has too many columns to describe")
        if columns_result.row_count == 0:
            raise TableNotFoundError(f"table {schema_name}.{name} not found")
    except DatabaseError as exc:
        raise _tool_error("db_describe_table", exc) from exc

    columns = _column_infos(columns_result)
    return DescribeTableOutput(
        schema_name=schema_name,
        name=name,
        kind=kind,
        columns=columns,
        primary_key=[column.name for column in columns if column.is_primary_key],
    )


def _resolved_relation(table: str, result: QueryResult) -> tuple[str, str, str]:
    if result.columns != ("schema_name", "name", "kind"):
        raise RuntimeError("unexpected table resolution result shape")
    if result.row_count == 0:
        raise TableNotFoundError(f"table {table!r} not found")
    if result.row_count > 1:
        schemas = [row[0] for row in result.rows]
        shown = ", ".join(schemas[:5])
        if len(schemas) > 5:
            shown += f" (+{len(schemas) - 5} more)"
        raise AmbiguousTableError(
            f"table {table!r} exists in multiple schemas: {shown}; specify schema"
        )
    row = result.rows[0]
    return row[0], row[1], row[2]


def _column_infos(result: QueryResult) -> list[ColumnInfo]:
    if result.columns != (
        "name",
        "attnum",
        "data_type",
        "nullable",
        "default",
        "is_primary_key",
        "comment",
    ):
        raise RuntimeError("unexpected column listing result shape")
    return [
        ColumnInfo(
            name=row[0],
            attnum=row[1],
            data_type=row[2],
            nullable=row[3],
            default=row[4],
            is_primary_key=row[5],
            comment=row[6],
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
    server.tool(
        title="Describe table",
        description=_DB_DESCRIBE_TABLE_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_describe_table)
