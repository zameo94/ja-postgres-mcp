"""Discovery tool models and SQL builders."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

TableKind = Literal["table", "view", "matview", "foreign"]
ConstraintKind = Literal["primary_key", "unique", "foreign_key", "check", "exclusion"]

# Shared CASE mapping pg_class.relkind to the public ``kind`` vocabulary.
_REL_KIND_CASE = (
    "CASE c.relkind "
    "WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' "
    "WHEN 'v' THEN 'view' WHEN 'm' THEN 'matview' WHEN 'f' THEN 'foreign' "
    "END"
)


class SchemaInfo(BaseModel):
    """A database schema."""

    name: str
    owner: str


class SchemaListOutput(BaseModel):
    """A page of ``db_list_schemas`` results."""

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


class ConstraintInfo(BaseModel):
    """A table constraint."""

    schema_name: str
    table_name: str
    name: str
    kind: ConstraintKind
    definition: str


class ConstraintListOutput(BaseModel):
    """A page of ``db_list_constraints`` results."""

    constraints: list[ConstraintInfo]
    next_cursor: str | None
    row_count: int


class RelationshipInfo(BaseModel):
    """A foreign-key relationship."""

    name: str
    source_schema: str
    source_table: str
    target_schema: str
    target_table: str
    definition: str


class RelationshipListOutput(BaseModel):
    """A page of ``db_list_relationships`` results."""

    relationships: list[RelationshipInfo]
    next_cursor: str | None
    row_count: int


class IndexInfo(BaseModel):
    """An index."""

    schema_name: str
    table_name: str
    name: str
    method: str
    is_unique: bool
    is_primary: bool
    columns: list[str]
    definition: str


class IndexListOutput(BaseModel):
    """A page of ``db_list_indexes`` results."""

    indexes: list[IndexInfo]
    next_cursor: str | None
    row_count: int


class ViewDefinitionOutput(BaseModel):
    """Result of ``db_get_view_definition`` (one object, not paginated)."""

    schema_name: str
    name: str
    kind: TableKind
    definition: str


class PreviewOutput(BaseModel):
    """A page of ``db_preview_table`` rows."""

    columns: list[str]
    rows: list[list[Any]]
    next_cursor: str | None
    row_count: int


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

_LIST_TABLES_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS name, "
    f"{_REL_KIND_CASE} AS kind, "
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


def resolve_conditions(
    allowed_schemas: tuple[str, ...],
    name: str,
    schema: str | None = None,
) -> tuple[list[str], list[object]]:
    """Build the shared conditions resolving a relation by (optional) schema."""
    conditions = ["c.relname = %s"]
    params: list[object] = [name]
    if schema is not None:
        conditions.append("n.nspname = %s")
        params.append(schema)
    if allowed_schemas:
        conditions.append("n.nspname = ANY(%s)")
        params.append(list(allowed_schemas))
    return conditions, params


_RESOLVE_TABLE_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS name, "
    f"{_REL_KIND_CASE} AS kind "
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
    conditions, params = resolve_conditions(allowed_schemas, table, schema)
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


# ``conparentid = 0`` excludes the per-partition clones of partitioned constraints.
_CONSTRAINTS_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS table_name, con.conname AS name, "
    "CASE con.contype WHEN 'p' THEN 'primary_key' WHEN 'u' THEN 'unique' "
    "WHEN 'f' THEN 'foreign_key' WHEN 'c' THEN 'check' WHEN 'x' THEN 'exclusion' "
    "END AS kind, "
    "pg_catalog.pg_get_constraintdef(con.oid) AS definition "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE con.contype IN ('p', 'u', 'f', 'c', 'x') AND con.conrelid <> 0 "
    "AND con.conparentid = 0 "
    "AND n.nspname <> 'information_schema' AND LEFT(n.nspname, 3) <> 'pg_'"
)


def build_list_constraints_query(
    allowed_schemas: tuple[str, ...],
    page_size: int,
    schema: str | None = None,
    table: str | None = None,
    cursor: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Build one keyset page of constraints ordered by (schema, table, name)."""
    conditions: list[str] = []
    params: list[object] = []
    if allowed_schemas:
        conditions.append("n.nspname = ANY(%s)")
        params.append(list(allowed_schemas))
    if schema is not None:
        conditions.append("n.nspname = %s")
        params.append(schema)
    if table is not None:
        conditions.append("c.relname = %s")
        params.append(table)
    if cursor is not None:
        conditions.append("(n.nspname, c.relname, con.conname) > (%s, %s, %s)")
        params.extend(cursor)
    query = _CONSTRAINTS_SQL
    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY n.nspname, c.relname, con.conname LIMIT %s"
    params.append(page_size + 1)
    return query, params


