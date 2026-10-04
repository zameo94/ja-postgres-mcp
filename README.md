# ja-pst-mcp

MCP server that exposes PostgreSQL-backed tools for `ja-pst`, consumed by the
separate `ja-pst-mcp-client` application via MCP.

## Responsibility

This repository owns:

- the MCP server and its tools
- PostgreSQL access, connection management and input validation
- configuration, error handling, logging, security
- tests, Docker and essential documentation

It does **not** own the web application layer. React, FastAPI, HTTP APIs, the
Agent, the MCP Client and application authentication live in the separate
`ja-pst-mcp-client` repository.

## Architecture (high level)

```
ja-pst-mcp-client (MCP client)
        |
        | MCP
        v
ja-pst-mcp  (this repo: MCP server + tools)
        |
        v
    PostgreSQL
```

## Status

Under construction, built in small incremental steps. No MCP tools are
implemented yet.

## Requirements

- Python 3.12
- PostgreSQL (external dependency)

## Development setup

```
uv venv
uv pip install -e ".[dev]"
```

## Running the tests

```
pytest
```

> Tests are intended to be run by the developer/CI (see repository workflow).
