"""Public helpers for tools built on rashid.

These names are re-exported from private modules. A name imported from
``rashid_core.api`` stays available across 0.x releases. The same name imported
from the module that defines it can break in any patch release.

A tool that rewrites catalog metadata has to read that metadata the way
rashid reads it. Reimplementing a check produces code that passes its own
tests and still disagrees with the rule it was written against.

    from rashid_core.api import has_cog, links_of, roles_of

``SPDX_LICENSE_IDS`` is the list PTL-LIC-001 validates against, and
``canonical_spdx_id`` is the lookup behind its case-mismatch hint. A tool that
gates on a license identifier should use both rather than keep a shorter list,
which would reject real identifiers the rule accepts.

``newest_profile_version`` names the profile version whose requirements rashid
applies, and ``bundled_profile_versions`` lists every profile schema the wheel
carries, oldest first. Compare versions with ``profile_version_key``; a string sort
puts v0.10.0 before v0.9.0.

To run validation, use the top-level package:

    from rashid_core import validate
"""

from __future__ import annotations

from rashid_core._href import is_absolute_href
from rashid_core._media import has_cog, is_cog_media_type
from rashid_core._multihash import (
    SHA2_256,
    decode_multihash,
    encode_multihash,
    is_well_formed_multihash,
)
from rashid_core._profile import (
    bundled_profile_versions,
    newest_profile_version,
    profile_uri,
    profile_version,
    profile_version_key,
    spec_commit,
)
from rashid_core._spdx import SPDX_LICENSE_IDS, canonical_spdx_id
from rashid_core._stac import STRUCTURAL_RELS, links_of, parse_rfc3339, roles_of
from rashid_core.catalog import CatalogGraph, ExpectedLink, Node, expected_links
from rashid_core.config import RulesConfig
from rashid_core.data.reader import LocalOnlyReader
from rashid_core.model import Severity
from rashid_core.report import Report
from rashid_core.runner import ValidationOptions

__all__ = [
    "CatalogGraph",
    "ExpectedLink",
    "LocalOnlyReader",
    "Node",
    "Report",
    "RulesConfig",
    "SHA2_256",
    "SPDX_LICENSE_IDS",
    "STRUCTURAL_RELS",
    "Severity",
    "ValidationOptions",
    "bundled_profile_versions",
    "canonical_spdx_id",
    "decode_multihash",
    "encode_multihash",
    "expected_links",
    "has_cog",
    "is_absolute_href",
    "is_cog_media_type",
    "is_well_formed_multihash",
    "links_of",
    "newest_profile_version",
    "parse_rfc3339",
    "profile_uri",
    "profile_version",
    "roles_of",
    "spec_commit",
    "profile_version_key",
]
