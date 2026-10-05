"""Discovery tool models and SQL builders."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TableKind = Literal["table", "view", "matview", "foreign"]


class SchemaInfo(BaseModel):
    """A database schema."""

    name: str
    owner: str


class SchemaListOutput(BaseModel):
    """Result of ``db_list_schemas``."""

    schemas: list[SchemaInfo]
    truncated: bool


class TableInfo(BaseModel):
    """A table, view, materialized view or foreign table."""

    schema_name: str
    name: str
    kind: TableKind
    estimated_rows: int | None


class TableListOutput(BaseModel):
    """Result of ``db_list_tables``."""

    tables: list[TableInfo]
    truncated: bool


class ColumnInfo(BaseModel):
    """A column of a table or view."""

    name: str
    attnum: int
    data_type: str
    nullable: bool
    default: str | None
    is_primary_key: bool
    comment: str | None


class DescribeTableOutput(BaseModel):
    """Result of ``db_describe_table``.

    ``primary_key`` is ``None`` when the column list was truncated, because it
    would otherwise look complete while derived from partial data.
    """

    schema_name: str
    name: str
    kind: TableKind
    columns: list[ColumnInfo]
    primary_key: list[str] | None
    truncated: bool


# System schemas (``pg_*`` and ``information_schema``) are excluded by default.
_LIST_SCHEMAS_SQL = (
    "SELECT schema_name AS name, schema_owner AS owner "
    "FROM information_schema.schemata "
    "WHERE LEFT(schema_name, 3) <> 'pg_' "
    "AND schema_name <> 'information_schema'"
)


def build_list_schemas_query(
    allowed_schemas: tuple[str, ...],
) -> tuple[str, list[list[str]] | None]:
    """Build the schema-listing SQL and its parameters (allowlist optional).

    The allowlist is bound as a single PostgreSQL array parameter, hence the
    nested ``[list(...)]`` (psycopg reads the outer sequence as parameters).
    """
    query = _LIST_SCHEMAS_SQL
    params: list[list[str]] | None = None
    if allowed_schemas:
        query += " AND schema_name = ANY(%s)"
        params = [list(allowed_schemas)]
    return query + " ORDER BY schema_name", params


# relkind codes grouped by the public ``kind`` vocabulary.
_KIND_RELKINDS: dict[str, tuple[str, ...]] = {
    "table": ("r", "p"),
    "view": ("v",),
    "matview": ("m",),
    "foreign": ("f",),
}

# System schemas (``pg_*`` and ``information_schema``) are excluded by default.
_LIST_TABLES_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS name, "
    "CASE c.relkind "
    "WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' "
    "WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' WHEN 'f' THEN 'foreign' "
    "END AS kind, "
    "CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END "
    "AS estimated_rows "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') "
    "AND n.nspname <> 'information_schema' "
    "AND LEFT(n.nspname, 3) <> 'pg_'"
)


def build_list_tables_query(
    allowed_schemas: tuple[str, ...],
    schema: str | None = None,
    kind: TableKind | None = None,
) -> tuple[str, list[object] | None]:
    """Build the table-listing SQL and its parameters (all filters optional).

    Array filters are bound as single PostgreSQL array parameters, hence the
    nested lists in ``params``.
    """
    conditions: list[str] = []
    params: list[object] = []
    if allowed_schemas:
        conditions.append("n.nspname = ANY(%s)")
        params.append(list(allowed_schemas))
    if schema is not None:
        conditions.append("n.nspname = %s")
        params.append(schema)
    if kind is not None:
        conditions.append("c.relkind::text = ANY(%s)")
        params.append(list(_KIND_RELKINDS[kind]))

    query = _LIST_TABLES_SQL
    if conditions:
        query += " AND " + " AND ".join(conditions)
    return query + " ORDER BY n.nspname, c.relname", (params or None)


_RESOLVE_TABLE_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS name, "
    "CASE c.relkind "
    "WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' "
    "WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' WHEN 'f' THEN 'foreign' "
    "END AS kind "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') "
    "AND n.nspname <> 'information_schema' "
    "AND LEFT(n.nspname, 3) <> 'pg_'"
)


def build_resolve_table_query(
    allowed_schemas: tuple[str, ...],
    table: str,
    schema: str | None = None,
) -> tuple[str, list[object]]:
    """Build the query resolving a table name to (schema, name, kind)."""
    conditions = ["c.relname = %s"]
    params: list[object] = [table]
    if schema is not None:
        conditions.append("n.nspname = %s")
        params.append(schema)
    if allowed_schemas:
        conditions.append("n.nspname = ANY(%s)")
        params.append(list(allowed_schemas))
    query = _RESOLVE_TABLE_SQL + " AND " + " AND ".join(conditions)
    return query + " ORDER BY n.nspname", params


_DESCRIBE_COLUMNS_SQL = (
    "SELECT a.attname AS name, a.attnum::int AS attnum, "
    "pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type, "
    "NOT a.attnotnull AS nullable, "
    "pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default, "
    "(a.attnum = ANY(COALESCE(pk.conkey, '{}'::int2[]))) AS is_primary_key, "
    "pg_catalog.col_description(a.attrelid, a.attnum) AS comment "
    "FROM pg_catalog.pg_attribute a "
    "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "LEFT JOIN pg_catalog.pg_attrdef d "
    "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    "LEFT JOIN pg_catalog.pg_constraint pk "
    "ON pk.conrelid = a.attrelid AND pk.contype = 'p' "
    "WHERE n.nspname = %s AND c.relname = %s "
    "AND a.attnum > 0 AND NOT a.attisdropped"
)


def build_describe_columns_query(
    schema: str, table: str
) -> tuple[str, list[object]]:
    """Build the query listing the columns of a resolved relation."""
    return _DESCRIBE_COLUMNS_SQL + " ORDER BY a.attnum", [schema, table]
