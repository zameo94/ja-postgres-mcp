"""ja-postgres-mcp: MCP server exposing PostgreSQL-backed tools."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ja-postgres-mcp")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled source tree
    __version__ = "0.0.0"

__all__: list[str] = []
