"""Unit tests for the MCP server wiring (no server is started)."""

from __future__ import annotations

from dataclasses import replace

from mcp.server.transport_security import TransportSecuritySettings

from ja_pst_mcp.config import ServerSettings, Settings
from ja_pst_mcp.server import (
    MCP_TRANSPORT,
    SERVER_NAME,
    _transport_security,
    create_server,
    run_server,
)


def test_create_server_uses_name_and_log_level(settings: Settings) -> None:
    server = create_server(settings)

    assert server.name == SERVER_NAME
    assert server.settings.log_level == "DEBUG"


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_run_server_uses_streamable_http_and_host_port(settings: Settings) -> None:
    fake = FakeServer()

    run_server(fake, settings)

    call = fake.calls[0]
    assert call["transport"] == MCP_TRANSPORT
    assert call["host"] == settings.server.host
    assert call["port"] == settings.server.port
    assert call["transport_security"] is None


def test_transport_security_is_none_without_allowlist() -> None:
    assert _transport_security(ServerSettings()) is None


def test_transport_security_built_from_allowlist(settings: Settings) -> None:
    server = replace(
        settings.server,
        allowed_hosts=("ja-pst-mcp:8000",),
        allowed_origins=("https://app.example",),
    )

    security = _transport_security(server)

    assert isinstance(security, TransportSecuritySettings)
    assert security.allowed_hosts == ["ja-pst-mcp:8000"]
    assert security.allowed_origins == ["https://app.example"]


def test_transport_security_with_hosts_and_empty_origins(settings: Settings) -> None:
    server = replace(settings.server, allowed_hosts=("ja-pst-mcp:8000",))

    security = _transport_security(server)

    assert isinstance(security, TransportSecuritySettings)
    assert security.allowed_hosts == ["ja-pst-mcp:8000"]
    assert security.allowed_origins == []


def test_run_server_passes_configured_transport_security(settings: Settings) -> None:
    configured = replace(
        settings,
        server=replace(
            settings.server,
            allowed_hosts=("ja-pst-mcp:8000",),
            allowed_origins=("https://app.example",),
        ),
    )
    fake = FakeServer()

    run_server(fake, configured)

    security = fake.calls[0]["transport_security"]
    assert isinstance(security, TransportSecuritySettings)
    assert security.allowed_hosts == ["ja-pst-mcp:8000"]
    assert security.allowed_origins == ["https://app.example"]


def test_mcp_transport_is_streamable_http() -> None:
    assert MCP_TRANSPORT == "streamable-http"
