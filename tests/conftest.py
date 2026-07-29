"""Shared fixtures: a programmatic builder for valid Portolan catalogs.

Every builder default produces a fully conformant tree; each test mutates
exactly one aspect (builder kwarg or post-write surgery via ``mutate_json`` /
``delete_file``) and asserts the precise findings.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from rashid.model import Report

PORTOLAN_URI = "https://schemas.portolan-sdi.org/portolan/v0.1.0/schema.json"
# multihash: varint(0x12 sha2-256) + varint(0x20) + sha256(b"")
VALID_MULTIHASH = "1220e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_AGENTS_MD = "# Agents\n\nAccess data via relative hrefs in collection.json.\n"
_README_MD = "# Demo\n\nDescription, license: CC-BY-4.0, provenance: produced in-house.\n"


def default_providers() -> list[dict[str, Any]]:
    return [
        {
            "name": "Demo Org",
            "roles": ["producer", "host"],
            "url": "https://example.org/contact",
        }
    ]


def mirror_providers() -> list[dict[str, Any]]:
    return [
        {"name": "Source Org", "roles": ["producer"], "url": "https://source.example.org"},
        {"name": "Mirror Org", "roles": ["host"], "url": "https://mirror.example.org"},
    ]


def default_asset() -> dict[str, Any]:
    return {
        "href": "./data.parquet",
        "type": "application/vnd.apache.parquet",
        "roles": ["data"],
        "file:size": 1234,
        "file:checksum": VALID_MULTIHASH,
    }


def thumbnail_asset() -> dict[str, Any]:
    return {
        "href": "./thumbnail.png",
        "type": "image/png",
        "roles": ["thumbnail"],
        "file:size": 2048,
        "file:checksum": VALID_MULTIHASH,
    }


class ItemBuilder:
    def __init__(self, item_id: str, depth: int, collection_id: str, **overrides: Any):
        self.item_id = item_id
        self.depth = depth
        self.collection_id = collection_id
        self.overrides = overrides

    def build(self) -> dict[str, Any]:
        to_root = "../" * self.depth + "catalog.json"
        data: dict[str, Any] = {
            "type": "Feature",
            "stac_version": "1.1.0",
            "id": self.item_id,
            "collection": self.collection_id,
            "geometry": {"type": "Point", "coordinates": [5.0, 51.0]},
            "bbox": [4.0, 50.0, 6.0, 52.0],
            "properties": {"datetime": "2024-01-01T00:00:00Z"},
            "links": [
                {"rel": "root", "href": to_root, "type": "application/json"},
                {"rel": "parent", "href": "../collection.json", "type": "application/json"},
                {"rel": "collection", "href": "../collection.json", "type": "application/json"},
            ],
            "assets": {"data": default_asset()},
        }
        data.update(self.overrides)
        return data


class CollectionBuilder:
    def __init__(self, collection_id: str, depth: int, title: str, **overrides: Any):
        self.collection_id = collection_id
        self.depth = depth  # directories between this collection dir and the root
        self.title = title
        self.overrides = overrides
        self.items: list[ItemBuilder] = []

    def item(self, item_id: str, **overrides: Any) -> ItemBuilder:
        item = ItemBuilder(item_id, self.depth + 2, self.collection_id, **overrides)
        self.items.append(item)
        return item

    def build(self) -> dict[str, Any]:
        to_root = "../" * (self.depth + 1) + "catalog.json"
        links: list[dict[str, Any]] = [
            {"rel": "root", "href": to_root, "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown"},
            {"rel": "describedby", "href": "./README.md", "type": "text/markdown"},
        ]
        for item in self.items:
            links.append(
                {
                    "rel": "item",
                    "href": f"./{item.item_id}/{item.item_id}.json",
                    "type": "application/geo+json",
                    "title": f"Item {item.item_id}",
                }
            )
        data: dict[str, Any] = {
            "type": "Collection",
            "stac_version": "1.1.0",
            "id": self.collection_id,
            "title": self.title,
            "description": f"A demo collection named {self.title}.",
            "license": "CC-BY-4.0",
            "stac_extensions": [PORTOLAN_URI],
            "extent": {
                "spatial": {"bbox": [[4.0, 50.0, 6.0, 52.0]]},
                "temporal": {"interval": [["2024-01-01T00:00:00Z", "2024-12-31T23:59:59Z"]]},
            },
            "providers": default_providers(),
            "links": links,
            "assets": {"data": default_asset(), "thumbnail": thumbnail_asset()},
        }
        data.update(self.overrides)
        return data

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "collection.json").write_text(
            json.dumps(self.build(), indent=2), encoding="utf-8"
        )
        (directory / "AGENTS.md").write_text(_AGENTS_MD, encoding="utf-8")
        (directory / "README.md").write_text(_README_MD, encoding="utf-8")
        for item in self.items:
            item_dir = directory / item.item_id
            item_dir.mkdir(exist_ok=True)
            (item_dir / f"{item.item_id}.json").write_text(
                json.dumps(item.build(), indent=2), encoding="utf-8"
            )


class CatalogBuilder:
    """Builds a valid Portolan catalog tree; also used for sub-catalogs."""

    def __init__(
        self,
        root: Path,
        catalog_id: str = "root",
        title: str = "Demo Catalog",
        depth: int = 0,
        **overrides: Any,
    ):
        self.root = root
        self.catalog_id = catalog_id
        self.title = title
        self.depth = depth
        self.overrides = overrides
        self.collections: list[CollectionBuilder] = []
        self.subcatalogs: list[CatalogBuilder] = []

    def collection(
        self, collection_id: str, title: str | None = None, **overrides: Any
    ) -> CollectionBuilder:
        built = CollectionBuilder(
            collection_id, self.depth, title or f"Collection {collection_id}", **overrides
        )
        self.collections.append(built)
        return built

    def subcatalog(
        self, catalog_id: str, title: str | None = None, **overrides: Any
    ) -> CatalogBuilder:
        built = CatalogBuilder(
            self.root / catalog_id,
            catalog_id,
            title or f"Catalog {catalog_id}",
            depth=self.depth + 1,
            **overrides,
        )
        self.subcatalogs.append(built)
        return built

    def build(self) -> dict[str, Any]:
        to_root = "../" * self.depth + "catalog.json" if self.depth else "./catalog.json"
        links: list[dict[str, Any]] = [
            {"rel": "root", "href": to_root, "type": "application/json"},
            {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown"},
            {"rel": "describedby", "href": "./README.md", "type": "text/markdown"},
        ]
        if self.depth:
            links.insert(
                1, {"rel": "parent", "href": "../catalog.json", "type": "application/json"}
            )
        for collection in self.collections:
            links.append(
                {
                    "rel": "child",
                    "href": f"./{collection.collection_id}/collection.json",
                    "type": "application/json",
                    "title": collection.title,
                }
            )
        for subcatalog in self.subcatalogs:
            links.append(
                {
                    "rel": "child",
                    "href": f"./{subcatalog.catalog_id}/catalog.json",
                    "type": "application/json",
                    "title": subcatalog.title,
                }
            )
        data: dict[str, Any] = {
            "type": "Catalog",
            "stac_version": "1.1.0",
            "id": self.catalog_id,
            "title": self.title,
            "description": f"A demo catalog named {self.title}.",
            "stac_extensions": [PORTOLAN_URI],
            "links": links,
        }
        data.update(self.overrides)
        return data

    def write(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "catalog.json").write_text(
            json.dumps(self.build(), indent=2), encoding="utf-8"
        )
        (self.root / "AGENTS.md").write_text(_AGENTS_MD, encoding="utf-8")
        (self.root / "README.md").write_text(_README_MD, encoding="utf-8")
        for collection in self.collections:
            collection.write(self.root / collection.collection_id)
        for subcatalog in self.subcatalogs:
            subcatalog.write()
        return self.root


@pytest.fixture
def catalog(tmp_path: Path) -> CatalogBuilder:
    return CatalogBuilder(tmp_path / "catalog")


def write_organizing_catalog_layout(
    catalog: CatalogBuilder, collection_href: str = "../../collection.json"
) -> Path:
    """Build a catalog-under-collection tree and return the catalog root.

    core.md, Core Structure allows a catalog below a collection to organize
    its items. The item's parent is that organizing catalog while its
    ``collection`` link points at the collection above it::

        catalog.json
        roads/collection.json
        roads/2024/catalog.json
        roads/2024/roads-2024/roads-2024.json

    ``collection_href`` is written verbatim as the item's rel:'collection'
    href, relative to the item file, so callers can aim it at a wrong target.
    """
    collection = catalog.collection("roads")
    item = collection.item("roads-2024")
    root = catalog.write()

    year_dir = root / "roads" / "2024"
    year_dir.mkdir(parents=True, exist_ok=True)
    (year_dir / "catalog.json").write_text(
        json.dumps(
            {
                "type": "Catalog",
                "stac_version": "1.1.0",
                "id": "roads-by-year",
                "title": "Roads by year",
                "description": "Organizes the roads collection's items by year.",
                "stac_extensions": [PORTOLAN_URI],
                "links": [
                    {"rel": "root", "href": "../../catalog.json", "type": "application/json"},
                    {"rel": "parent", "href": "../collection.json", "type": "application/json"},
                    {
                        "rel": "item",
                        "href": "./roads-2024/roads-2024.json",
                        "type": "application/geo+json",
                        "title": "Item roads-2024",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Move the item one level down, under the organizing catalog.
    data = item.build()
    data["links"] = [
        {"rel": "root", "href": "../../../catalog.json", "type": "application/json"},
        {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
        {"rel": "collection", "href": collection_href, "type": "application/json"},
    ]
    item_dir = year_dir / "roads-2024"
    item_dir.mkdir(exist_ok=True)
    (item_dir / "roads-2024.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (root / "roads" / "roads-2024" / "roads-2024.json").unlink()
    (root / "roads" / "roads-2024").rmdir()

    def repoint(d: dict[str, Any]) -> None:
        d["links"] = [link for link in d["links"] if link["rel"] != "item"]
        d["links"].append(
            {
                "rel": "child",
                "href": "./2024/catalog.json",
                "type": "application/json",
                "title": "Roads by year",
            }
        )

    mutate_json(root / "roads" / "collection.json", repoint)
    return root


def nest_items_under_organizing_catalog(
    root: Path, collection_dir: Path, catalog_id: str = "2024"
) -> Path:
    """Move a written collection's items under a catalog that organizes them.

    core.md, Core Structure permits a catalog below a collection to group its
    items, a raster collection by year being the example it gives. Containment
    changes, ownership does not, so every item keeps its rel:'collection' link
    to the collection above. Returns the organizing catalog's directory.
    """
    depth = len(collection_dir.relative_to(root).parts)
    name = collection_dir.name
    organizing = collection_dir / catalog_id
    organizing.mkdir(parents=True, exist_ok=True)

    collection_json = collection_dir / "collection.json"
    links = json.loads(collection_json.read_text(encoding="utf-8"))["links"]
    item_links = [link for link in links if link.get("rel") == "item"]
    title = f"{name} by {catalog_id}"

    (organizing / "catalog.json").write_text(
        json.dumps(
            {
                "type": "Catalog",
                "stac_version": "1.1.0",
                "id": f"{name}-by-{catalog_id}",
                "title": title,
                "description": f"Organizes the {name} collection's items.",
                "stac_extensions": [PORTOLAN_URI],
                "links": [
                    {
                        "rel": "root",
                        "href": "../" * (depth + 1) + "catalog.json",
                        "type": "application/json",
                    },
                    {"rel": "parent", "href": "../collection.json", "type": "application/json"},
                    *item_links,
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    for link in item_links:
        item_id = link["href"].rsplit("/", 2)[-2]
        shutil.move(str(collection_dir / item_id), str(organizing / item_id))
        mutate_json(
            organizing / item_id / f"{item_id}.json",
            lambda d: d.__setitem__(
                "links",
                [
                    {
                        "rel": "root",
                        "href": "../" * (depth + 2) + "catalog.json",
                        "type": "application/json",
                    },
                    {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
                    {
                        "rel": "collection",
                        "href": "../../collection.json",
                        "type": "application/json",
                    },
                ],
            ),
        )

    def repoint(data: dict[str, Any]) -> None:
        data["links"] = [link for link in data["links"] if link.get("rel") != "item"]
        data["links"].append(
            {
                "rel": "child",
                "href": f"./{catalog_id}/catalog.json",
                "type": "application/json",
                "title": title,
            }
        )

    mutate_json(collection_json, repoint)
    return organizing


def mutate_json(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Load a JSON file, apply an in-place mutation, and write it back."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def rule_ids(report: Report) -> set[str]:
    return {finding.rule_id for finding in report.findings}


def findings_for(report: Report, rule_id: str) -> list[Any]:
    return [finding for finding in report.findings if finding.rule_id == rule_id]


# Substrings that mark a finding as "the host went away", not "the catalog is wrong".
_NETWORK_FLAKE_MARKERS = (
    "could not be read",
    "unresolvable",
    "connection",
    "timed out",
    "timeout",
    "name resolution",
    "ssl",
)


def skip_if_network_flaked(messages: Iterable[str]) -> None:
    """Skip the calling test when a networked check never reached its host.

    Networked tests talk to live servers, which sometimes reset the connection
    or stall. A finding that reports unreadable bytes or an unresolvable schema
    describes the transport, not the catalog under test, so it should skip
    rather than fail.
    """
    for message in messages:
        lowered = message.lower()
        if any(marker in lowered for marker in _NETWORK_FLAKE_MARKERS):
            pytest.skip(f"network flaked: {message}")
