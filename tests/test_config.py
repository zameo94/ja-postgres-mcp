from pathlib import Path

import pytest

from ja_pst_mcp.config import ConfigurationError, load_settings

VALID_ENV = {
    "JA_PST_DB_HOST": "localhost",
    "JA_PST_DB_NAME": "japst",
    "JA_PST_DB_USER": "japst",
    "JA_PST_DB_PASSWORD": "secret",
}

REQUIRED_KEYS = [
    "JA_PST_DB_HOST",
    "JA_PST_DB_NAME",
    "JA_PST_DB_USER",
    "JA_PST_DB_PASSWORD",
]


def test_load_settings_reads_required_values() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.database.host == "localhost"
    assert settings.database.name == "japst"
    assert settings.database.user == "japst"
    assert settings.database.password == "secret"


def test_load_settings_applies_defaults() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.database.port == 5432
    assert settings.database.connect_timeout_seconds == 10
    assert settings.log_level == "INFO"


def test_server_settings_defaults() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8000


def test_server_settings_can_be_overridden() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_SERVER_HOST": "127.0.0.1",
        "JA_PST_SERVER_PORT": "9000",
    }

    settings = load_settings(env)

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9000


def test_server_allowlists_default_to_empty() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.server.allowed_hosts == ()
    assert settings.server.allowed_origins == ()


def test_server_allowlists_are_parsed_from_comma_separated_values() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_ALLOWED_HOSTS": "ja-pst-mcp:8000, localhost:8000",
        "JA_PST_ALLOWED_ORIGINS": "https://app.example",
    }

    settings = load_settings(env)

    assert settings.server.allowed_hosts == ("ja-pst-mcp:8000", "localhost:8000")
    assert settings.server.allowed_origins == ("https://app.example",)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_non_localhost_host_without_allowlist_raises(host: str) -> None:
    env = {**VALID_ENV, "JA_PST_SERVER_HOST": host}

    with pytest.raises(ConfigurationError, match="JA_PST_ALLOWED_HOSTS"):
        load_settings(env)


def test_non_localhost_host_with_allowlist_is_accepted() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_SERVER_HOST": "0.0.0.0",
        "JA_PST_ALLOWED_HOSTS": "ja-pst-mcp:8000",
    }

    settings = load_settings(env)

    assert settings.server.host == "0.0.0.0"
    assert settings.server.allowed_hosts == ("ja-pst-mcp:8000",)


def test_allowed_origins_without_allowed_hosts_raises() -> None:
    env = {**VALID_ENV, "JA_PST_ALLOWED_ORIGINS": "https://app.example"}

    with pytest.raises(ConfigurationError, match="JA_PST_ALLOWED_ORIGINS"):
        load_settings(env)


def test_localhost_host_without_allowlist_is_accepted() -> None:
    settings = load_settings({**VALID_ENV, "JA_PST_SERVER_HOST": "localhost"})

    assert settings.server.host == "localhost"
    assert settings.server.allowed_hosts == ()


def test_localhost_host_with_allowlist_is_accepted() -> None:
    settings = load_settings(
        {
            **VALID_ENV,
            "JA_PST_SERVER_HOST": "localhost",
            "JA_PST_ALLOWED_HOSTS": "localhost:8000",
        }
    )

    assert settings.server.allowed_hosts == ("localhost:8000",)


def test_database_pool_defaults() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.database.pool_min_size == 1
    assert settings.database.pool_max_size == 5
    assert settings.database.pool_timeout_seconds == 30


def test_database_pool_can_be_overridden() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_DB_POOL_MIN": "2",
        "JA_PST_DB_POOL_MAX": "10",
        "JA_PST_DB_POOL_TIMEOUT": "15",
    }

    settings = load_settings(env)

    assert settings.database.pool_min_size == 2
    assert settings.database.pool_max_size == 10
    assert settings.database.pool_timeout_seconds == 15


def test_database_pool_max_lower_than_min_raises() -> None:
    env = {**VALID_ENV, "JA_PST_DB_POOL_MIN": "5", "JA_PST_DB_POOL_MAX": "2"}

    with pytest.raises(ConfigurationError, match="JA_PST_DB_POOL_MAX"):
        load_settings(env)


@pytest.mark.parametrize(
    "key",
    ["JA_PST_DB_POOL_MIN", "JA_PST_DB_POOL_MAX", "JA_PST_DB_POOL_TIMEOUT"],
)
def test_invalid_pool_values_raise(key: str) -> None:
    env = {**VALID_ENV, key: "0"}

    with pytest.raises(ConfigurationError, match=key):
        load_settings(env)


def test_query_settings_defaults() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.query.max_rows == 200
    assert settings.query.statement_timeout_seconds == 5
    assert settings.query.lock_timeout_seconds == 5
    assert settings.query.allowed_schemas == ()
    assert settings.query.discovery_page_size == 200
    assert settings.query.discovery_max_page_size == 1000


def test_query_settings_can_be_overridden() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_MAX_ROWS": "50",
        "JA_PST_STATEMENT_TIMEOUT": "10",
        "JA_PST_LOCK_TIMEOUT": "3",
        "JA_PST_ALLOWED_SCHEMAS": "public, sales",
    }

    settings = load_settings(env)

    assert settings.query.max_rows == 50
    assert settings.query.statement_timeout_seconds == 10
    assert settings.query.lock_timeout_seconds == 3
    assert settings.query.allowed_schemas == ("public", "sales")


