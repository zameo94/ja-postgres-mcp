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
