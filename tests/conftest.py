"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from ja_pst_mcp.config import DatabaseSettings, ServerSettings, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            host="db", port=5432, name="japst", user="alice", password="s3cret"
        ),
        server=ServerSettings(),
        log_level="DEBUG",
    )
