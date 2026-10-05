"""MCP tool definitions."""

from __future__ import annotations

import logging
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from ja_postgres_mcp.config import MAX_DISCOVERY_PAGE_SIZE, QuerySettings
from ja_postgres_mcp.context import AppContext
from ja_postgres_mcp.database import (
    AmbiguousTableError,
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
    QueryResult,
    TableNotFoundError,
)
from ja_postgres_mcp.discovery import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintListOutput,
    DescribeTableOutput,
    IndexInfo,
    IndexListOutput,
    PreviewOutput,
    RelationshipInfo,
    RelationshipListOutput,
    SchemaInfo,
    SchemaListOutput,
    TableInfo,
    TableKind,
    TableListOutput,
    ViewDefinitionOutput,
    build_describe_columns_query,
    build_get_view_definition_query,
    build_list_constraints_query,
    build_list_indexes_query,
    build_list_relationships_query,
    build_list_schemas_query,
    build_list_tables_query,
    build_preview_resolve_query,
    build_preview_rows_query,
    build_resolve_table_query,
)
from ja_postgres_mcp.pagination import cursor_scope, decode_cursor, encode_cursor

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
_DB_LIST_CONSTRAINTS_DESCRIPTION = (
    "List table constraints: primary key, unique, foreign key, check and "
    "exclusion (read-only). Excludes system schemas; paginated via next_cursor; "
    "optional schema/table filters."
)
_DB_LIST_RELATIONSHIPS_DESCRIPTION = (
    "List foreign-key relationships between tables, with source and target "
    "relation and the constraint definition (read-only). Paginated via "
    "next_cursor; optional schema/table filters apply to the source table. Note: "
    "the definition names the target table even if it is outside the schema "
    "allowlist."
)
_DB_LIST_INDEXES_DESCRIPTION = (
    "List indexes with access method, uniqueness, primary flag, column names and "
    "definition (read-only). Excludes system schemas; paginated via next_cursor; "
    "optional schema/table filters."
)
_DB_GET_VIEW_DEFINITION_DESCRIPTION = (
    "Return the SQL definition of a view or materialized view (read-only). "
    "Resolves by name; if it exists in several schemas, specify schema."
)
_DB_PREVIEW_TABLE_DESCRIPTION = (
    "Preview rows of a relation using keyset pagination on its primary key "
    "(read-only). The relation must have a primary key; rows are ordered by the "
    "text form of the key, so the order is deterministic but lexicographic. This "
    "tool intentionally mixes row data (columns/rows) with discovery-style "
    "pagination (next_cursor)."
)


