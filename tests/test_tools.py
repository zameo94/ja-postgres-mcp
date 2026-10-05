"""Tests for the MCP tools and their wiring (no real database)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from mcp import Client
from psycopg import OperationalError

from ja_pst_mcp.config import DatabaseSettings, QuerySettings, Settings
from ja_pst_mcp.database import (
    DatabaseConnectionError,
    DatabaseError,
    InvalidQueryError,
    QueryResult,
)
from ja_pst_mcp.discovery import (
    build_describe_columns_query,
    build_list_schemas_query,
    build_list_tables_query,
    build_resolve_table_query,
)
from ja_pst_mcp.pagination import cursor_scope, decode_cursor, encode_cursor
from ja_pst_mcp.server import create_server
from ja_pst_mcp.tools import (
    DATABASE_OPERATION_MESSAGE,
    DATABASE_UNAVAILABLE_MESSAGE,
)


class FakeDatabase:
    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        ping_error: BaseException | None = None,
        query_result: QueryResult | None = None,
        query_results: list[QueryResult] | None = None,
        query_error: BaseException | None = None,
        raise_on_query_call: int | None = None,
    ) -> None:
        self.settings = settings
        self._ping_error = ping_error
        self._query_result = query_result
        self._query_results = (
            deque(query_results) if query_results is not None else None
        )
        self._query_error = query_error
        self._raise_on_query_call = raise_on_query_call
        self.opened = False
        self.closed = False
        self.ping_count = 0
        self.calls: list[tuple[str, Any]] = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> None:
        if self._ping_error is not None:
            raise self._ping_error
        self.ping_count += 1

    async def fetch_rows(
        self, query: str, params: Any = None, *, max_rows: int | None = None
    ) -> QueryResult:
        self.calls.append((query, params))
        if self._query_error is not None and (
            self._raise_on_query_call is None
            or len(self.calls) == self._raise_on_query_call
        ):
            raise self._query_error
        if self._query_results is not None:
            assert self._query_results, "no more fake query results"
            return self._query_results.popleft()
        assert self._query_result is not None
        return self._query_result


def make_factory(
    created: dict[str, FakeDatabase], **database_kwargs: Any
) -> Callable[[DatabaseSettings, QuerySettings], FakeDatabase]:
    def factory(
        database_settings: DatabaseSettings, query_settings: QuerySettings
    ) -> FakeDatabase:
        created["database"] = FakeDatabase(database_settings, **database_kwargs)
        return created["database"]

    return factory


async def test_db_health_tool_over_in_memory_client(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(settings, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "db_health")
        assert tool.title == "Database health"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is False

        result = await client.call_tool("db_health", {})

        assert result.is_error is False
        assert result.structured_content == {"status": "ok"}
        assert created["database"].settings == settings.database
        assert created["database"].ping_count == 1

    assert created["database"].opened is True
    assert created["database"].closed is True


async def test_db_health_maps_connection_failure(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseConnectionError(
        'connection to server at "db.example" (10.0.0.1), port 5432 failed'
    )
    server = create_server(settings, database_factory=make_factory(created, ping_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_UNAVAILABLE_MESSAGE in text
    assert "db.example" not in text
    assert "10.0.0.1" not in text


async def test_db_health_maps_operation_failure(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseError("serialization failure")
    server = create_server(settings, database_factory=make_factory(created, ping_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_health", {})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_OPERATION_MESSAGE in text
    assert "serialization failure" not in text


async def test_db_health_does_not_mask_unexpected_errors(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = OperationalError('connection to server at "db.example", port 5432 failed')
    server = create_server(settings, database_factory=make_factory(created, ping_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_health", {})

    assert result.is_error is True
    assert result.content[0].text == "Error executing tool db_health"
    assert "db.example" not in result.content[0].text


async def test_db_run_read_only_query_returns_structured_result(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    query_result = QueryResult(
        columns=("n", "label"), rows=((1, "x"),), row_count=1, truncated=False
    )
    server = create_server(
        settings, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(
            item for item in listing.tools if item.name == "db_run_read_only_query"
        )
        assert tool.title == "Run read-only query"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

        result = await client.call_tool(
            "db_run_read_only_query", {"sql": "SELECT 1 AS n, 'x' AS label"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "columns": ["n", "label"],
        "rows": [[1, "x"]],
        "row_count": 1,
        "truncated": False,
    }
    assert created["database"].calls == [("SELECT 1 AS n, 'x' AS label", None)]


async def test_db_run_read_only_query_passes_params(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    query_result = QueryResult(columns=("n",), rows=((1,),), row_count=1, truncated=False)
    server = create_server(
        settings, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "db_run_read_only_query",
            {
                "sql": "SELECT %(value)s AS n",
                "params": {"value": 1},
            },
        )

    assert created["database"].calls == [("SELECT %(value)s AS n", {"value": 1})]


async def test_db_run_read_only_query_reports_invalid_query(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = InvalidQueryError("only a single statement is allowed")
    server = create_server(settings, database_factory=make_factory(created, query_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_run_read_only_query", {"sql": "SELECT 1; SELECT 2"}
        )

    assert result.is_error is True
    assert "only a single statement is allowed" in result.content[0].text


async def test_db_run_read_only_query_maps_database_error(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    error = DatabaseError("statement timeout")
    server = create_server(settings, database_factory=make_factory(created, query_error=error))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_run_read_only_query", {"sql": "SELECT 1"})

    assert result.is_error is True
    text = result.content[0].text
    assert DATABASE_OPERATION_MESSAGE in text
    assert "statement timeout" not in text


def test_build_list_schemas_query_uses_limit_without_allowlist() -> None:
    query, params = build_list_schemas_query((), 200)

    assert params == [201]
    assert query.count("%s") == 1
    assert "ORDER BY schema_name" in query


def test_build_list_schemas_query_binds_allowlist_and_cursor() -> None:
    query, params = build_list_schemas_query(("public", "sales"), 50, ("b",))

    assert params == [["public", "sales"], "b", 51]
    assert "schema_name > %s" in query


async def test_db_list_schemas_returns_schemas(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    query_result = QueryResult(
        columns=("name", "owner"),
        rows=(("public", "postgres"), ("sales", "app")),
        row_count=2,
        truncated=False,
    )
    server = create_server(
        settings, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "db_list_schemas")
        assert tool.title == "List schemas"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

        result = await client.call_tool("db_list_schemas", {})

    assert result.is_error is False
    assert result.structured_content == {
        "schemas": [
            {"name": "public", "owner": "postgres"},
            {"name": "sales", "owner": "app"},
        ],
        "next_cursor": None,
        "row_count": 2,
    }


async def test_db_list_schemas_paginates(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    configured = replace(
        settings,
        query=QuerySettings(discovery_page_size=2, discovery_max_page_size=10),
    )
    query_result = QueryResult(
        columns=("name", "owner"),
        rows=(("a", "o"), ("b", "o"), ("c", "o")),
        row_count=3,
        truncated=False,
    )
    server = create_server(
        configured, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_schemas", {})

    content = result.structured_content
    assert [schema["name"] for schema in content["schemas"]] == ["a", "b"]
    assert content["row_count"] == 2
    assert decode_cursor(
        content["next_cursor"], 1, cursor_scope("db_list_schemas", {})
    ) == ("b",)


async def test_db_list_schemas_passes_allowlist(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    configured = replace(settings, query=QuerySettings(allowed_schemas=("public",)))
    server = create_server(
        configured,
        database_factory=make_factory(
            created,
            query_result=QueryResult(
                columns=("name", "owner"), rows=(), row_count=0, truncated=False
            ),
        ),
    )

    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool("db_list_schemas", {})

    query, params = created["database"].calls[0]
    assert "= ANY(%s)" in query
    assert params[0] == ["public"]


def test_build_list_tables_query_uses_limit_without_filters() -> None:
    query, params = build_list_tables_query((), 200)

    assert params == [201]
    assert query.count("%s") == 1


def test_build_list_tables_query_binds_filters_and_cursor() -> None:
    query, params = build_list_tables_query(
        ("public",), 200, schema="public", kind="table", cursor=("public", "orders")
    )

    assert params == [["public"], "public", ["r", "p"], "public", "orders", 201]
    assert query.count("%s") == 6
    assert "(n.nspname, c.relname) > (%s, %s)" in query


async def test_db_list_tables_returns_tables(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    query_result = QueryResult(
        columns=("schema_name", "name", "kind", "estimated_rows"),
        rows=(
            ("public", "customers", "table", 42),
            ("public", "orders", "table", None),
        ),
        row_count=2,
        truncated=False,
    )
    server = create_server(
        settings, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "db_list_tables")
        assert tool.title == "List tables"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

        result = await client.call_tool("db_list_tables", {})

    assert result.is_error is False
    assert result.structured_content == {
        "tables": [
            {
                "schema_name": "public",
                "name": "customers",
                "kind": "table",
                "estimated_rows": 42,
            },
            {
                "schema_name": "public",
                "name": "orders",
                "kind": "table",
                "estimated_rows": None,
            },
        ],
        "next_cursor": None,
        "row_count": 2,
    }


async def test_db_list_tables_paginates(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    configured = replace(
        settings,
        query=QuerySettings(discovery_page_size=1, discovery_max_page_size=10),
    )
    query_result = QueryResult(
        columns=("schema_name", "name", "kind", "estimated_rows"),
        rows=(("public", "a", "table", None), ("public", "b", "table", None)),
        row_count=2,
        truncated=False,
    )
    server = create_server(
        configured, database_factory=make_factory(created, query_result=query_result)
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {})

    content = result.structured_content
    assert [table["name"] for table in content["tables"]] == ["a"]
    assert content["row_count"] == 1
    assert decode_cursor(
        content["next_cursor"],
        2,
        cursor_scope("db_list_tables", {"schema": None, "kind": None}),
    ) == ("public", "a")


async def test_db_list_tables_rejects_cursor_from_other_scope(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(settings, database_factory=make_factory(created))
    wrong_scope = cursor_scope("db_list_tables", {"schema": "sales", "kind": None})
    cursor = encode_cursor(("public", "a"), wrong_scope)

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {"cursor": cursor})

    assert result.is_error is True
    assert "does not match" in result.content[0].text
    assert created["database"].calls == []


async def test_db_list_tables_rejects_invalid_page_size(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(settings, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {"page_size": 5000})

    assert result.is_error is True
    assert "page_size" in result.content[0].text


async def test_db_list_tables_rejects_invalid_cursor(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(settings, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {"cursor": "not-a-cursor"})

    assert result.is_error is True
    assert "cursor" in result.content[0].text


async def test_db_list_tables_maps_database_error(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(
        settings,
        database_factory=make_factory(created, query_error=DatabaseError("boom")),
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {})

    assert result.is_error is True
    assert DATABASE_OPERATION_MESSAGE in result.content[0].text
    assert "boom" not in result.content[0].text


async def test_db_list_tables_does_not_mask_unexpected_errors(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(
        settings,
        database_factory=make_factory(
            created,
            query_error=OperationalError('connection to server at "db.example" failed'),
        ),
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {})

    assert result.is_error is True
    assert result.content[0].text == "Error executing tool db_list_tables"
    assert "db.example" not in result.content[0].text


async def test_db_list_tables_rejects_schema_outside_allowlist(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    configured = replace(settings, query=QuerySettings(allowed_schemas=("public",)))
    server = create_server(configured, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_list_tables", {"schema": "sales"})

    assert result.is_error is True
    assert "not in the allowed schemas" in result.content[0].text
    assert created["database"].calls == []


def test_build_resolve_table_query_with_schema() -> None:
    query, params = build_resolve_table_query((), "t", "public")

    assert params == ["t", "public"]
    assert "c.relname = %s" in query
    assert "n.nspname = %s" in query


def test_build_resolve_table_query_with_allowlist() -> None:
    query, params = build_resolve_table_query(("public",), "t")

    assert params == ["t", ["public"]]


def test_build_describe_columns_query_binds_schema_and_table() -> None:
    query, params = build_describe_columns_query("public", "t")

    assert params == ["public", "t"]
    assert query.count("%s") == 2


def _describe_query_results(resolve_rows, column_rows):
    return [
        QueryResult(
            columns=("schema_name", "name", "kind"),
            rows=resolve_rows,
            row_count=len(resolve_rows),
            truncated=False,
        ),
        QueryResult(
            columns=(
                "name",
                "attnum",
                "data_type",
                "nullable",
                "default",
                "is_primary_key",
                "comment",
            ),
            rows=column_rows,
            row_count=len(column_rows),
            truncated=False,
        ),
    ]


async def test_db_describe_table_returns_columns(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    results = _describe_query_results(
        (("public", "customers", "table"),),
        (
            ("id", 1, "integer", False, None, True, None),
            ("name", 2, "text", True, "'x'::text", False, "display name"),
        ),
    )
    server = create_server(
        settings, database_factory=make_factory(created, query_results=results)
    )

    async with Client(server, raise_exceptions=True) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "db_describe_table")
        assert tool.title == "Describe table"
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

        result = await client.call_tool("db_describe_table", {"table": "customers"})

    assert result.is_error is False
    assert result.structured_content == {
        "schema_name": "public",
        "name": "customers",
        "kind": "table",
        "columns": [
            {
                "name": "id",
                "attnum": 1,
                "data_type": "integer",
                "nullable": False,
                "default": None,
                "is_primary_key": True,
                "comment": None,
            },
            {
                "name": "name",
                "attnum": 2,
                "data_type": "text",
                "nullable": True,
                "default": "'x'::text",
                "is_primary_key": False,
                "comment": "display name",
            },
        ],
        "primary_key": ["id"],
    }


async def test_db_describe_table_reports_missing_table(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    results = _describe_query_results((), ())
    server = create_server(
        settings, database_factory=make_factory(created, query_results=results)
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "nope"})

    assert result.is_error is True
    assert "not found" in result.content[0].text


async def test_db_describe_table_reports_ambiguous_table(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    results = _describe_query_results(
        (("a", "t", "table"), ("b", "t", "table")), ()
    )
    server = create_server(
        settings, database_factory=make_factory(created, query_results=results)
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "t"})

    assert result.is_error is True
    assert "multiple schemas" in result.content[0].text


async def test_db_describe_table_rejects_schema_outside_allowlist(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    configured = replace(settings, query=QuerySettings(allowed_schemas=("public",)))
    server = create_server(configured, database_factory=make_factory(created))

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "db_describe_table", {"table": "t", "schema": "sales"}
        )

    assert result.is_error is True
    assert "not in the allowed schemas" in result.content[0].text
    assert created["database"].calls == []


async def test_db_describe_table_reports_missing_columns(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    results = _describe_query_results((("public", "t", "table"),), ())
    server = create_server(
        settings, database_factory=make_factory(created, query_results=results)
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "t"})

    assert result.is_error is True
    assert "not found" in result.content[0].text


async def test_db_describe_table_bounds_ambiguous_schemas(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    rows = tuple((f"s{index}", "t", "table") for index in range(8))
    results = _describe_query_results(rows, ())
    server = create_server(
        settings, database_factory=make_factory(created, query_results=results)
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "t"})

    text = result.content[0].text
    assert "multiple schemas" in text
    assert "s4" in text
    assert "s5" not in text
    assert "(+3 more)" in text


async def test_db_describe_table_maps_database_error(settings: Settings) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(
        settings,
        database_factory=make_factory(created, query_error=DatabaseError("boom")),
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "t"})

    assert result.is_error is True
    assert DATABASE_OPERATION_MESSAGE in result.content[0].text
    assert "boom" not in result.content[0].text


async def test_db_describe_table_does_not_mask_unexpected_errors(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    server = create_server(
        settings,
        database_factory=make_factory(
            created,
            query_error=OperationalError('connection to server at "db.example" failed'),
        ),
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "t"})

    assert result.is_error is True
    assert result.content[0].text == "Error executing tool db_describe_table"


async def test_db_describe_table_maps_error_on_second_query(
    settings: Settings,
) -> None:
    created: dict[str, FakeDatabase] = {}
    results = [
        QueryResult(
            ("schema_name", "name", "kind"), (("public", "t", "table"),), 1, False
        )
    ]
    server = create_server(
        settings,
        database_factory=make_factory(
            created,
            query_results=results,
            query_error=DatabaseError("boom"),
            raise_on_query_call=2,
        ),
    )

    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("db_describe_table", {"table": "t"})

    assert result.is_error is True
    assert DATABASE_OPERATION_MESSAGE in result.content[0].text
