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

## MCP server

- The server is a standalone service exposed over **MCP Streamable HTTP**
  (`transport="streamable-http"`). The HTTP endpoint is solely the MCP
  transport; do **not** add a custom REST API or non-MCP routes.
- The client is not spawned as a subprocess: do not use stdio as the transport.

## Non-negotiable workflow

1. Work in **small, independent, reviewable steps** (a single responsibility per step).
2. For each step: explain the goal, implement only that step, add/update tests.
3. **Do NOT run tests** (`pytest`, runners, integration tests, Docker tests). Write them; the developer executes them.
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
- Do not log or return sensitive data (passwords, connection strings, stack traces).

## Commands

```
# With uv
uv sync
uv run pytest

# With Poetry
poetry install
poetry run pytest

# Tests (developer-run only; agents must not execute)
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
