"""Public helpers for tools built on rashid.

These names are re-exported from private modules. A name imported from
``rashid.api`` stays available across 0.x releases. The same name imported
from the module that defines it can break in any patch release.

A tool that rewrites catalog metadata has to read that metadata the way
rashid reads it. Reimplementing a check produces code that passes its own
tests and still disagrees with the rule it was written against.

    from rashid.api import has_cog, links_of, roles_of

``SPDX_LICENSE_IDS`` is the list PTL-LIC-001 validates against, and
``canonical_spdx_id`` is the lookup behind its case-mismatch hint. A tool that
gates on a license identifier should use both rather than keep a shorter list,
which would reject real identifiers the rule accepts.

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
from rashid._spdx import SPDX_LICENSE_IDS, canonical_spdx_id
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
    "SPDX_LICENSE_IDS",
    "STRUCTURAL_RELS",
    "canonical_spdx_id",
    "decode_multihash",
    "encode_multihash",
    "has_cog",
    "is_cog_media_type",
    "is_well_formed_multihash",
    "links_of",
    "parse_rfc3339",
    "roles_of",
]
