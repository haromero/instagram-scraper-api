"""Paginación por cursor opaco (base64 de {"offset": N})."""

import base64
import json
from typing import Sequence, TypeVar

from .errors import APIError
from .schemas import Pagination

T = TypeVar("T")

_MAX_CURSOR_LENGTH = 512


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise APIError(400, "INVALID_CURSOR", "El cursor no es válido.")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        offset = int(data.get("offset", 0))
        if offset < 0:
            raise ValueError("offset negativo")
        return offset
    except Exception:
        raise APIError(400, "INVALID_CURSOR", "El cursor no es válido.")


def paginate(items: Sequence[T], limit: int, cursor: str | None) -> tuple[list[T], Pagination]:
    offset = decode_cursor(cursor)
    chunk = list(items[offset : offset + limit])
    next_offset = offset + len(chunk)
    has_more = next_offset < len(items)
    pagination = Pagination(
        next_cursor=encode_cursor(next_offset) if has_more else None,
        has_more=has_more,
        limit=limit,
    )
    return chunk, pagination