_RELATIONSHIPS_SQL = (
    "SELECT con.conname AS name, sn.nspname AS source_schema, "
    "sc.relname AS source_table, tn.nspname AS target_schema, "
    "tc.relname AS target_table, pg_catalog.pg_get_constraintdef(con.oid) AS definition "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class sc ON sc.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace sn ON sn.oid = sc.relnamespace "
    "JOIN pg_catalog.pg_class tc ON tc.oid = con.confrelid "
    "JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace "
    "WHERE con.contype = 'f' "
    "AND sn.nspname <> 'information_schema' AND LEFT(sn.nspname, 3) <> 'pg_' "
    "AND tn.nspname <> 'information_schema' AND LEFT(tn.nspname, 3) <> 'pg_'"
)


def build_list_relationships_query(
    allowed_schemas: tuple[str, ...],
    page_size: int,
    schema: str | None = None,
    table: str | None = None,
    cursor: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Build one keyset page of foreign keys ordered by source (schema, table, name)."""
    conditions: list[str] = []
    params: list[object] = []
    if allowed_schemas:
        conditions.append("sn.nspname = ANY(%s)")
        params.append(list(allowed_schemas))
    if schema is not None:
        conditions.append("sn.nspname = %s")
        params.append(schema)
    if table is not None:
        conditions.append("sc.relname = %s")
        params.append(table)
    if cursor is not None:
        conditions.append("(sn.nspname, sc.relname, con.conname) > (%s, %s, %s)")
        params.extend(cursor)
    query = _RELATIONSHIPS_SQL
    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY sn.nspname, sc.relname, con.conname LIMIT %s"
    params.append(page_size + 1)
    return query, params


# ``pg_get_indexdef(index, position, true)`` renders each key column or
# expression; ``indnkeyatts`` excludes INCLUDE columns.
_INDEXES_SQL = (
    "SELECT n.nspname AS schema_name, tc.relname AS table_name, ic.relname AS name, "
    "am.amname AS method, i.indisunique AS is_unique, i.indisprimary AS is_primary, "
    "COALESCE(cols.columns, ARRAY[]::text[]) AS columns, "
    "pg_catalog.pg_get_indexdef(i.indexrelid) AS definition "
    "FROM pg_catalog.pg_index i "
    "JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = tc.relnamespace "
    "JOIN pg_catalog.pg_am am ON am.oid = ic.relam "
    "LEFT JOIN LATERAL ("
    "SELECT array_agg("
    "pg_catalog.pg_get_indexdef(i.indexrelid, k.ord, true) ORDER BY k.ord"
    ") AS columns "
    "FROM generate_series(1, i.indnkeyatts) AS k(ord)"
    ") cols ON true "
    "WHERE n.nspname <> 'information_schema' AND LEFT(n.nspname, 3) <> 'pg_'"
)


def build_list_indexes_query(
    allowed_schemas: tuple[str, ...],
    page_size: int,
    schema: str | None = None,
    table: str | None = None,
    cursor: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Build one keyset page of indexes ordered by (schema, table, name)."""
    conditions: list[str] = []
    params: list[object] = []
    if allowed_schemas:
        conditions.append("n.nspname = ANY(%s)")
        params.append(list(allowed_schemas))
    if schema is not None:
        conditions.append("n.nspname = %s")
        params.append(schema)
    if table is not None:
        conditions.append("tc.relname = %s")
        params.append(table)
    if cursor is not None:
        conditions.append("(n.nspname, tc.relname, ic.relname) > (%s, %s, %s)")
        params.extend(cursor)
    query = _INDEXES_SQL
    if conditions:
        query += " AND " + " AND ".join(conditions)
    query += " ORDER BY n.nspname, tc.relname, ic.relname LIMIT %s"
    params.append(page_size + 1)
    return query, params


_VIEW_DEFINITION_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS name, "
    f"{_REL_KIND_CASE} AS kind, "
    "pg_catalog.pg_get_viewdef(c.oid, true) AS definition "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('v', 'm') "
    "AND n.nspname <> 'information_schema' AND LEFT(n.nspname, 3) <> 'pg_'"
)


