"""License rules (spec: core.md, License)."""

from __future__ import annotations

from collections.abc import Iterable

from rashid._spdx import SPDX_LICENSE_IDS, canonical_spdx_id
from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import links_of


class LicenseDeclaredRule(Rule):
    """Every collection declares an SPDX license identifier or 'other'."""

    id = "PTL-LIC-001"
    spec_ids = ("PORTO-CORE-058", "PORTO-CORE-059")
    default_severity = Severity.ERROR
    description = "collections must declare license as an SPDX identifier or 'other'"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        value = node.data.get("license")
        if not isinstance(value, str) or not value.strip():
            yield self.finding(
                node,
                "collection declares no license",
                json_pointer="/license",
                fix_hint="add a license with an SPDX identifier, e.g. 'CC-BY-4.0', or 'other'"
                " plus a rel:'license' link to the license text",
            )
            return
        if value == "proprietary":
            return  # PTL-LIC-003 reports this specifically
        if value == "other" or value in SPDX_LICENSE_IDS:
            return
        canonical = canonical_spdx_id(value)
        if canonical is not None:
            hint = f"SPDX identifiers are case-sensitive; write '{canonical}'"
        else:
            hint = (
                "replace it with the SPDX identifier for this license, e.g. 'CC-BY-4.0', or"
                " with 'other' plus a rel:'license' link to the license text"
            )
        yield self.finding(
            node,
            f"license '{value}' is not an SPDX identifier or 'other'",
            json_pointer="/license",
            fix_hint=hint,
            expected=canonical,
            actual=value,
        )


class OtherLicenseLinkRule(Rule):
    """license 'other' requires a rel:license link to the license text."""

    id = "PTL-LIC-002"
    spec_ids = ("PORTO-CORE-059",)
    default_severity = Severity.ERROR
    description = "license 'other' requires a rel:'license' link to the license text"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if node.data.get("license") != "other":
            return
        if not any(link.get("rel") == "license" for link in links_of(node)):
            yield self.finding(
                node,
                "license is 'other' but no rel:'license' link points to the license text",
                json_pointer="/links",
                fix_hint='add {"rel": "license", "href": <the license text or landing page>}'
                " to links, or replace 'other' with an SPDX identifier",
            )


class NoProprietaryLicenseRule(Rule):
    """The deprecated STAC value 'proprietary' is forbidden."""

    id = "PTL-LIC-003"
    spec_ids = ("PORTO-CORE-060",)
    default_severity = Severity.ERROR
    description = "the deprecated license value 'proprietary' must not be used"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if node.data.get("license") == "proprietary":
            yield self.finding(
                node,
                "license 'proprietary' is deprecated and must not be used",
                json_pointer="/license",
                fix_hint="use 'other' with a rel:'license' link, or an SPDX identifier",
            )
