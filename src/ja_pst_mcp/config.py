"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from dotenv import dotenv_values, find_dotenv

_ENV_PREFIX = "JA_PST_"

_DEFAULT_DB_PORT = 5432
_DEFAULT_DB_CONNECT_TIMEOUT_SECONDS = 10
_DEFAULT_LOG_LEVEL = "INFO"
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


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


@dataclass(frozen=True, slots=True)
class Settings:
    database: DatabaseSettings
    log_level: str = _DEFAULT_LOG_LEVEL


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build settings from ``environ``.

    When ``environ`` is not provided, values are read from the local ``.env``
    file (if present) merged with ``os.environ``; real environment variables
    take precedence over ``.env``.

    Raises:
        ConfigurationError: if a required variable is missing or any value is invalid.
    """
    env = _resolve_environment(environ)

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
    )

    log_level = env.get(_ENV_PREFIX + "LOG_LEVEL", _DEFAULT_LOG_LEVEL).strip().upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigurationError(
            f"{_ENV_PREFIX}LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}"
        )

    return Settings(database=database, log_level=log_level)


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
