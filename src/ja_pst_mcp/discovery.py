"""Discovery tool models and SQL builders."""

from __future__ import annotations

from pydantic import BaseModel


class SchemaInfo(BaseModel):
    """A database schema."""

    name: str
    owner: str


class SchemaListOutput(BaseModel):
    """Result of ``db_list_schemas``."""

    schemas: list[SchemaInfo]
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
) -> tuple[str, list[str] | None]:
    """Build the schema-listing SQL and its parameters (allowlist optional)."""
    query = _LIST_SCHEMAS_SQL
    params: list[str] | None = None
    if allowed_schemas:
        query += " AND schema_name = ANY(%s)"
        params = list(allowed_schemas)
    return query + " ORDER BY schema_name", params
