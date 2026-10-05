# AGENTS.md

Guidance for AI coding agents working in `ja-pst-mcp`.

## What this repository is

An MCP server (Python 3.12, Docker, PostgreSQL) consumed by the separate
`ja-pst-mcp-client` application over MCP.

This repository owns: MCP server, MCP tools, PostgreSQL access, configuration,
connection management, input validation, error handling, logging, security,
tests, Docker and essential documentation.

This repository does **not** own the web application layer. Do not add React,
FastAPI, HTTP APIs, an Agent, an MCP Client, conversation handling,
application authentication or `/api/...` endpoints — those belong to
`ja-pst-mcp-client`.

## Quality bar

- This is an MVP in **scope**, not in **quality**. MVP = fewer features, not
  worse implementations.
- Expect senior-level engineering: clear contracts, correct typing, separated
  responsibilities, real security, adequate tests and correct lifecycle.
- Do **not** deliberately introduce technical debt to reach a demo. If a
  capability is a natural part of the architecture (e.g. SQL parameterization,
  bounded memory), design and build it correctly now instead of a throwaway
  version.
- At the same time, do not implement speculative features with no concrete
  requirement (no reverse-YAGNI). Keep the design open so it can extend.

## Product and domain direction

- `pst` = **PostgreSQL**. This is a **generic PostgreSQL MCP**, not a vertical
  (no legal/BI/CRM domain baked in).
- **The database is the domain.** Tables, columns, constraints and data are
  discovered **at runtime**; never hardcode application entities (no
  `clients`, `invoices`, etc.). The demo database is only a test fixture.
- The MVP is **read-only**. It must let a model answer real business questions
  against an arbitrary PostgreSQL database:
  `metadata discovery -> schema understanding -> read-only SQL -> result`.
- Two tool families, kept distinct:
  - **discovery** tools: help the model understand the DB (schemas, tables,
    columns, constraints, indexes, views);
  - **analysis** tools: read-only SQL/aggregations to answer questions.
- Naming: analysis/discovery tools use the `db_` prefix (e.g.
  `db_run_read_only_query`, `db_list_tables`).
- No `INSERT`/`UPDATE`/`DELETE`/DDL: writing is out of MVP scope.

### Read-only query policy (MVP defaults)

- `JA_PST_MAX_ROWS=200`, `JA_PST_STATEMENT_TIMEOUT=5`, `JA_PST_LOCK_TIMEOUT=5`.
- `JA_PST_ALLOWED_SCHEMAS` is optional and consumed by the **discovery** tools
  (schema/table listing); arbitrary read-only queries are not filtered by it.
  Discovery excludes system schemas (`pg_catalog`, `information_schema`,
  `pg_toast`) by default; empty allowlist means all non-system schemas.
- Every pooled connection is `default_transaction_read_only=on`; analysis query
  timeouts are set locally per transaction.
- The connection pool is configured via `JA_PST_DB_POOL_MIN`,
  `JA_PST_DB_POOL_MAX` and `JA_PST_DB_POOL_TIMEOUT`; it bounds how many queries
  run concurrently (each `fetch_rows` checks out its own connection).
- Analysis queries run through a **server-side cursor** (`DECLARE ... CURSOR`),
  which uses the extended protocol, so **multiple statements are structurally
  impossible** and rows are fetched in batches with bounded client memory.
- A PostgreSQL **least-privilege, read-only role** is the real security
  boundary and is required in deployment (see Security requirements).

### MVP roadmap (build order)

1. Read-only query foundation (limits config + `Database.fetch_rows`). **Done.**
2. `db_run_read_only_query` (core analysis tool) and `db_health`. **Done.**
3. Discovery tools (in progress): `db_list_schemas`, `db_list_tables` **done**;
   next `db_describe_table`, constraints/relationships/indexes,
   `db_get_view_definition`.
4. `db_preview_table`.

## Query engine design (MVP)

- `db_run_read_only_query` takes `sql: str` and optional `params` (positional
  list or named mapping). Values always go through the driver's
  parameterization; **never** string interpolation.
- Multiple statements are **structurally impossible**: queries run through a
  psycopg **server-side cursor** (`DECLARE ... CURSOR`), which uses the extended
  protocol. There is no hand-written SQL parser acting as a security control.
- Result model: `columns` plus positional `rows` (value tuples aligned by
  index), so **duplicate column names never collapse**, plus `row_count`
  (rows actually returned) and `truncated`.
- Serialization contract (see `QueryResult` docstring): `str`/`int`/`float`/
  `bool`/`null` pass through; `json`/`jsonb` → `dict`/`list`; `bytes` → hex;
  `Decimal` → `str` (exactness); `date`/`datetime` → ISO 8601; other → `str`.
- Four distinct concerns, kept separate:
  - **safety limit** — `max_rows` + `truncated`;
  - **DB/result streaming** — server-side cursor fetches rows in batches from
    the server (bounded client memory); closing the cursor releases the portal;
  - **pagination** — not exposed for arbitrary SQL; the model writes
    `LIMIT`/`OFFSET` itself;
  - **transport streaming** — MCP returns one tool result; no row streaming and
    no custom SSE. `Context.report_progress` is the only progress channel.
- Caching is **not** implemented now (nondeterministic queries, staleness,
  invalidation); `fetch_rows` stays stateless so a cache can wrap it later.
- `QuerySettings` is the single policy object: the lifespan passes the **same**
  instance to `Database` (execution limits: `max_rows`/timeouts) and to
  `AppContext` (tool policy: allowed schemas), so there is one source of truth.
