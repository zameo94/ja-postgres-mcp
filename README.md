# ja-pst-mcp

The code in this repo was entirely written by a coding agent (mostly DeepSeek
V4.1 Flash). The idea, the architecture and the system design were under human
control.

A **generic PostgreSQL MCP server**. `pst` stands for PostgreSQL: this is not a
vertical (no legal/BI/CRM domain baked in). It connects to an **arbitrary,
already existing PostgreSQL 15+** database, discovers its schema at runtime and
lets a model answer real business questions with **read-only** SQL.

The **database is the domain**: tables, columns, constraints and data are
discovered at runtime and never hardcoded. The server is consumed by the separate
`ja-pst-mcp-client` application over MCP.

> **PostgreSQL 15 or newer.** The server targets PostgreSQL 15+; its SQL and
> catalog queries are compatible with version 15 and later.
>
> **MVP.** The goal is a solid, verifiable foundation, not a complete platform.
> Some capabilities are intentionally out of scope (see
> [MVP limitations](#mvp-limitations)). Every behavior is covered by tests.

---

## Table of contents

- [Project](#project)
- [First run](#first-run)
- [Connecting a client](#connecting-a-client)
- [MCP tools](#mcp-tools)
- [Query engine](#query-engine)
- [Security](#security)
- [Demo database](#demo-database)
- [Technology stack](#technology-stack)
- [Repository layout](#repository-layout)
- [Configuration](#configuration)
- [Tests](#tests)
- [MVP limitations](#mvp-limitations)
- [License](#license)

---

## Project

`ja-pst-mcp` is a **read-only PostgreSQL MCP server** exposed over **MCP
Streamable HTTP**. The model goes through a fixed flow:

```
metadata discovery -> schema understanding -> read-only SQL -> result
```

What it does:

- **Discovery** tools to understand the database at runtime: schemas, tables,
  views, columns, constraints, foreign keys and indexes.
- **Analysis** tools to answer questions: a single read-only SQL statement with
  aggregations, plus a keyset-paginated table preview.
- **Mandatory keyset pagination** for the listing tools, so the model can walk
  the whole database page after page without the server materializing it.
- **Defence in depth** for security: parameterization, extended protocol,
  `READ ONLY` transactions and a least-privilege PostgreSQL role.

What it does **not** do (in this MVP): no `INSERT`/`UPDATE`/`DELETE`/DDL, no
arbitrary SQL exposed as an unguarded tool, no caching, no custom REST API, and
it never creates database roles.

---

## First run

Requirements: **Docker** and Docker Compose; the target database must be
**PostgreSQL 15 or newer**. To run the tests you also need Docker (the
integration tests start a throwaway PostgreSQL 15).

```sh
cp .env.example .env
```

### Server + seeded demo database (easiest)

```sh
docker compose -f docker-compose.yaml -f docker-compose.demo.yaml up -d --build
```

This starts the MCP server **and** a PostgreSQL 15 seeded from
[`db/demo/seed.sql`](db/demo/seed.sql) (see [Demo database](#demo-database)).

| Service     | URL                                   |
| ----------- | ------------------------------------- |
| MCP server  | http://localhost:8000/mcp             |
| Demo DB     | `localhost:5432` (`JA_PST_DB_*` creds) |

### Server only (against an existing database)

```sh
docker compose up -d --build
```

This starts only the MCP server; point `JA_PST_DB_HOST` (and the rest of the
`JA_PST_DB_*` variables) in `.env` at your database. From inside the container,
a database on the host is reachable as `host.docker.internal` (Docker Desktop).

The seed runs **only on the first initialization** of the demo volume. To
re-seed from scratch, remove the `ja_pst_mcp_postgres_volume` volume.

### Local development (server outside Docker)

```sh
poetry install
poetry run pytest   # integration tests provision PostgreSQL 15 via testcontainers
```

---

## Connecting a client

The server speaks **MCP Streamable HTTP**; the client connects to the MCP
endpoint (default path `/mcp`) and does not spawn the server as a subprocess.

- Endpoint: `http://<host>:<JA_PST_SERVER_PORT>/mcp`.
- The bind address/port come from `JA_PST_SERVER_HOST` / `JA_PST_SERVER_PORT`.
- Behind a real hostname you must allow it: set `JA_PST_ALLOWED_HOSTS` (and, for
  browser clients, `JA_PST_ALLOWED_ORIGINS`). A non-localhost bind **without** an
  allowlist fails at startup (fail-closed).

The MCP server is a standalone service; there is **no custom REST API** — the
HTTP endpoint is solely the MCP transport.

---

## MCP tools

All tools are **read-only** (`read_only_hint=true`, `open_world_hint=false`) and
never mutate the database.

### Discovery

| Tool                      | Input                          | Output (page)                                                        |
| ------------------------- | ------------------------------ | -------------------------------------------------------------------- |
| `db_list_schemas`         | `page_size?`, `cursor?`        | `schemas` (`name`, `owner`), `next_cursor`, `row_count`              |
| `db_list_tables`          | `schema?`, `kind?`, `page_size?`, `cursor?` | `tables` (`schema_name`, `name`, `kind`, `estimated_rows`) |
| `db_describe_table`       | `table`, `schema?`             | `columns` (`name`, `attnum`, `data_type`, `nullable`, `default`, `is_primary_key`, `comment`), `primary_key` |
| `db_list_constraints`     | `schema?`, `table?`, `page_size?`, `cursor?` | `constraints` (`schema_name`, `table_name`, `name`, `kind`, `definition`) |
| `db_list_relationships`   | `schema?`, `table?`, `page_size?`, `cursor?` | `relationships` (`name`, `source_*`, `target_*`, `definition`) |
| `db_list_indexes`         | `schema?`, `table?`, `page_size?`, `cursor?` | `indexes` (`schema_name`, `table_name`, `name`, `method`, `is_unique`, `is_primary`, `columns`, `definition`) |
| `db_get_view_definition`  | `view`, `schema?`              | `schema_name`, `name`, `kind`, `definition`                          |

### Analysis

| Tool                      | Input                          | Output                                                               |
| ------------------------- | ------------------------------ | -------------------------------------------------------------------- |
| `db_run_read_only_query`  | `sql`, `params?`               | `columns`, `rows`, `row_count`, `truncated`                          |
| `db_preview_table`        | `table`, `schema?`, `page_size?`, `cursor?` | `columns`, `rows`, `next_cursor`, `row_count`            |

### Health

| Tool        | Input | Output          |
| ----------- | ----- | --------------- |
| `db_health` | none  | `{"status":"ok"}` |

Notes:

- `params` (for `db_run_read_only_query`) accepts a positional list (`%s`) or a
  named mapping (`%(name)s`); values are **always** bound by the driver, never
  interpolated.
- `db_preview_table` requires a primary key; rows are ordered by the **text form**
  of the key (deterministic but lexicographic).
- `estimated_rows` (in `db_list_tables`) is planner statistics from the last
  `ANALYZE`: it can be stale and is `null` when the relation was never analyzed.

---

## Query engine

Two **distinct result contracts**, never mixed:

- **Generic SQL** (`db_run_read_only_query`): `max_rows` + `truncated` — a
  safety-bounded result. The model writes `LIMIT`/`OFFSET` itself; the server
  does not rewrite arbitrary SQL.
- **Discovery** (`db_list_*`): **mandatory keyset pagination** via `page_size` +
  opaque `cursor`; the output is the tool's item list plus `row_count` (items in
  this page) and `next_cursor`. There is no `all` and no `truncated`: the
  consumer must request page after page.
  - Ordering is stable and deterministic (**no OFFSET**).
  - The cursor is **opaque** (`base64url(JSON)` with a format version); its
    fingerprint binds the tool and its keyset filters (not `page_size`), so a
    cursor reused with different filters is rejected.
  - Keyset avoids offset shift, but does **not** guarantee a global snapshot
    between separate requests (no long-lived transaction across MCP calls).
- **Exception**: `db_preview_table` returns row data (`columns`/`rows`) *and*
  discovery-style pagination (`next_cursor`/`row_count`).

Implementation details:

- Queries run through a psycopg **server-side cursor** (`DECLARE ... CURSOR`),
  which uses the extended protocol: **multiple statements are structurally
  impossible**, and rows are fetched in batches (bounded client memory).
- `statement_timeout` / `lock_timeout` are set locally per transaction; the DB
  layer also enforces an absolute row cap.
- Serialization: `str`/`int`/`float`/`bool`/`null` pass through; `json`/`jsonb`
  become `dict`/`list`; `bytes` become hex; `Decimal` becomes `str`
  (exactness); `date`/`datetime` become ISO 8601; everything else becomes `str`.

---

## Security

Every model-generated query is treated as **hostile input**. It must be
**technically impossible** to mutate the database (`INSERT`, `UPDATE`, `DELETE`,
`MERGE`, `TRUNCATE`, DDL, `GRANT`/`REVOKE`, side-effecting functions, ...).

Defence in depth, each layer independent:

```
MCP input -> tool validation -> parameterization -> extended protocol
(single statement) -> READ ONLY transaction -> least-privilege role -> PostgreSQL
```

- **PostgreSQL (READ ONLY + role privileges) is the real security boundary**, not
  the Python code.
- Every pooled connection is `default_transaction_read_only=on`.
- No SQL injection surface: dynamic values only via driver parameters.
- Sensitive data is never returned to the client; server logs stay sanitized.

### Least-privilege role

`db/roles/least_privilege.sql` is the reference script an operator runs **once**
as a DBA to create the read-only role the server connects with. It grants
`CONNECT`, `USAGE` on the listed schemas and `SELECT` on their tables (plus
default privileges for future tables) and nothing else. The server never creates
roles. A read-only transaction does **not** make every `SELECT` safe by itself,
so the role must not be a member of privileged roles and must not have `EXECUTE`
on dangerous functions/extensions (`dblink`, `postgres_fdw` writes,
`COPY ... PROGRAM`, large-object file functions, ...).

The integration tests connect as a least-privilege role and assert the role
boundary (can read, cannot write/DDL), independently of the `READ ONLY`
transaction.

---

## Demo database

`db/demo/seed.sql` is a **dev/test fixture** that creates a realistic schema in
the `demo` schema (`customers`, `products`, `orders`, `order_items`, `payments`,
indexes and a `monthly_revenue` view) with **deterministic fake data** (50
customers, 200 orders — some `pending`, 600 order items, 120 payments across
2025). It is only meant to try the tools manually.

The seed is **not** read by the tools and is **never assumed**: the MCP server
discovers whatever database it connects to.

The postgres image runs files in `/docker-entrypoint-initdb.d` only on the first
initialization, so the seed runs when the demo volume is created and is skipped
afterwards:

```sh
# server + seeded demo database
docker compose -f docker-compose.yaml -f docker-compose.demo.yaml up -d --build
```

The same seed is loaded into the test database by the integration suite, so a
few tests run against a realistic structure.

---

## Technology stack

- **Python 3.12**.
- **MCP Python SDK** (`mcp` 2.x) — Streamable HTTP server and tools.
- **psycopg 3** (async) with a **connection pool** (`psycopg_pool`) on
  **PostgreSQL 15+**.
- **pydantic** for the structured tool outputs.
- **python-dotenv** for local `.env` loading (real environment variables win).
- **pytest** + **pytest-asyncio**; **testcontainers** to provision a throwaway
  PostgreSQL 15 for integration tests.
- **ruff** (lint + format) and **mypy** (type checking).
- **Docker** / Docker Compose.

---

## Repository layout

```
src/ja_pst_mcp/
  server.py        # MCP server (Streamable HTTP), lifespan, CLI entry point
  tools.py         # MCP tool handlers + registration
  discovery.py     # discovery models + SQL builders
  pagination.py    # opaque, versioned keyset cursors
  database.py      # async connection pool, read-only queries, serialization
  context.py       # AppContext shared through the lifespan
  config.py        # environment-driven configuration
tests/             # unit + integration tests (integration uses testcontainers)
db/demo/           # demo seed (compose demo stack + integration fixture)
db/roles/          # least-privilege role reference script
.github/workflows/ # CI (ruff, mypy, pytest, docker build)
Dockerfile
docker-compose.yaml        # MCP server only
docker-compose.demo.yaml   # + seeded PostgreSQL
```

---

## Configuration

All configuration comes from the environment. Sensitive values live in a local
`.env` (gitignored, copied from `.env.example`); real environment variables take
precedence over `.env`.

### Database

| Variable                     | Default     | Notes                                   |
| ---------------------------- | ----------- | --------------------------------------- |
| `JA_PST_DB_HOST`             | — (required)|                                         |
| `JA_PST_DB_PORT`             | `5432`      |                                         |
| `JA_PST_DB_NAME`             | — (required)|                                         |
| `JA_PST_DB_USER`             | — (required)|                                         |
| `JA_PST_DB_PASSWORD`         | — (required)|                                         |
| `JA_PST_DB_CONNECT_TIMEOUT`  | `10`        | seconds                                 |
| `JA_PST_DB_POOL_MIN`         | `1`         | pool lower bound                        |
| `JA_PST_DB_POOL_MAX`         | `5`         | bounds concurrent DB work               |
| `JA_PST_DB_POOL_TIMEOUT`     | `30`        | seconds to acquire a connection         |

### Server

| Variable                 | Default     | Notes                                                       |
| ------------------------ | ----------- | ----------------------------------------------------------- |
| `JA_PST_SERVER_HOST`     | `127.0.0.1` | non-localhost requires `ALLOWED_HOSTS`                      |
| `JA_PST_SERVER_PORT`     | `8000`      |                                                             |
| `JA_PST_ALLOWED_HOSTS`   | empty       | comma-separated; enables DNS-rebinding protection           |
| `JA_PST_ALLOWED_ORIGINS` | empty       | needed by browser MCP clients                               |
| `JA_PST_LOG_LEVEL`       | `INFO`      | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`                 |

### Query and discovery policy

| Variable                       | Default | Notes                                        |
| ------------------------------ | ------- | -------------------------------------------- |
| `JA_PST_MAX_ROWS`              | `200`   | safety cap for `db_run_read_only_query`      |
| `JA_PST_STATEMENT_TIMEOUT`     | `5`     | seconds, per transaction                     |
| `JA_PST_LOCK_TIMEOUT`          | `5`     | seconds, per transaction                     |
| `JA_PST_DISCOVERY_PAGE_SIZE`   | `200`   | default discovery page size                  |
| `JA_PST_DISCOVERY_MAX_PAGE_SIZE` | `1000`| hard maximum for a discovery page            |
| `JA_PST_ALLOWED_SCHEMAS`       | empty   | discovery allowlist; empty = all non-system  |

Discovery always excludes system schemas (`pg_catalog`, `information_schema`,
`pg_toast`).

---

## Tests

```sh
poetry run pytest
```

Unit and integration tests run together. The integration tests start a
**throwaway PostgreSQL 15** automatically via testcontainers (Docker required),
load the demo seed and assert the security boundary, so no external database is
needed.

Static checks:

```sh
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy            # checks src/ only
```

---

## MVP limitations

- **Read-only**: no writes, DDL or schema changes.
- No arbitrary SQL tool without guardrails; only the read-only query tool with a
  safety cap and mandatory single statement.
- **No caching**: repeated identical queries hit the database every time.
- **No transport streaming of rows**: MCP returns one tool result per call;
  progress is the only progressive channel.
- **Pagination, not "all"**: discovery tools must be traversed page by page.
- A **single database** per server instance (the one it connects to).

---

## License

Released under the [Apache License 2.0](LICENSE).
