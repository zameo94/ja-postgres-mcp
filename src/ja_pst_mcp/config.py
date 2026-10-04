"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Mapping, cast

from dotenv import dotenv_values, find_dotenv

_ENV_PREFIX = "JA_PST_"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_DEFAULT_DB_PORT = 5432
_DEFAULT_DB_CONNECT_TIMEOUT_SECONDS = 10
_DEFAULT_DB_POOL_MIN = 1
_DEFAULT_DB_POOL_MAX = 5
_DEFAULT_DB_POOL_TIMEOUT_SECONDS = 30
_DEFAULT_SERVER_HOST = "127.0.0.1"
_DEFAULT_SERVER_PORT = 8000
_DEFAULT_MAX_ROWS = 200
_DEFAULT_STATEMENT_TIMEOUT_SECONDS = 5
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5
_MAX_MAX_ROWS = 10000
_MAX_TIMEOUT_SECONDS = 300
_DEFAULT_LOG_LEVEL: LogLevel = "INFO"
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
# Hosts the SDK protects by default. Keep in sync with mcp==2.3.0 (pinned by
# poetry.lock); any other host requires an explicit allowlist.
_LOCAL_SERVER_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    connect_timeout_seconds: int = _DEFAULT_DB_CONNECT_TIMEOUT_SECONDS
    pool_min_size: int = _DEFAULT_DB_POOL_MIN
    pool_max_size: int = _DEFAULT_DB_POOL_MAX
    pool_timeout_seconds: int = _DEFAULT_DB_POOL_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str = _DEFAULT_SERVER_HOST
    port: int = _DEFAULT_SERVER_PORT
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuerySettings:
    """Read-only analysis query policy.

    ``allowed_schemas`` is consumed by the discovery tools (schema/table
    listing); it is intentionally **not** applied to ``fetch_rows``, which runs
    arbitrary read-only SQL. Empty means "all non-system schemas".
    """

    max_rows: int = _DEFAULT_MAX_ROWS
    statement_timeout_seconds: int = _DEFAULT_STATEMENT_TIMEOUT_SECONDS
    lock_timeout_seconds: int = _DEFAULT_LOCK_TIMEOUT_SECONDS
    allowed_schemas: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Settings:
    database: DatabaseSettings
    server: ServerSettings
    query: QuerySettings = field(default_factory=QuerySettings)
    log_level: LogLevel = _DEFAULT_LOG_LEVEL


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``environ``.

    When ``environ`` is not provided, values are read from the local ``.env``
    file (if present) merged with ``os.environ``; real environment variables
    take precedence over ``.env``.

    Raises:
        ConfigurationError: if a required variable is missing or any value is invalid.
    """
    env = _resolve_environment(environ)

    pool_min_size = _optional_int(
        env, "DB_POOL_MIN", default=_DEFAULT_DB_POOL_MIN, minimum=1
    )
    pool_max_size = _optional_int(
        env, "DB_POOL_MAX", default=_DEFAULT_DB_POOL_MAX, minimum=1
    )
    if pool_max_size < pool_min_size:
        raise ConfigurationError(
            f"{_ENV_PREFIX}DB_POOL_MAX must be >= {_ENV_PREFIX}DB_POOL_MIN"
        )

    database = DatabaseSettings(
        host=_required(env, "DB_HOST"),
        port=_optional_int(env, "DB_PORT", default=_DEFAULT_DB_PORT, minimum=1, maximum=65535),
        name=_required(env, "DB_NAME"),
        user=_required(env, "DB_USER"),
        password=_required(env, "DB_PASSWORD"),
        connect_timeout_seconds=_optional_int(
            env,
            "DB_CONNECT_TIMEOUT",
            default=_DEFAULT_DB_CONNECT_TIMEOUT_SECONDS,
            minimum=1,
        ),
        pool_min_size=pool_min_size,
        pool_max_size=pool_max_size,
        pool_timeout_seconds=_optional_int(
            env,
            "DB_POOL_TIMEOUT",
            default=_DEFAULT_DB_POOL_TIMEOUT_SECONDS,
            minimum=1,
        ),
    )

    log_level = _optional_log_level(env, "LOG_LEVEL", default=_DEFAULT_LOG_LEVEL)

    server = ServerSettings(
        host=_optional_str(env, "SERVER_HOST", default=_DEFAULT_SERVER_HOST),
        port=_optional_int(
            env, "SERVER_PORT", default=_DEFAULT_SERVER_PORT, minimum=1, maximum=65535
        ),
        allowed_hosts=_optional_list(env, "ALLOWED_HOSTS"),
        allowed_origins=_optional_list(env, "ALLOWED_ORIGINS"),
    )
    _validate_server_settings(server)

    query = QuerySettings(
        max_rows=_optional_int(
            env, "MAX_ROWS", default=_DEFAULT_MAX_ROWS, minimum=1, maximum=_MAX_MAX_ROWS
        ),
        statement_timeout_seconds=_optional_int(
            env,
            "STATEMENT_TIMEOUT",
            default=_DEFAULT_STATEMENT_TIMEOUT_SECONDS,
            minimum=1,
            maximum=_MAX_TIMEOUT_SECONDS,
        ),
        lock_timeout_seconds=_optional_int(
            env,
            "LOCK_TIMEOUT",
            default=_DEFAULT_LOCK_TIMEOUT_SECONDS,
            minimum=1,
            maximum=_MAX_TIMEOUT_SECONDS,
        ),
        allowed_schemas=_optional_list(env, "ALLOWED_SCHEMAS"),
    )

    return Settings(database=database, server=server, query=query, log_level=log_level)


def _resolve_environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    if environ is not None:
        return environ
    return {**_dotenv_values(), **os.environ}


def _dotenv_values() -> dict[str, str]:
    path = find_dotenv(usecwd=True)
    if not path:
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _required(environ: Mapping[str, str], key: str) -> str:
    full_key = _ENV_PREFIX + key
    value = environ.get(full_key, "").strip()
    if not value:
        raise ConfigurationError(f"missing required environment variable {full_key}")
    return value


def _validate_server_settings(server: ServerSettings) -> None:
    if server.allowed_origins and not server.allowed_hosts:
        raise ConfigurationError(
            f"{_ENV_PREFIX}ALLOWED_ORIGINS requires {_ENV_PREFIX}ALLOWED_HOSTS"
        )
    if not server.allowed_hosts and server.host not in _LOCAL_SERVER_HOSTS:
        raise ConfigurationError(
            f"{_ENV_PREFIX}ALLOWED_HOSTS must be set when "
            f"{_ENV_PREFIX}SERVER_HOST={server.host!r} is not localhost"
        )


def _optional_str(environ: Mapping[str, str], key: str, *, default: str) -> str:
    raw = environ.get(_ENV_PREFIX + key, "")
    return raw.strip() or default


def _optional_list(environ: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = environ.get(_ENV_PREFIX + key, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _optional_log_level(
    environ: Mapping[str, str], key: str, *, default: LogLevel
) -> LogLevel:
    value = environ.get(_ENV_PREFIX + key, default).strip().upper()
    if value not in _VALID_LOG_LEVELS:
        raise ConfigurationError(
            f"{_ENV_PREFIX}{key} must be one of {sorted(_VALID_LOG_LEVELS)}"
        )
    return cast(LogLevel, value)


def _optional_int(
    environ: Mapping[str, str],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    full_key = _ENV_PREFIX + key
    raw = environ.get(full_key)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(f"{full_key} must be an integer") from None

    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ConfigurationError(f"{full_key} must be {bounds}")

    return value
