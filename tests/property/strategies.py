"""Hypothesis strategies that generate valid Portolan catalogs.

A strategy draws a :class:`CatalogPlan`, a frozen description of a tree, and
the test writes it with :meth:`CatalogPlan.write` into a temporary directory
it owns. Hypothesis forbids function-scoped fixtures such as ``tmp_path``
across examples, so the plan, not the directory, is what gets drawn.

Every plan writes a tree that ``validate`` reports clean, as the builder in
``tests.conftest`` does (``tests/integration/test_validate_clean.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hypothesis import strategies as st

from tests.conftest import CatalogBuilder, mirror_providers, mutate_json
from tests.property.permitted import mirror_links

# core.md, Collection IDs: lowercase [a-z0-9_-], starting with a letter, and
# unique across the tree (PORTO-CORE-016). Sub-catalog and item directories
# reuse the same alphabet so every id is also a safe directory name.
_IDS = st.from_regex(r"[a-z][a-z0-9_-]{0,11}", fullmatch=True)

# core.md, Human-Readable Titles (PORTO-CORE-038, PORTO-CORE-040): words of
# letters and digits, any script, separated by spaces. A single word is not a
# slug; only a run joined by "_" or "." is.
_LETTERS = st.characters(categories=("Lu", "Ll", "Lo"), include_characters="0123456789")
_WORD = st.text(alphabet=_LETTERS, min_size=1, max_size=8)
_TITLES = st.lists(_WORD, min_size=1, max_size=3).map(" ".join)

_SYNCED = "2026-07-01T12:00:00Z"


@dataclass(frozen=True)
class CollectionPlan:
    collection_id: str
    title: str
    items: tuple[str, ...]
    # core.md, Provenance: a mirror's producer and host differ.
    mirror: bool


@dataclass(frozen=True)
class CatalogPlan:
    catalog_id: str
    title: str
    collections: tuple[CollectionPlan, ...]
    subcatalogs: tuple[CatalogPlan, ...]

    def write(self, root: Path) -> Path:
        """Write the tree under ``root`` and return the root directory."""
        builder = CatalogBuilder(root, self.catalog_id, self.title)
        self._fill(builder)
        written = builder.write()
        for path in self._mirror_dirs(written):
            mutate_json(path / "collection.json", mirror_links)
        return written

    def _fill(self, builder: CatalogBuilder) -> None:
        # PORTO-CORE-057 asks every catalog whose collections are all mirrors
        # for an 'updated' field; an 'updated' on any catalog is plain STAC
        # common metadata, so every catalog records one.
        builder.overrides["updated"] = _SYNCED
        for plan in self.collections:
            overrides: dict[str, Any] = {}
            if plan.mirror:
                overrides = {"providers": mirror_providers(), "updated": _SYNCED}
            collection = builder.collection(plan.collection_id, plan.title, **overrides)
            for item_id in plan.items:
                collection.item(item_id)
        for sub in self.subcatalogs:
            sub._fill(builder.subcatalog(sub.catalog_id, sub.title))

    def _mirror_dirs(self, directory: Path) -> list[Path]:
        found = [directory / c.collection_id for c in self.collections if c.mirror]
        for sub in self.subcatalogs:
            found.extend(sub._mirror_dirs(directory / sub.catalog_id))
        return found

    def collection_dirs(self, root: Path) -> list[Path]:
        """Every collection directory, depth first, in plan order."""
        found = [root / c.collection_id for c in self.collections]
        for sub in self.subcatalogs:
            found.extend(sub.collection_dirs(root / sub.catalog_id))
        return found


@st.composite
def catalogs(draw: st.DrawFn, *, min_items: int = 0, max_depth: int = 1) -> CatalogPlan:
    """A valid catalog: a root with one to three collections and up to two
    sub-catalogs (PORTO-CORE-003 lets catalogs nest), each nesting at most
    ``max_depth`` levels. The first root collection has at least ``min_items``
    items, so a breaker can count on one being there."""
    ids = iter(draw(st.lists(_IDS, min_size=40, max_size=40, unique=True)))

    def collection(min_count: int) -> CollectionPlan:
        count = draw(st.integers(min_count, max(min_count, 2)))
        return CollectionPlan(
            next(ids), draw(_TITLES), tuple(next(ids) for _ in range(count)), draw(st.booleans())
        )

    def catalog(depth: int, first_items: int) -> CatalogPlan:
        low = 1 if depth == 0 else 0
        count = draw(st.integers(low, 3 if depth == 0 else 2))
        cols = tuple(collection(first_items if i == 0 else 0) for i in range(count))
        subs = tuple(
            catalog(depth + 1, 0)
            for _ in range(draw(st.integers(0, 2 if depth < max_depth else 0)))
        )
        return CatalogPlan(next(ids), draw(_TITLES), cols, subs)

    return catalog(0, min_items)
