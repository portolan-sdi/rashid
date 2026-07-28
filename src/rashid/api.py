"""Public helpers for tools built on rashid.

These names are re-exported from private modules. A name imported from
``rashid.api`` stays available across 0.x releases. The same name imported
from the module that defines it can break in any patch release.

A tool that rewrites catalog metadata has to read that metadata the way
rashid reads it. Reimplementing a check produces code that passes its own
tests and still disagrees with the rule it was written against.

    from rashid.api import has_cog, links_of, roles_of

To run validation, use the top-level package:

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
