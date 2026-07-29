"""Required-file rules (spec: core.md, Core Structure / AGENTS.md / README.md).

Existence, linkage, and the README content minimum. core.md, README.md: "The
`README.md` MUST contain at minimum a title, description, license, and data
provenance." The content rules are the one place the metadata pass reads a
file beyond the graph load: README.md is not a STAC object, so its text is not
in the graph, and the MUST cannot be judged without it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rashid.catalog import CatalogGraph, Node, is_absolute_href
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import links_of

_REQUIRED = ("AGENTS.md", "README.md")
_MARKDOWN = "text/markdown"

# A Markdown heading of any level — the README's title.
_HEADING = re.compile(r"(?m)^#{1,6}\s+\S")
# Heuristic markers for the license and data-provenance mentions the spec
# requires. Substring matches on the casefolded text, so "Licensed under",
# "data sources", "Provenance:" all count.
_LICENSE_MARKERS = ("license", "licence", "spdx")
_PROVENANCE_MARKERS = ("provenance", "source", "produced", "derived", "origin", "collected")


def _readme_text(node: Node, graph: CatalogGraph) -> str | None:
    """The node's sibling README.md text, or None when absent or unreadable.

    Absence is PTL-FIL-001's finding; an unreadable file cannot be graded, so
    it degrades to silence rather than a false content error.
    """
    if not graph.file_exists(node.path.parent / "README.md"):
        return None
    try:
        return (node.abs_path.parent / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _link_findings(
    rule: Rule,
    node: Node,
    graph: CatalogGraph,
    *,
    rel: str,
    target: str,
    index: int,
    link: dict[str, object],
) -> list[Finding]:
    """Everything wrong with one candidate link, empty when it conforms."""
    findings: list[Finding] = []
    if link.get("type") != _MARKDOWN:
        findings.append(
            rule.finding(
                node,
                f"rel:{rel!r} link has type {link.get('type')!r}, expected 'text/markdown'",
                json_pointer=f"/links/{index}/type",
                fix_hint=f"set the rel:{rel!r} link type to 'text/markdown'",
                expected=_MARKDOWN,
                actual=link.get("type"),
            )
        )
    href = link.get("href")
    if not isinstance(href, str) or not href or is_absolute_href(href):
        findings.append(
            rule.finding(
                node,
                f"rel:{rel!r} link href must be a relative path, got {href!r}",
                json_pointer=f"/links/{index}/href",
                fix_hint=f'set the href to "./{target}"',
                expected=f"./{target}",
                actual=href,
            )
        )
        return findings
    expected = node.path.parent / target
    if graph.resolve_path(node, href) != expected or not graph.file_exists(expected):
        findings.append(
            rule.finding(
                node,
                f"rel:{rel!r} link href {href!r} does not resolve to the sibling {target}",
                json_pointer=f"/links/{index}/href",
                fix_hint=f'set the href to "./{target}" and make sure that file exists next to'
                " this object",
                expected=f"./{target}",
                actual=href,
            )
        )
    return findings


def _points_at_sibling(
    node: Node, graph: CatalogGraph, link: dict[str, object], target: str
) -> bool:
    """True when the link's href names the sibling ``target``, broken or not."""
    href = link.get("href")
    if not isinstance(href, str) or not href or is_absolute_href(href):
        return False
    return graph.resolve_path(node, href) == node.path.parent / target


def _check_markdown_link(
    rule: Rule, node: Node, graph: CatalogGraph, *, rel: str, target: str
) -> Iterable[Finding]:
    """Findings for a required ``rel`` link pointing at a sibling markdown file.

    Shared by the AGENTS.md and README.md link rules: at least one relative,
    ``text/markdown`` link that resolves to ``target`` in the object's own
    directory. STAC allows several links under one rel, and a collection may
    legitimately point ``describedby`` at a data dictionary or another
    descriptive resource alongside its README. One conforming link satisfies
    the rule and the rest are left alone.

    When no link conforms, the findings name a single link: the one that comes
    closest to being the required one. Blaming every candidate would report a
    foreign link a fixer must not rewrite.
    """
    matches = [(index, link) for index, link in enumerate(links_of(node)) if link.get("rel") == rel]
    if not matches:
        yield rule.finding(
            node,
            f"missing rel:{rel!r} link to {target}",
            json_pointer="/links",
            fix_hint=f'add {{"rel": "{rel}", "href": "./{target}", "type": "text/markdown"}}',
        )
        return
    candidates: list[tuple[tuple[bool, bool, int], list[Finding]]] = []
    for index, link in matches:
        findings = _link_findings(rule, node, graph, rel=rel, target=target, index=index, link=link)
        if not findings:
            return
        rank = (
            not _points_at_sibling(node, graph, link, target),
            link.get("type") != _MARKDOWN,
            index,
        )
        candidates.append((rank, findings))
    yield from min(candidates, key=lambda candidate: candidate[0])[1]


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
        text = _readme_text(node, graph)
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
        text = _readme_text(node, graph)
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
