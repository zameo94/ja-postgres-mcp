"""Unit tests for the MCP server wiring (no server is started)."""

from __future__ import annotations

from ja_pst_mcp.config import DatabaseSettings, ServerSettings, Settings
from ja_pst_mcp.server import MCP_TRANSPORT, SERVER_NAME, create_server, run_server


def make_settings() -> Settings:
    return Settings(
        database=DatabaseSettings(
            host="db", port=5432, name="japst", user="alice", password="s3cret"
        ),
        server=ServerSettings(host="0.0.0.0", port=9000),
        log_level="DEBUG",
    )


def test_create_server_uses_name_and_log_level() -> None:
    server = create_server(make_settings())

    assert server.name == SERVER_NAME
    assert server.settings.log_level == "DEBUG"


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_run_server_uses_streamable_http_and_host_port() -> None:
    fake = FakeServer()

    run_server(fake, make_settings())

    assert fake.calls == [
        {"transport": MCP_TRANSPORT, "host": "0.0.0.0", "port": 9000}
    ]


def test_mcp_transport_is_streamable_http() -> None:
    assert MCP_TRANSPORT == "streamable-http"
