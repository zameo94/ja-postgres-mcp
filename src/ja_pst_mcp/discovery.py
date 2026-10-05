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
    """A page of ``db_list_schemas`` results.

    ``next_cursor`` is ``None`` on the last page; otherwise pass it back to get
    the next page.
    """

    schemas: list[SchemaInfo]
    next_cursor: str | None
    row_count: int


class TableInfo(BaseModel):
    """A table, view, materialized view or foreign table."""

    schema_name: str
    name: str
    kind: TableKind
    estimated_rows: int | None


class TableListOutput(BaseModel):
    """A page of ``db_list_tables`` results."""

    tables: list[TableInfo]
    next_cursor: str | None
    row_count: int


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
    """Result of ``db_describe_table`` (one object, not paginated)."""

    schema_name: str
    name: str
    kind: TableKind
    columns: list[ColumnInfo]
    primary_key: list[str]


# System schemas (``pg_*`` and ``information_schema``) are excluded by default.
_LIST_SCHEMAS_SQL = (
    "SELECT schema_name AS name, schema_owner AS owner "
    "FROM information_schema.schemata "
    "WHERE LEFT(schema_name, 3) <> 'pg_' "
    "AND schema_name <> 'information_schema'"
)


def build_list_schemas_query(
    allowed_schemas: tuple[str, ...],
    page_size: int,
    cursor: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Build one keyset page of schemas ordered by name."""
    conditions: list[str] = []
    params: list[object] = []
    if allowed_schemas:
        conditions.append("schema_name = ANY(%s)")
        params.append(list(allowed_schemas))
    if cursor is not None:
        conditions.append("schema_name > %s")
        params.append(cursor[0])

    query = _LIST_SCHEMAS_SQL
    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY schema_name LIMIT %s"
    params.append(page_size + 1)
    return query, params


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
    page_size: int,
    schema: str | None = None,
    kind: TableKind | None = None,
    cursor: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Build one keyset page of tables ordered by (schema, name)."""
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
    if cursor is not None:
        conditions.append("(n.nspname, c.relname) > (%s, %s)")
        params.extend([cursor[0], cursor[1]])

    query = _LIST_TABLES_SQL
    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY n.nspname, c.relname LIMIT %s"
    params.append(page_size + 1)
    return query, params


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
