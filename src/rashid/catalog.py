"""In-memory model of a Portolan catalog's file tree.

The graph is built in a single I/O pass; rules query memory and never touch
disk. STAC objects are raw JSON dicts, not pystac objects, so validation
observes exactly what is on disk.
"""

from __future__ import annotations

import json
import os
import posixpath
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

Kind = Literal["catalog", "collection", "item", "unknown"]

ROOT_CATALOG = PurePosixPath("catalog.json")


@dataclass
class Node:
    """One STAC object file inside the catalog tree.

    Attributes:
        path: POSIX path of the JSON file, relative to the catalog root.
        abs_path: Absolute path on disk.
        kind: Detected object kind; ``unknown`` when the file cannot be parsed.
        id: The object's STAC ``id``, when present.
        data: Raw parsed JSON (empty dict when parsing failed).
        parse_error: JSON decode failure message, when parsing failed.
    """

    path: PurePosixPath
    abs_path: Path
    kind: Kind
    id: str | None
    data: dict[str, Any]
    parse_error: str | None = None


def _detect_kind(data: dict[str, Any]) -> Kind:
    stac_type = data.get("type")
    if stac_type == "Catalog":
        return "catalog"
    if stac_type == "Collection":
        return "collection"
    if stac_type == "Feature" and "stac_version" in data:
        return "item"
    return "unknown"


def is_absolute_href(href: str) -> bool:
    """True for hrefs with a URI scheme or a leading slash."""
    return href.startswith("/") or bool(urlparse(href).scheme)


