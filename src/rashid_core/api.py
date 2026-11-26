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

``is_pmtiles_asset`` is the PMTiles test the viz rules use. It accepts an
asset dict and matches the PMTiles media type, or an href whose path ends in
``.pmtiles`` in any case, after the query and fragment.

``sidecar_link_gaps`` is the check behind PTL-FIL-002 and PTL-FIL-003. It
returns a ``SidecarLinkGap`` for each problem with a node's link to a sibling
file such as ``README.md``, and an empty tuple when one matching link
conforms. A tool that repairs these links should act on the gaps, so that it
agrees with the rules.

``resolve_local_href`` resolves an href against the root-relative path of the
document that holds it, the way the graph resolves hrefs on disk. It needs no
loaded graph. ``CatalogGraph.from_documents`` builds a graph from parsed
documents that are not on disk yet, so a writer can plan ``expected_links``
before it writes.

``validate`` runs validation and returns a ``Report`` of ``Finding`` objects.
``CHECKS`` maps every check ID to its ``CheckInfo``, and ``describe`` returns
the description of one check ID. The top-level package exports the same
objects:

    from rashid_core.api import validate
"""

from __future__ import annotations

from rashid_core._href import is_absolute_href, resolve_local_href
from rashid_core._media import has_cog, is_cog_media_type, is_pmtiles_asset
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
from rashid_core._stac import (
    STRUCTURAL_RELS,
    SidecarLinkGap,
    links_of,
    parse_rfc3339,
    roles_of,
    sidecar_link_gaps,
)
from rashid_core.catalog import CatalogGraph, ExpectedLink, Node, expected_links
from rashid_core.config import RulesConfig
from rashid_core.data.reader import LocalOnlyReader
from rashid_core.model import Finding, Severity
from rashid_core.registry import CHECKS, CheckInfo, describe
from rashid_core.report import Report
from rashid_core.runner import ValidationOptions, validate

__all__ = [
    "CHECKS",
    "SHA2_256",
    "SPDX_LICENSE_IDS",
    "STRUCTURAL_RELS",
    "CatalogGraph",
    "CheckInfo",
    "ExpectedLink",
    "Finding",
    "LocalOnlyReader",
    "Node",
    "Report",
    "RulesConfig",
    "Severity",
    "SidecarLinkGap",
    "ValidationOptions",
    "bundled_profile_versions",
    "canonical_spdx_id",
    "decode_multihash",
    "describe",
    "encode_multihash",
    "expected_links",
    "has_cog",
    "is_absolute_href",
    "is_cog_media_type",
    "is_pmtiles_asset",
    "is_well_formed_multihash",
    "links_of",
    "newest_profile_version",
    "parse_rfc3339",
    "profile_uri",
    "profile_version",
    "profile_version_key",
    "resolve_local_href",
    "roles_of",
    "sidecar_link_gaps",
    "spec_commit",
    "validate",
]
