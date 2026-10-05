import ja_postgres_mcp


def test_package_exposes_version() -> None:
    assert isinstance(ja_postgres_mcp.__version__, str)
    assert ja_postgres_mcp.__version__ == "0.1.0"


def test_package_public_api_is_minimal() -> None:
    assert ja_postgres_mcp.__all__ == []