def _is_within(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    """True when ``path`` sits at or below the directory ``prefix``.

    Only ever asked about a translation root's directory, which is a real
    subdirectory: the one file that could make ``prefix`` the catalog root is
    the root ``catalog.json``, and that is not a translation of itself.
    """
    return path.parts[: len(prefix.parts)] == prefix.parts


@dataclass
class CatalogGraph:
    """The catalog file tree loaded into memory.

    ``nodes`` maps relative POSIX file paths to STAC object nodes.
    ``dir_listing`` maps every scanned directory (relative POSIX path, ``.``
    for the root) to the set of filenames it contains, so required-file rules
    can check existence without touching disk again.
    """

    root_path: Path
    nodes: dict[PurePosixPath, Node] = field(default_factory=dict)
    dir_listing: dict[PurePosixPath, set[str]] = field(default_factory=dict)
    _translation_roots: list[Node] | None = field(default=None, repr=False, compare=False)

    @property
    def root(self) -> Node | None:
        """The root ``catalog.json`` node, when present and parseable."""
        node = self.nodes.get(ROOT_CATALOG)
        if node is not None and node.kind == "catalog":
            return node
        return None

    def translation_roots(self) -> list[Node]:
        """The root catalogs of the catalog's alternate-language trees.

        core.md, Alternate-Language Trees: a catalog may carry its metadata in
        more than one language, as one tree of STAC objects per language joined
        by ``alternate`` links. Such a tree translates the catalog rather than
        extending it, so it hangs off the root catalog's ``alternate`` links
        and never off its ``child`` links. Following those links is the only
        way to find it, since the walk that builds the graph sees a translation
        as an ordinary directory of STAC files.

        Only the root catalog's own ``alternate`` links count, and only those
        declaring ``application/json``. ``alternate`` also covers other media
        types, so the type is what separates a translation from an HTML
        rendering of the same object.
        """
        if self._translation_roots is None:
            self._translation_roots = self._find_translation_roots()
        return self._translation_roots

    def _find_translation_roots(self) -> list[Node]:
        root = self.root
        if root is None:
            return []
        raw = root.data.get("links")
        found: list[Node] = []
        for link in raw if isinstance(raw, list) else []:
            if not isinstance(link, dict):
                continue
            if link.get("rel") != "alternate" or link.get("type") != "application/json":
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            target = self.resolve_link(root, href)
            if target is not None and target.kind == "catalog" and target is not root:
                found.append(target)
        return found

    def language_root_of(self, node: Node) -> Node | None:
        """The root catalog of the language tree ``node`` sits in.

        A node inside an alternate-language tree answers with that tree's root
        catalog. Every other node answers with the catalog's own root, so a
        caller can compare two nodes' answers to tell whether they share a
        language tree.
        """
        best: Node | None = None
        for translation in self.translation_roots():
            prefix = translation.path.parent
            if not _is_within(node.path, prefix):
                continue
            if best is None or len(prefix.parts) > len(best.path.parent.parts):
                best = translation
        return best if best is not None else self.root

    @classmethod
    def load(cls, root_path: Path) -> CatalogGraph:
        """Walk ``root_path`` and load every STAC object file.

        ``catalog.json`` and ``collection.json`` files are always loaded
        (a parse failure becomes a ``parse_error`` node). Any other ``.json``
        file is loaded and kept only if it is a STAC item; non-item JSON
        (style files, arbitrary sidecars) is ignored. Hidden directories are
        skipped.
        """
        graph = cls(root_path=root_path.resolve())
        for dirpath, dirnames, filenames in os.walk(graph.root_path):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            rel_dir = PurePosixPath(Path(dirpath).relative_to(graph.root_path).as_posix())
            graph.dir_listing[rel_dir] = set(filenames)
            for filename in sorted(filenames):
                if not filename.endswith(".json"):
                    continue
                abs_path = Path(dirpath) / filename
                rel = (
                    rel_dir / filename if rel_dir != PurePosixPath(".") else PurePosixPath(filename)
                )
                structural = filename in ("catalog.json", "collection.json")
                try:
                    data = json.loads(abs_path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    if structural:
                        graph.nodes[rel] = Node(
                            path=rel,
                            abs_path=abs_path,
                            kind="unknown",
                            id=None,
                            data={},
                            parse_error=str(exc),
                        )
                    continue
                if not isinstance(data, dict):
                    if structural:
                        graph.nodes[rel] = Node(
                            path=rel,
                            abs_path=abs_path,
                            kind="unknown",
                            id=None,
                            data={},
                            parse_error="top-level JSON value is not an object",
                        )
                    continue
                kind = _detect_kind(data)
                if not structural and kind != "item":
                    continue
                raw_id = data.get("id")
                graph.nodes[rel] = Node(
                    path=rel,
                    abs_path=abs_path,
                    kind=kind,
                    id=raw_id if isinstance(raw_id, str) else None,
                    data=data,
                )
        return graph

    def iter(self, *kinds: Kind) -> Iterator[Node]:
        """Yield nodes of the given kinds (all nodes when none given)."""
        for path in sorted(self.nodes):
            node = self.nodes[path]
            if not kinds or node.kind in kinds:
                yield node

    def file_exists(self, rel_path: PurePosixPath) -> bool:
        """True when the walk saw a file at this relative path."""
        return rel_path.name in self.dir_listing.get(rel_path.parent, set())

    def resolve_link(self, from_node: Node, href: str) -> Node | None:
        """Resolve a relative href against the file tree.

        Returns the target node, or None when the href is absolute, escapes
        the catalog root, or does not land on a known STAC object file.
        """
        resolved = self.resolve_path(from_node, href)
        if resolved is None:
            return None
        return self.nodes.get(resolved)

    def resolve_path(self, from_node: Node, href: str) -> PurePosixPath | None:
        """Normalize a relative href to a root-relative POSIX path."""
        if not href or is_absolute_href(href):
            return None
        base = from_node.path.parent
        joined = posixpath.normpath(posixpath.join(str(base), href))
        if joined == "." or joined.startswith(".."):
            return None
        return PurePosixPath(joined)

    def parent_of(self, node: Node) -> Node | None:
        """The object containing ``node``: the nearest ancestor-directory object.

        For a catalog or collection the search starts at the directory above
        its own; for an item it starts at its own directory (item JSON may sit
        directly in the collection directory or in a per-item subdirectory).
        The root catalog has no parent.
        """
        if node.path == ROOT_CATALOG:
            return None
        start = node.path.parent
        if node.kind in ("catalog", "collection"):
            start = start.parent
        current = start
        while True:
            for name in ("catalog.json", "collection.json"):
                candidate = self.nodes.get(current / name)
                if candidate is None and str(current) == ".":
                    candidate = self.nodes.get(PurePosixPath(name))
                if candidate is not None and candidate.path != node.path:
                    return candidate
            if str(current) == ".":
                return None
            current = current.parent

    def enclosing_collection_of(self, node: Node) -> Node | None:
        """The nearest collection above ``node`` in the containment chain.

        core.md, Core Structure permits a catalog below a collection to
        organize its items, so the object containing an item is not always
        the collection the item belongs to. This walks the parent chain past
        any intermediate catalogs and returns the first collection it finds,
        or None when no collection encloses ``node``.
        """
        current = self.parent_of(node)
        while current is not None:
            if current.kind == "collection":
                return current
            current = self.parent_of(current)
        return None

    def children_of(self, node: Node) -> list[Node]:
        """Objects whose containment parent is ``node``."""
        return [n for n in self.iter() if self.parent_of(n) is node]

    def items_of(self, node: Node) -> list[Node]:
        """The items ``node`` owns, across any catalogs organizing them.

        The containment child of a collection is not always its item:
        core.md, Core Structure permits a catalog below a collection to group
        items, so an item can sit any number of levels down. Ownership follows
        :meth:`enclosing_collection_of`, which stops at the nearest collection,
        so a nested collection keeps its own items rather than lending them
        upward.
        """
        return [
            n for n in self.iter() if n.kind == "item" and self.enclosing_collection_of(n) is node
        ]
