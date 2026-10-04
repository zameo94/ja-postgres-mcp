"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest

from ja_pst_mcp.config import DatabaseSettings, QuerySettings, ServerSettings, Settings

# The Ryuk reaper is unreliable on some Docker setups (e.g. Docker Desktop);
# the container fixture stops its containers explicitly regardless.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            host="db", port=5432, name="japst", user="alice", password="s3cret"
        ),
        server=ServerSettings(),
        query=QuerySettings(),
        log_level="DEBUG",
    )


@pytest.fixture(scope="session")
def postgres_container():
    """A throwaway PostgreSQL 15 started for the test session (via Docker)."""
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:15", driver=None) as container:
        yield container
