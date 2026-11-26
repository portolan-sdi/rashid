"""Required-file rules (spec: core.md, Core Structure / AGENTS.md / README.md).

Existence, linkage, and the README content minimum. core.md, README.md: "The
`README.md` MUST contain at minimum a title, description, license, and data
provenance." The graph reads README text on demand because README.md is not
a STAC object.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rashid_core._stac import sidecar_link_gaps
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.model import Finding, Severity
from rashid_core.rule import Rule

_REQUIRED = ("AGENTS.md", "README.md")

# A Markdown heading of any level — the README's title.
_HEADING = re.compile(r"(?m)^#{1,6}\s+\S")
# Heuristic markers for the license and data-provenance mentions the spec
# requires. Substring matches on the casefolded text, so "Licensed under",
# "data sources", "Provenance:" all count.
_LICENSE_MARKERS = ("license", "licence", "spdx")
_PROVENANCE_MARKERS = ("provenance", "source", "produced", "derived", "origin", "collected")


def _check_markdown_link(
    rule: Rule, node: Node, graph: CatalogGraph, *, rel: str, target: str
) -> Iterable[Finding]:
    """The findings for each gap that :func:`sidecar_link_gaps` reports."""
    for gap in sidecar_link_gaps(graph, node, rel=rel, target=target):
        yield rule.finding(
            node,
            gap.message,
            json_pointer=gap.json_pointer,
            fix_hint=gap.fix_hint,
            expected=gap.expected,
            actual=gap.actual,
        )


class RequiredFilesRule(Rule):
    """Every catalog and collection directory carries its required files."""

    id = "PTL-FIL-001"
    spec_ids = ("PORTO-CORE-005",)
    default_severity = Severity.ERROR
    description = "catalog and collection directories must contain AGENTS.md and README.md"
    kinds = ()  # graph-level: needs the directory listings

    def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
        for node in graph.iter("catalog", "collection"):
            listing = graph.dir_listing.get(node.path.parent, set())
            for required in _REQUIRED:
                if required in listing:
                    continue
                message = f"directory is missing required file {required}"
                variant = next(
                    (name for name in listing if name.casefold() == required.casefold()), None
                )
                hint = f"create {required} in this object's directory"
                if variant is not None:
                    message += f" (found '{variant}'; filenames are case-sensitive)"
                    hint = f"rename '{variant}' to '{required}'"
                yield Finding(
                    rule_id=self.id,
                    severity=self.default_severity,
                    message=message,
                    path=str(node.path),
                    object_id=node.id,
                    fix_hint=hint,
                    expected=required,
                    actual=variant,
                )


class AgentsLinkRule(Rule):
    """AGENTS.md is referenced through a rel:'agents' markdown link."""

    id = "PTL-FIL-002"
    spec_ids = ("PORTO-CORE-061",)
    default_severity = Severity.ERROR
    description = "AGENTS.md must be linked with rel:'agents' and type text/markdown"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        yield from _check_markdown_link(self, node, graph, rel="agents", target="AGENTS.md")


class ReadmeLinkRule(Rule):
    """README.md is referenced through a rel:'describedby' markdown link."""

    id = "PTL-FIL-003"
    spec_ids = ("PORTO-CORE-062",)
    default_severity = Severity.ERROR
    description = "README.md must be linked with rel:'describedby' and type text/markdown"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        yield from _check_markdown_link(self, node, graph, rel="describedby", target="README.md")


class ReadmeContentRule(Rule):
    """README.md carries actual content: non-empty, with a title heading.

    core.md, README.md: "The `README.md` MUST contain at minimum a title,
    description, license, and data provenance." The machine-decidable core of
    that MUST is checked here: an empty or whitespace-only README, or one with
    no Markdown heading (no title), cannot satisfy it. The license and
    provenance mentions are heuristic and live in PTL-FIL-005.
    """

    id = "PTL-FIL-004"
    spec_ids = ("PORTO-CORE-063",)
    default_severity = Severity.ERROR
    description = "README.md must not be empty and must carry a title heading"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        text = graph.read_text(node.path.parent / "README.md")
        if text is None:
            return  # PTL-FIL-001 reports the absence
        if not text.strip():
            yield self.finding(
                node,
                "README.md is empty; it must contain at minimum a title, description,"
                " license, and data provenance",
                fix_hint="write the README: what the data is, its license, and where it came from",
            )
            return
        if not _HEADING.search(text):
            yield self.finding(
                node,
                "README.md has no Markdown heading; it must open with a title",
                fix_hint="add a title heading, e.g. '# Road Centerlines 2024'",
            )


class ReadmeSectionsRule(Rule):
    """README.md mentions the license and the data's provenance.

    The same content MUST as PTL-FIL-004, but whether prose "contains a
    license" or "contains data provenance" is only heuristically decidable
    (keyword markers on the text), and heuristics misfire — so, like
    PTL-TTL-002, this defaults to WARNING; use a severity override to promote
    it. Scoped to collections: license and provenance are properties of the
    data, and the reference catalog's organizing sub-catalogs legitimately
    describe structure, not licensing.
    """

    id = "PTL-FIL-005"
    spec_ids = ("PORTO-CORE-063",)
    default_severity = Severity.WARNING
    description = "a collection's README.md should mention its license and data provenance"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        text = graph.read_text(node.path.parent / "README.md")
        if text is None or not text.strip():
            return  # PTL-FIL-001/004 report absence and emptiness
        lowered = text.casefold()
        if not any(marker in lowered for marker in _LICENSE_MARKERS):
            yield self.finding(
                node,
                "README.md does not appear to mention the license",
                fix_hint="state the license, e.g. 'License: CC-BY-4.0'",
            )
        if not any(marker in lowered for marker in _PROVENANCE_MARKERS):
            yield self.finding(
                node,
                "README.md does not appear to mention the data's provenance",
                fix_hint="say where the data comes from, e.g. 'Source: national cadastre, 2024'",
            )
