"""Opaque, versioned keyset-pagination cursors.

The scope digest binds a cursor to the tool and its filters, so a cursor from a
different listing is rejected instead of silently returning a wrong window. It
is an **unkeyed** hash on purpose: it is context detection, not a security
barrier. The cursor parts are bound as SQL parameters and the schema allowlist
is applied independently by the query, so a forged cursor can at worst move the
keyset window within the schemas the caller is already allowed to query; a
server secret would add state to a stateless design for no security gain.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence

from ja_postgres_mcp.database import InvalidQueryError

_CURSOR_VERSION = 1
_SCOPE_DIGEST_SIZE = 8  # 64 bits -> 16 hex characters
# A legitimate cursor is ~320 chars at most; this also keeps a hostile,
# deeply-nested payload from reaching json.loads (RecursionError).
_MAX_CURSOR_LENGTH = 512


def cursor_scope(tool: str, filters: Mapping[str, object]) -> str:
    """Return a stable fingerprint of the tool and its keyset-affecting filters.

    ``page_size`` is deliberately excluded: changing it between pages is valid
    keyset usage. The schema allowlist is excluded because it is stable for the
    process lifetime and cannot cause a mismatch.
    """
    canonical = json.dumps({"tool": tool, **filters}, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=_SCOPE_DIGEST_SIZE).hexdigest()


def encode_cursor(parts: Sequence[str], scope: str) -> str:
    """Encode a keyset tuple and its scope as an opaque, URL-safe cursor."""
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "k": list(parts), "s": scope},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, parts: int, scope: str) -> tuple[str, ...]:
    """Decode and validate a cursor for the given keyset and scope.

    Raises:
        InvalidQueryError: if the cursor is malformed, too long, its version is
            unsupported, or its scope does not match the current filters.
    """
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidQueryError("invalid pagination cursor")

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw)
    except ValueError:
        raise InvalidQueryError("invalid pagination cursor") from None

    if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
        raise InvalidQueryError(
            "unsupported pagination cursor; restart the listing without a cursor"
        )

    key = data.get("k")
    if (
        not isinstance(key, list)
        or len(key) != parts
        or not all(isinstance(part, str) for part in key)
    ):
        raise InvalidQueryError("invalid pagination cursor")

    if data.get("s") != scope:
        raise InvalidQueryError(
            "pagination cursor does not match the current filters; "
            "restart the listing without a cursor"
        )

    return tuple(key)
