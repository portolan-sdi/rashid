"""Parse JSON with orjson, and give the stdlib's answer when orjson refuses.

A large catalog spends most of its graph load parsing documents, and orjson
parses them several times faster than :func:`json.loads`. The two parsers
disagree on some input, though. orjson rejects ``NaN``, ``Infinity``, a lone
surrogate escape, a UTF-8 byte order mark, and UTF-16 or UTF-32 bytes, all of
which :func:`json.loads` accepts. When orjson rejects a document, :func:`loads`
passes the same object to :func:`json.loads`, so a document parses exactly
when it did before and a parse failure carries the stdlib's message, which
findings quote.

A caller that must refuse a byte order mark or a non-UTF-8 encoding decodes
the bytes as UTF-8 itself and passes a ``str``, as ``CatalogGraph.load`` does.

One difference stays. orjson reads an integer beyond the 64-bit range as a
float, without an error. No STAC field holds such a value.
"""

from __future__ import annotations

import json
from typing import Any

import orjson

from rashid import _trace


def loads(raw: bytes | str) -> Any:
    """Parse ``raw`` as :func:`json.loads` parses it.

    Raises :class:`ValueError` as :func:`json.loads` does, including
    :class:`UnicodeDecodeError` for bytes in no encoding JSON allows.
    """
    _trace.count("json.parses")
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        _trace.count("json.stdlib_fallbacks")
        return json.loads(raw)