def build_get_view_definition_query(
    allowed_schemas: tuple[str, ...],
    view: str,
    schema: str | None = None,
) -> tuple[str, list[object]]:
    """Build the query resolving a view/matview and returning its definition."""
    conditions, params = resolve_conditions(allowed_schemas, view, schema)
    query = _VIEW_DEFINITION_SQL + " AND " + " AND ".join(conditions)
    return query + " ORDER BY n.nspname", params


_PREVIEW_RESOLVE_SQL = (
    "SELECT n.nspname AS schema_name, c.relname AS name, "
    f"{_REL_KIND_CASE} AS kind, "
    "COALESCE(pk.columns, ARRAY[]::text[]) AS pk_columns "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "LEFT JOIN LATERAL ("
    "SELECT array_agg(a.attname ORDER BY k.ord) AS columns "
    "FROM pg_catalog.pg_constraint con "
    "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_catalog.pg_attribute a "
    "ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
    "WHERE con.conrelid = c.oid AND con.contype = 'p'"
    ") pk ON true "
    "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') "
    "AND n.nspname <> 'information_schema' AND LEFT(n.nspname, 3) <> 'pg_'"
)


def build_preview_resolve_query(
    allowed_schemas: tuple[str, ...],
    table: str,
    schema: str | None = None,
) -> tuple[str, list[object]]:
    """Build the query resolving a relation and its primary-key columns."""
    conditions, params = resolve_conditions(allowed_schemas, table, schema)
    query = _PREVIEW_RESOLVE_SQL + " AND " + " AND ".join(conditions)
    return query + " ORDER BY n.nspname", params


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_preview_rows_query(
    schema: str,
    table: str,
    pk_columns: tuple[str, ...],
    page_size: int,
    cursor: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Build one keyset page of rows ordered by the text form of the PK.

    The PK is rendered by PostgreSQL itself (``pk::text``) both in the keyset
    predicate and as leading ``__pk_*`` columns, so the cursor parts come back
    exactly as the server compares them (any PK type, default DateStyle). The
    first ``len(pk_columns)`` result columns are the key parts and must be
    stripped from the user-visible rows. The order is deterministic but
    lexicographic.
    """
    table_ref = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    pk_text = [f"{_quote_identifier(column)}::text" for column in pk_columns]
    select_key = ", ".join(
        f"{expression} AS {_quote_identifier(f'__pk_{index}')}"
        for index, expression in enumerate(pk_text)
    )
    params: list[object] = []
    where = ""
    if cursor is not None:
        placeholders = ", ".join(["%s"] * len(pk_columns))
        where = f" WHERE ({', '.join(pk_text)}) > ({placeholders})"
        params.extend(cursor)
    query = (
        f"SELECT {select_key}, * FROM {table_ref}{where} "
        f"ORDER BY {', '.join(pk_text)} LIMIT %s"
    )
    params.append(page_size + 1)
    return query, params
