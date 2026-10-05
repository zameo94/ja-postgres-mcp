import re

import ja_postgres_mcp


def test_package_exposes_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", ja_postgres_mcp.__version__)


def test_package_public_api_is_minimal() -> None:
    assert ja_postgres_mcp.__all__ == []
