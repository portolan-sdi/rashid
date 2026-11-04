"""Version navigation consistency (STAC version extension and RFC 5829)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import PurePosixPath

from rashid._href import is_absolute_href
from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity, json_pointer
from rashid.rule import Rule

_NEIGHBOURS = {
    "predecessor-version": "successor-version",
    "successor-version": "predecessor-version",
}
_RELS = {*_NEIGHBOURS, "latest-version", "version-history"}


class VersionChainRule(Rule):
    """Check the known version graph without prescribing a catalog layout.

    RFC 5829 defines predecessor and successor as inverses and permits
    branching and merging. The STAC extension limits latest-version to one
    link per resource. External targets are left to the live pass. Relative history links may
    name any existing file, including a changelog.
    """

    id = "PTL-VER-001"
    spec_ids = ()  # The delegated extension's navigation semantics have no manifest ID.
    default_severity = Severity.WARNING
    description = (
        "version links resolve, neighbours agree, histories are acyclic, and latest names a head"
    )

    def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
        resolved: dict[tuple[PurePosixPath, str], set[PurePosixPath]] = {}
        successors: dict[PurePosixPath, set[PurePosixPath]] = {}
        links, findings, uncertain = self._resolve_links(graph)
        yield from findings
        for node, _index, rel, target in links:
            resolved.setdefault((node.path, rel), set()).add(target.path)
            if rel in _NEIGHBOURS:
                older, newer = (target, node) if rel == "predecessor-version" else (node, target)
                successors.setdefault(older.path, set()).add(newer.path)
        for node, index, rel, target in links:
            pointer = json_pointer("links", index, "href")
            if rel in _NEIGHBOURS:
                inverse = _NEIGHBOURS[rel]
                if (target.path, inverse) not in uncertain and node.path not in resolved.get(
                    (target.path, inverse), set()
                ):
                    yield self.finding(
                        node,
                        f"{rel} target '{target.path}' has no reciprocal {inverse} link",
                        json_pointer=pointer,
                    )
            elif successors.get(target.path):
                yield self.finding(
                    node,
                    f"latest-version target '{target.path}' has a successor; it is not a head",
                    json_pointer=pointer,
                )
        for path in _cycle_origins(successors):
            yield self.finding(
                graph.nodes[path],
                "version history contains a cycle",
                json_pointer=json_pointer("links"),
            )

    def _resolve_links(
        self, graph: CatalogGraph
    ) -> tuple[list[tuple[Node, int, str, Node]], list[Finding], set[tuple[PurePosixPath, str]]]:
        links: list[tuple[Node, int, str, Node]] = []
        findings: list[Finding] = []
        uncertain: set[tuple[PurePosixPath, str]] = set()
        for node in graph.iter("catalog", "collection", "item"):
            latest = 0
            for index, rel, href in _version_links(node):
                pointer = json_pointer("links", index, "href")
                if rel == "latest-version":
                    latest += 1
                    if latest == 2:
                        findings.append(
                            self.finding(
                                node,
                                "at most one latest-version link is allowed",
                                json_pointer=json_pointer("links"),
                            )
                        )
                if rel == "version-history":
                    findings.extend(self._history_link(node, index, href, graph))
                    continue
                if isinstance(href, str) and href:
                    judgeable, target = graph.resolve_structural_link(node, href)
                else:
                    judgeable, target = True, None
                if not judgeable:
                    uncertain.add((node.path, rel))
                    continue
                if target is None and graph.base_url is not None:
                    continue  # Version links do not make the crawl load their targets.
                if target is None or target.kind == "unknown":
                    uncertain.add((node.path, rel))
                    findings.append(
                        self.finding(
                            node, f"{rel} does not resolve to a STAC resource", json_pointer=pointer
                        )
                    )
                    continue
                links.append((node, index, rel, target))
        return links, findings, uncertain

    def _history_link(
        self, node: Node, index: int, href: object, graph: CatalogGraph
    ) -> Iterable[Finding]:
        if graph.base_url is not None:
            return  # A crawl mirror omits arbitrary history documents.
        if isinstance(href, str) and href:
            if is_absolute_href(href):
                return  # Absolute changelogs need a network probe, not a STAC node.
            path = graph.local_path(node, href)
            if path is not None and graph.file_exists(path):
                return
        yield self.finding(
            node,
            "version-history does not resolve to an existing file",
            json_pointer=json_pointer("links", index, "href"),
        )


def _version_links(node: Node) -> Iterator[tuple[int, str, object]]:
    raw = node.data.get("links")
    for index, link in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(link, dict):
            continue
        rel = link.get("rel")
        if isinstance(rel, str) and rel in _RELS:
            yield index, rel, link.get("href")


def _cycle_origins(successors: dict[PurePosixPath, set[PurePosixPath]]) -> Iterator[PurePosixPath]:
    """Iterative DFS: each edge is visited once, even for long or branching histories."""
    finished: set[PurePosixPath] = set()
    active: set[PurePosixPath] = set()
    for start in successors:
        if start in finished:
            continue
        active.add(start)
        stack = [(start, iter(sorted(successors.get(start, ()))))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                active.remove(node)
                finished.add(node)
            elif child in active:
                yield node
            elif child not in finished:
                active.add(child)
                stack.append((child, iter(sorted(successors.get(child, ())))))
