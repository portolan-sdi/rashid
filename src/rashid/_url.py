"""URL identity shared by transport and self-reference checks."""

from __future__ import annotations

import posixpath
import re
from urllib.parse import SplitResult, unquote, urlsplit

_ENCODED_SLASH = re.compile(r"%2f", re.IGNORECASE)


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


def object_location(url: str) -> tuple[str, str, str, str]:
    """Identify objects by the path transport requests.

    obstore uses the decoded store path; urllib preserves the URL path.
    Only file URLs use filesystem path normalization.
    """
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        path = posixpath.normpath(unquote(parsed.path))
    else:
        stored = store_path(parsed)
        path = parsed.path if stored is None else stored
    return parsed.scheme.lower(), url_authority(parsed), path, parsed.query
