"""Helpers rashid's own rules use, published for tooling built on rashid.

Everything here is re-exported from a module whose internals move freely.
Import it from ``rashid.api`` and the name stays put across 0.x releases;
import it from its defining module and a refactor can break you.

The point is agreement rather than convenience. A fixer that rewrites what
rashid checks has to read metadata the same way rashid reads it, and a
reimplemented predicate drifts silently — it keeps passing its own tests
while disagreeing with the rule it was written against.

    from rashid.api import has_cog, links_of, roles_of

For running validation, use the top-level package instead:

    from rashid import validate
"""

from __future__ import annotations

from rashid._multihash import (
    SHA2_256,
    decode_multihash,
    encode_multihash,
    is_well_formed_multihash,
)
from rashid.rules._common import (
    STRUCTURAL_RELS,
    is_cog_media_type,
    links_of,
    parse_rfc3339,
    roles_of,
)
from rashid.rules.item_mirror import has_cog

__all__ = [
    "SHA2_256",
    "STRUCTURAL_RELS",
    "decode_multihash",
    "encode_multihash",
    "has_cog",
    "is_cog_media_type",
    "is_well_formed_multihash",
    "links_of",
    "parse_rfc3339",
    "roles_of",
]