class QueryOutput(BaseModel):
    """Structured result of a read-only query."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


def _tool_error(tool_name: str, exc: DatabaseError) -> ToolError:
    """Log a real database failure and return a safe client error."""
    logger.warning("database error while running tool %s", tool_name, exc_info=exc)
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
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
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
    try:
        page = _resolve_page_size(page_size, context.query)
        scope = cursor_scope("db_list_schemas", {})
        key = decode_cursor(cursor, 1, scope) if cursor is not None else None
        query, params = build_list_schemas_query(context.query.allowed_schemas, page, key)
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_list_schemas", exc) from exc

    schemas = _schema_infos(result)
    has_more = len(schemas) > page
    items = schemas[:page]
    next_cursor = encode_cursor((items[-1].name,), scope) if has_more else None
    return SchemaListOutput(schemas=items, next_cursor=next_cursor, row_count=len(items))


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
    try:
        page = _resolve_page_size(page_size, context.query)
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        scope = cursor_scope("db_list_tables", {"schema": schema, "kind": kind})
        key = decode_cursor(cursor, 2, scope) if cursor is not None else None
        query, params = build_list_tables_query(
            context.query.allowed_schemas, page, schema=schema, kind=kind, cursor=key
        )
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_list_tables", exc) from exc

    tables = _table_infos(result)
    has_more = len(tables) > page
    items = tables[:page]
    next_cursor = (
        encode_cursor((items[-1].schema_name, items[-1].name), scope) if has_more else None
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
    try:
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
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
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
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


def _ambiguous_message(kind: str, name: str, result: QueryResult) -> str:
    schemas = [row[0] for row in result.rows]
    shown = ", ".join(schemas[:5])
    if len(schemas) > 5:
        shown += f" (+{len(schemas) - 5} more)"
    return f"{kind} {name!r} exists in multiple schemas: {shown}; specify schema"


def _require_single(kind: str, name: str, result: QueryResult) -> None:
    if result.row_count == 0:
        raise TableNotFoundError(f"{kind} {name!r} not found")
    if result.row_count > 1:
        raise AmbiguousTableError(_ambiguous_message(kind, name, result))


def _resolved_relation(table: str, result: QueryResult) -> tuple[str, str, TableKind]:
    if result.columns != ("schema_name", "name", "kind"):
        raise RuntimeError("unexpected table resolution result shape")
    _require_single("table", table, result)
    row = result.rows[0]
    return row[0], row[1], cast(TableKind, row[2])


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


async def db_list_constraints(
    ctx: Context[AppContext],
    schema: str | None = None,
    table: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> ConstraintListOutput:
    """List table constraints (read-only, paginated)."""
    context = ctx.request_context.lifespan_context
    try:
        page = _resolve_page_size(page_size, context.query)
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        scope = cursor_scope("db_list_constraints", {"schema": schema, "table": table})
        key = decode_cursor(cursor, 3, scope) if cursor is not None else None
        query, params = build_list_constraints_query(
            context.query.allowed_schemas, page, schema, table, key
        )
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_list_constraints", exc) from exc

    constraints = _constraint_infos(result)
    has_more = len(constraints) > page
    items = constraints[:page]
    next_cursor = (
        encode_cursor((items[-1].schema_name, items[-1].table_name, items[-1].name), scope)
        if has_more
        else None
    )
    return ConstraintListOutput(constraints=items, next_cursor=next_cursor, row_count=len(items))


def _constraint_infos(result: QueryResult) -> list[ConstraintInfo]:
    if result.columns != ("schema_name", "table_name", "name", "kind", "definition"):
        raise RuntimeError("unexpected constraint listing result shape")
    return [
        ConstraintInfo(
            schema_name=row[0],
            table_name=row[1],
            name=row[2],
            kind=row[3],
            definition=row[4],
        )
        for row in result.rows
    ]


async def db_list_relationships(
    ctx: Context[AppContext],
    schema: str | None = None,
    table: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> RelationshipListOutput:
    """List foreign-key relationships (read-only, paginated)."""
    context = ctx.request_context.lifespan_context
    try:
        page = _resolve_page_size(page_size, context.query)
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        scope = cursor_scope("db_list_relationships", {"schema": schema, "table": table})
        key = decode_cursor(cursor, 3, scope) if cursor is not None else None
        query, params = build_list_relationships_query(
            context.query.allowed_schemas, page, schema, table, key
        )
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_list_relationships", exc) from exc

    relationships = _relationship_infos(result)
    has_more = len(relationships) > page
    items = relationships[:page]
    next_cursor = (
        encode_cursor((items[-1].source_schema, items[-1].source_table, items[-1].name), scope)
        if has_more
        else None
    )
    return RelationshipListOutput(
        relationships=items, next_cursor=next_cursor, row_count=len(items)
    )


def _relationship_infos(result: QueryResult) -> list[RelationshipInfo]:
    if result.columns != (
        "name",
        "source_schema",
        "source_table",
        "target_schema",
        "target_table",
        "definition",
    ):
        raise RuntimeError("unexpected relationship listing result shape")
    return [
        RelationshipInfo(
            name=row[0],
            source_schema=row[1],
            source_table=row[2],
            target_schema=row[3],
            target_table=row[4],
            definition=row[5],
        )
        for row in result.rows
    ]


async def db_list_indexes(
    ctx: Context[AppContext],
    schema: str | None = None,
    table: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> IndexListOutput:
    """List indexes (read-only, paginated)."""
    context = ctx.request_context.lifespan_context
    try:
        page = _resolve_page_size(page_size, context.query)
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        scope = cursor_scope("db_list_indexes", {"schema": schema, "table": table})
        key = decode_cursor(cursor, 3, scope) if cursor is not None else None
        query, params = build_list_indexes_query(
            context.query.allowed_schemas, page, schema, table, key
        )
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_list_indexes", exc) from exc

    indexes = _index_infos(result)
    has_more = len(indexes) > page
    items = indexes[:page]
    next_cursor = (
        encode_cursor((items[-1].schema_name, items[-1].table_name, items[-1].name), scope)
        if has_more
        else None
    )
    return IndexListOutput(indexes=items, next_cursor=next_cursor, row_count=len(items))


def _index_infos(result: QueryResult) -> list[IndexInfo]:
    if result.columns != (
        "schema_name",
        "table_name",
        "name",
        "method",
        "is_unique",
        "is_primary",
        "columns",
        "definition",
    ):
        raise RuntimeError("unexpected index listing result shape")
    return [
        IndexInfo(
            schema_name=row[0],
            table_name=row[1],
            name=row[2],
            method=row[3],
            is_unique=row[4],
            is_primary=row[5],
            columns=list(row[6]),
            definition=row[7],
        )
        for row in result.rows
    ]


async def db_get_view_definition(
    ctx: Context[AppContext],
    view: str,
    schema: str | None = None,
) -> ViewDefinitionOutput:
    """Return the SQL definition of a view or materialized view (read-only)."""
    context = ctx.request_context.lifespan_context
    try:
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        query, params = build_get_view_definition_query(context.query.allowed_schemas, view, schema)
        result = await context.database.fetch_rows(query, params)
        return _view_definition(view, result)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_get_view_definition", exc) from exc


def _view_definition(view: str, result: QueryResult) -> ViewDefinitionOutput:
    if result.columns != ("schema_name", "name", "kind", "definition"):
        raise RuntimeError("unexpected view definition result shape")
    _require_single("view", view, result)
    row = result.rows[0]
    return ViewDefinitionOutput(schema_name=row[0], name=row[1], kind=row[2], definition=row[3])


async def db_preview_table(
    ctx: Context[AppContext],
    table: str,
    schema: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
) -> PreviewOutput:
    """Preview rows of a table, keyset-paginated on its primary key (read-only)."""
    context = ctx.request_context.lifespan_context
    try:
        page = _resolve_page_size(page_size, context.query)
        if schema is not None:
            _ensure_schema_allowed(schema, context.query.allowed_schemas)
        resolve_query, resolve_params = build_preview_resolve_query(
            context.query.allowed_schemas, table, schema
        )
        resolved = await context.database.fetch_rows(resolve_query, resolve_params)
        schema_name, name, _kind, pk_columns = _preview_relation(table, resolved)
        if not pk_columns:
            raise InvalidQueryError("relation has no primary key; row pagination is not available")
        scope = cursor_scope("db_preview_table", {"schema": schema_name, "table": name})
        key = decode_cursor(cursor, len(pk_columns), scope) if cursor is not None else None
        query, params = build_preview_rows_query(schema_name, name, tuple(pk_columns), page, key)
        result = await context.database.fetch_rows(query, params, max_rows=page + 1)
    except InvalidQueryError as exc:
        raise ToolError(str(exc)) from exc
    except DatabaseError as exc:
        raise _tool_error("db_preview_table", exc) from exc

    key_width = len(pk_columns)
    rows = list(result.rows)
    has_more = len(rows) > page
    items = rows[:page]
    next_cursor = None
    if has_more:
        parts = tuple(items[-1][index] for index in range(key_width))
        next_cursor = encode_cursor(parts, scope)
    return PreviewOutput(
        columns=list(result.columns[key_width:]),
        rows=[list(row[key_width:]) for row in items],
        next_cursor=next_cursor,
        row_count=len(items),
    )


def _preview_relation(table: str, result: QueryResult) -> tuple[str, str, str, list[str]]:
    if result.columns != ("schema_name", "name", "kind", "pk_columns"):
        raise RuntimeError("unexpected preview resolution result shape")
    _require_single("table", table, result)
    row = result.rows[0]
    return row[0], row[1], row[2], list(row[3])


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
    server.tool(
        title="List constraints",
        description=_DB_LIST_CONSTRAINTS_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_list_constraints)
    server.tool(
        title="List relationships",
        description=_DB_LIST_RELATIONSHIPS_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_list_relationships)
    server.tool(
        title="List indexes",
        description=_DB_LIST_INDEXES_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_list_indexes)
    server.tool(
        title="Get view definition",
        description=_DB_GET_VIEW_DEFINITION_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_get_view_definition)
    server.tool(
        title="Preview table",
        description=_DB_PREVIEW_TABLE_DESCRIPTION,
        annotations=_READ_ONLY_ANNOTATIONS,
    )(db_preview_table)