- Discovery tools live in `discovery.py` (models + SQL builders); `tools.py`
  holds only MCP handlers and registration. Discovery outputs carry a
  `truncated` flag so a partial list is never silent.

## Security requirements (non-negotiable)

- Treat every model-generated query as **hostile input**.
- It must be **technically impossible** to mutate the database: `INSERT`,
  `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `CREATE`, `ALTER`, `DROP`, `GRANT`,
  `REVOKE`, `COMMENT`, `VACUUM`, `CALL`, DDL, permission changes, and
  side-effecting functions/procedures.
- Defence in depth, each layer independent:
  `MCP input -> tool validation -> parameterization -> extended protocol
  (single statement) -> READ ONLY transaction -> least-privilege role ->
  PostgreSQL`.
- PostgreSQL (READ ONLY + role privileges) is the **real** security boundary,
  not the Python code.
- The MCP role is least privilege: `CONNECT`, `USAGE` on needed schemas,
  `SELECT` on needed relations, no write/DDL, no membership in privileged
  roles, and no `EXECUTE` on dangerous functions/extensions (`dblink`,
  `postgres_fdw` writes, `COPY ... PROGRAM`, large-object file functions, ...).
  Read-only does **not** make every `SELECT` safe by itself.
- No SQL injection surface in our code: dynamic values only via driver params.
- Never return sensitive data to the client; keep server logs sanitized.

## MCP server

- The server is a standalone service exposed over **MCP Streamable HTTP**
  (`transport="streamable-http"`). The HTTP endpoint is solely the MCP
  transport; do **not** add a custom REST API or non-MCP routes.
- The client is not spawned as a subprocess: do not use stdio as the transport.

## PostgreSQL compatibility

- The project targets **PostgreSQL 15 and newer**.
- Use SQL and PostgreSQL APIs compatible with every supported version (15+).
- Do **not** scatter PostgreSQL-version conditionals across the codebase.
- If a tool needs a feature introduced in a newer PostgreSQL version, isolate
  that requirement inside that specific tool/database implementation and
  document the minimum required version next to it.
- The health check stays a basic connectivity/health check. PostgreSQL version
  detection may be surfaced as diagnostic information but must not make the
  health check unnecessarily complex.
- The supported version range is documented in the README (at documentation
  stage) and covered by integration tests, with a PostgreSQL 15 baseline,
  wherever version-specific behavior exists.
- When adding a database-related MCP tool, explicitly verify that its SQL and
  PostgreSQL APIs work on the minimum supported version (15).

## Non-negotiable workflow

1. Work in **small, independent, reviewable steps** (a single responsibility per step).
2. For each step: explain the goal, implement only that step, add/update tests.
3. **Do NOT run tests** (`pytest`, runners, integration tests, Docker tests). Write them; the developer executes them. A one-off run explicitly requested by the developer is an **exception**, not a policy change; the agent otherwise prepares tests and reports the commands to run.
4. **Do NOT build or run Docker** (`docker build`, `docker run`). Configure only.
5. After each step, stop and wait for explicit approval before the next step.
6. Never claim tests pass if they were not executed.
7. Do **not** modify `README.md` during implementation; documentation is done at the end.

## Git rules

- **Never** run `git commit`, `git add`, `git status` or `git push`. The developer performs all git operations.
- Branch naming style: `feature/<short-name>` (e.g. `feature/add-configuration`).
- Do not create commits, amend history or push.

## Configuration and security

- All environment-specific or sensitive config comes from environment variables.
- All sensitive variables live in a local `.env` file, which is gitignored.
  - A committed `.env.example` documents every variable with placeholder values.
  - The developer copies `.env.example` to `.env` and fills in real values.
  - Configuration is read from `.env`, with real environment variables taking precedence.
- Never hardcode or commit credentials, passwords or connection strings.
- Never use raw string concatenation for SQL; use parameterized queries.
- Never expose arbitrary SQL execution as a tool without explicit approval.
- Never return sensitive data to the MCP client (passwords, connection strings,
  driver messages, stack traces, infrastructure details). Expected database
  errors are wrapped and exposed as a generic, safe message.
- Server logs stay diagnostic but sanitized: never log passwords, secrets,
  tokens or full DSNs. Expected database errors are logged (with cause) for
  debugging; unexpected errors keep their full traceback.

## Error handling

- Expected database/pool errors are wrapped in `DatabaseError` /
  `DatabaseConnectionError` and mapped to a safe `ToolError` at the MCP tool
  boundary. The original driver error is preserved as `__cause__` for server
  logs, never for the client.
- Unexpected application errors are not masked as database errors; the SDK
  logs their full traceback and the client receives only a generic message.

## Commands

```
# With uv
uv sync
uv run pytest

# With Poetry
poetry install
poetry run pytest

# Tests (developer-run only; agents must not execute)
# Unit + integration run together; the integration tests provision a throwaway
# PostgreSQL 15 automatically via testcontainers (Docker required).
pytest
```

## Project structure

```
src/ja_pst_mcp/    # package
tests/             # pytest tests
.github/workflows/ # CI
pyproject.toml     # project + tooling config
.env.example       # documented environment variables (committed)
.env               # real values (gitignored)
```

## Conventions

- Python 3.12, type hints, small functions, single responsibilities.
- Modern SPDX license header not required in source files.
- Keep runtime dependencies minimal; add a dependency only in the step that uses it.
- No comments unless necessary; prefer readable code and explicit names.
