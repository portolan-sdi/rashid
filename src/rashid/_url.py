"""URL identity shared by transport and self-reference checks."""

from __future__ import annotations

import posixpath
import re
from urllib.parse import SplitResult, unquote, urlsplit

_ENCODED_SLASH = re.compile(r"%2f", re.IGNORECASE)
_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def url_authority(parsed: SplitResult) -> str:
    """Case-insensitive authority, omitting an explicit default HTTP port."""
    authority = parsed.netloc.lower()
    default = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    try:
        if default is not None and parsed.port == default:
            authority = authority.rsplit(":", 1)[0]
    except ValueError:
        pass  # Malformed ports are reported elsewhere.
    return authority


def store_path(parsed: SplitResult) -> str | None:
    """The decoded object path, or None when transport must preserve the URL."""
    if parsed.query or _ENCODED_SLASH.search(parsed.path):
        return None
    path = unquote(parsed.path).lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def _normalize_escape(match: re.Match[str]) -> str:
    char = chr(int(match[0][1:], 16))
    return char if char in _UNRESERVED else match[0].upper()


def object_location(url: str) -> tuple[str, str, str, str]:
    """Preserve reserved escapes and path segments, as the urllib fallback does.

    Only file URLs use filesystem path normalization. HTTP paths with encoded
    separators or empty/dot segments can name distinct objects in the store.
    """
    parsed = urlsplit(url)
    path = (
        posixpath.normpath(unquote(parsed.path))
        if parsed.scheme == "file"
        else _ESCAPE.sub(
            _normalize_escape if store_path(parsed) is not None else lambda match: match[0].upper(),
            parsed.path,
        )
    )
    return parsed.scheme.lower(), url_authority(parsed), path, parsed.query