def test_discovery_page_settings_can_be_overridden() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_DISCOVERY_PAGE_SIZE": "50",
        "JA_PST_DISCOVERY_MAX_PAGE_SIZE": "500",
    }

    settings = load_settings(env)

    assert settings.query.discovery_page_size == 50
    assert settings.query.discovery_max_page_size == 500


def test_discovery_max_page_smaller_than_page_raises() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_DISCOVERY_PAGE_SIZE": "500",
        "JA_PST_DISCOVERY_MAX_PAGE_SIZE": "100",
    }

    with pytest.raises(ConfigurationError, match="JA_PST_DISCOVERY_MAX_PAGE_SIZE"):
        load_settings(env)


def test_discovery_max_page_above_ceiling_raises() -> None:
    env = {**VALID_ENV, "JA_PST_DISCOVERY_MAX_PAGE_SIZE": "20000"}

    with pytest.raises(ConfigurationError, match="JA_PST_DISCOVERY_MAX_PAGE_SIZE"):
        load_settings(env)


@pytest.mark.parametrize(
    "key",
    ["JA_PST_DISCOVERY_PAGE_SIZE", "JA_PST_DISCOVERY_MAX_PAGE_SIZE"],
)
def test_invalid_discovery_page_values_raise(key: str) -> None:
    with pytest.raises(ConfigurationError, match=key):
        load_settings({**VALID_ENV, key: "0"})


@pytest.mark.parametrize("value", ["0", "10001", "not-a-number"])
def test_invalid_max_rows_raises(value: str) -> None:
    env = {**VALID_ENV, "JA_PST_MAX_ROWS": value}

    with pytest.raises(ConfigurationError, match="JA_PST_MAX_ROWS"):
        load_settings(env)


@pytest.mark.parametrize("key", ["JA_PST_STATEMENT_TIMEOUT", "JA_PST_LOCK_TIMEOUT"])
@pytest.mark.parametrize("value", ["0", "301"])
def test_invalid_timeout_raises(key: str, value: str) -> None:
    env = {**VALID_ENV, key: value}

    with pytest.raises(ConfigurationError, match=key):
        load_settings(env)


@pytest.mark.parametrize("value", ["0", "70000", "not-a-port"])
def test_invalid_server_port_raises(value: str) -> None:
    env = {**VALID_ENV, "JA_PST_SERVER_PORT": value}

    with pytest.raises(ConfigurationError, match="JA_PST_SERVER_PORT"):
        load_settings(env)


def test_load_settings_uses_explicit_optional_values() -> None:
    env = {
        **VALID_ENV,
        "JA_PST_DB_PORT": "6432",
        "JA_PST_DB_CONNECT_TIMEOUT": "30",
        "JA_PST_LOG_LEVEL": "warning",
    }

    settings = load_settings(env)

    assert settings.database.port == 6432
    assert settings.database.connect_timeout_seconds == 30
    assert settings.log_level == "WARNING"


@pytest.mark.parametrize("missing_key", REQUIRED_KEYS)
def test_missing_required_variable_raises(missing_key: str) -> None:
    env = {key: value for key, value in VALID_ENV.items() if key != missing_key}

    with pytest.raises(ConfigurationError, match=missing_key):
        load_settings(env)


def test_blank_required_variable_raises() -> None:
    env = {**VALID_ENV, "JA_PST_DB_HOST": "   "}

    with pytest.raises(ConfigurationError, match="JA_PST_DB_HOST"):
        load_settings(env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("JA_PST_DB_PORT", "not-a-number"),
        ("JA_PST_DB_PORT", "0"),
        ("JA_PST_DB_PORT", "70000"),
        ("JA_PST_DB_CONNECT_TIMEOUT", "0"),
        ("JA_PST_DB_CONNECT_TIMEOUT", "-5"),
    ],
)
def test_invalid_numeric_value_raises(key: str, value: str) -> None:
    env = {**VALID_ENV, key: value}

    with pytest.raises(ConfigurationError, match=key):
        load_settings(env)


def test_invalid_log_level_raises() -> None:
    env = {**VALID_ENV, "JA_PST_LOG_LEVEL": "LOUD"}

    with pytest.raises(ConfigurationError, match="JA_PST_LOG_LEVEL"):
        load_settings(env)


def test_password_is_not_exposed_in_repr() -> None:
    settings = load_settings(VALID_ENV)

    assert "secret" not in repr(settings)
    assert "secret" not in repr(settings.database)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = (
        *REQUIRED_KEYS,
        "JA_PST_DB_PORT",
        "JA_PST_DB_CONNECT_TIMEOUT",
        "JA_PST_LOG_LEVEL",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def _write_dotenv(directory: Path, **values: str) -> None:
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    (directory / ".env").write_text(content)


def test_load_settings_reads_values_from_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_dotenv(
        tmp_path,
        JA_PST_DB_HOST="dotenv-host",
        JA_PST_DB_NAME="dotenv-name",
        JA_PST_DB_USER="dotenv-user",
        JA_PST_DB_PASSWORD="dotenv-secret",
    )
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.database.host == "dotenv-host"
    assert settings.database.name == "dotenv-name"
    assert settings.database.user == "dotenv-user"
    assert settings.database.password == "dotenv-secret"


def test_real_environment_overrides_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    _write_dotenv(
        tmp_path,
        JA_PST_DB_HOST="dotenv-host",
        JA_PST_DB_NAME="dotenv-name",
        JA_PST_DB_USER="dotenv-user",
        JA_PST_DB_PASSWORD="dotenv-secret",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JA_PST_DB_HOST", "real-host")

    settings = load_settings()

    assert settings.database.host == "real-host"
