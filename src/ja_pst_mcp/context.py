"""Shared application context exposed through the MCP lifespan."""

from __future__ import annotations

from dataclasses import dataclass

from ja_pst_mcp.database import Database


@dataclass(frozen=True, slots=True)
class AppContext:
    """Objects created once at startup and shared by every handler."""

    database: Database
