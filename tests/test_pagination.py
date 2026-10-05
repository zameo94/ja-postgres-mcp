"""Unit tests for the opaque keyset-pagination cursors."""

from __future__ import annotations

import base64
import json

import pytest

from ja_pst_mcp.database import InvalidQueryError
from ja_pst_mcp.pagination import cursor_scope, decode_cursor, encode_cursor

SCOPE = cursor_scope("db_list_tables", {"schema": "public", "kind": None})


def _token(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_cursor_roundtrip() -> None:
    token = encode_cursor(("public", "orders"), SCOPE)

    assert decode_cursor(token, 2, SCOPE) == ("public", "orders")


def test_cursor_is_url_safe_and_unpadded() -> None:
    token = encode_cursor(("a/b+c",), SCOPE)

    assert "=" not in token
    assert "/" not in token
    assert "+" not in token


def test_decode_rejects_malformed_cursor() -> None:
    with pytest.raises(InvalidQueryError, match="invalid pagination cursor"):
        decode_cursor("!!!not-base64!!!", 1, SCOPE)


def test_decode_rejects_overlong_cursor() -> None:
    with pytest.raises(InvalidQueryError, match="invalid pagination cursor"):
        decode_cursor("a" * 600, 1, SCOPE)


def test_decode_rejects_unsupported_version() -> None:
    with pytest.raises(InvalidQueryError, match="unsupported pagination cursor"):
        decode_cursor(_token({"v": 99, "k": ["x"], "s": SCOPE}), 1, SCOPE)


def test_decode_rejects_wrong_part_count() -> None:
    token = encode_cursor(("a", "b"), SCOPE)

    with pytest.raises(InvalidQueryError, match="invalid pagination cursor"):
        decode_cursor(token, 1, SCOPE)


def test_decode_rejects_non_list_key() -> None:
    with pytest.raises(InvalidQueryError, match="invalid pagination cursor"):
        decode_cursor(_token({"v": 1, "k": "x", "s": SCOPE}), 1, SCOPE)


def test_decode_rejects_non_string_parts() -> None:
    with pytest.raises(InvalidQueryError, match="invalid pagination cursor"):
        decode_cursor(_token({"v": 1, "k": [1], "s": SCOPE}), 1, SCOPE)


def test_decode_rejects_scope_mismatch() -> None:
    token = encode_cursor(("a",), SCOPE)
    other = cursor_scope("db_list_tables", {"schema": "sales", "kind": None})

    with pytest.raises(InvalidQueryError, match="does not match the current filters"):
        decode_cursor(token, 1, other)


def test_decode_rejects_cursor_from_other_tool() -> None:
    token = encode_cursor(("a",), cursor_scope("db_list_schemas", {}))

    with pytest.raises(InvalidQueryError, match="does not match the current filters"):
        decode_cursor(token, 1, SCOPE)
