"""Asset rules (spec: core.md, Assets).

Field presence and well-formedness only; verifying sizes and checksums
against actual bytes is a data-pass job.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from rashid._multihash import is_well_formed_multihash
from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule


def _assets_of(node: Node) -> list[tuple[str, str, dict[str, Any]]]:
    """(pointer_prefix, asset_key, asset) triples for a node's assets."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    assets = node.data.get("assets")
    if isinstance(assets, dict):
        for key, asset in assets.items():
            if isinstance(asset, dict):
                found.append((f"/assets/{key}", key, asset))
    return found


def _item_asset_templates(node: Node) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    templates = node.data.get("item_assets")
    if isinstance(templates, dict):
        for key, template in templates.items():
            if isinstance(template, dict):
                found.append((f"/item_assets/{key}", key, template))
    return found


class AssetFieldsRule(Rule):
    """Every asset carries an href, a media type, and at least one role."""

    id = "PTL-AST-001"
    spec_ids = ("PORTO-CORE-022", "PORTO-CORE-025", "PORTO-CORE-027")
    default_severity = Severity.ERROR
    description = "every asset needs an href, a type (media type), and at least one role"
    kinds = ("collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, key, asset in _assets_of(node):
            href = asset.get("href")
            if not isinstance(href, str) or not href.strip():
                yield self.finding(
                    node,
                    f"asset '{key}' has no href",
                    json_pointer=f"{pointer}/href",
                    fix_hint="set href to the file's path relative to this object,"
                    " e.g. './data.parquet'",
                )
            yield from self._check_type_and_roles(node, pointer, key, asset)
        # item_assets entries are templates: no href/size/checksum, but the
        # descriptive fields must still be complete.
        for pointer, key, template in _item_asset_templates(node):
            yield from self._check_type_and_roles(node, pointer, key, template)

    def _check_type_and_roles(
        self, node: Node, pointer: str, key: str, asset: dict[str, Any]
    ) -> Iterable[Finding]:
        media_type = asset.get("type")
        if not isinstance(media_type, str) or not media_type.strip():
            yield self.finding(
                node,
                f"asset '{key}' has no type (media type)",
                json_pointer=f"{pointer}/type",
                fix_hint="add the file's IANA media type, e.g."
                " 'application/vnd.apache.parquet' for GeoParquet",
            )
        roles = asset.get("roles")
        if not isinstance(roles, list) or not any(isinstance(r, str) and r.strip() for r in roles):
            yield self.finding(
                node,
                f"asset '{key}' has no roles",
                json_pointer=f"{pointer}/roles",
                fix_hint='add a roles array naming what the asset is for, e.g. ["data"],'
                ' ["thumbnail"], or ["visual"]',
            )


class AssetHrefSchemeRule(Rule):
    """Absolute asset hrefs use https, never s3 or plain http."""

    id = "PTL-AST-002"
    spec_ids = ("PORTO-CORE-023",)
    default_severity = Severity.ERROR
    description = "absolute asset hrefs must use https (browsers cannot fetch s3 URLs)"
    kinds = ("collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, key, asset in _assets_of(node):
            href = asset.get("href")
            if not isinstance(href, str) or not href:
                continue  # PTL-AST-001 reports missing hrefs
            scheme = urlparse(href).scheme.lower()
            if not scheme or scheme == "https":
                continue
            if scheme == "s3":
                yield self.finding(
                    node,
                    f"asset '{key}' href uses s3://; absolute hrefs must use https",
                    json_pointer=f"{pointer}/href",
                    fix_hint="use the https endpoint; expose s3 URLs via the alternate extension",
                    expected="https",
                    actual=scheme,
                )
            else:
                yield self.finding(
                    node,
                    f"asset '{key}' href uses scheme '{scheme}'; absolute hrefs must use https",
                    json_pointer=f"{pointer}/href",
                    fix_hint="rewrite the href as the https URL that serves this file",
                    expected="https",
                    actual=scheme,
                )


class AssetFileFieldsRule(Rule):
    """Every asset should carry file:size and file:checksum.

    core.md, Assets: "Assets SHOULD carry ``file:size`` and ``file:checksum``".
    A catalog that describes data it does not host cannot checksum bytes it
    never touches, so the absence is a warning rather than an error. A
    ``file:size`` that is present but not a positive integer is reported here
    too; the schema pass errors on its type independently (PORTO-CORE-028).
    """

    id = "PTL-AST-003"
    spec_ids = ("PORTO-CORE-028",)
    default_severity = Severity.WARNING
    description = "every asset should carry file:size and file:checksum"
    kinds = ("collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, key, asset in _assets_of(node):
            size = asset.get("file:size")
            if size is None:
                yield self.finding(
                    node,
                    f"asset '{key}' has no file:size",
                    json_pointer=f"{pointer}/file:size",
                    fix_hint="add file:size, the asset's size in bytes as an integer",
                )
            elif isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                yield self.finding(
                    node,
                    f"asset '{key}' file:size must be a positive integer, got {size!r}",
                    json_pointer=f"{pointer}/file:size",
                    fix_hint="set file:size to the asset's byte count, unquoted and greater than 0",
                    actual=size,
                )
            if asset.get("file:checksum") is None:
                yield self.finding(
                    node,
                    f"asset '{key}' has no file:checksum",
                    json_pointer=f"{pointer}/file:checksum",
                    fix_hint="add file:checksum, the multihash of the asset bytes:"
                    " '1220' followed by the sha256 hex digest",
                )


class CatalogAssetsRule(Rule):
    """Catalogs carry no assets.

    core.md, Core Structure: "catalogs and collections are internal nodes,
    items are leaves, and assets (data files) MUST sit at collection or item
    level. Catalogs organize only". An ``assets`` field on a catalog puts data
    at the wrong level, so its mere presence is the defect.
    """

    id = "PTL-AST-005"
    spec_ids = ("PORTO-CORE-002",)
    default_severity = Severity.ERROR
    description = "assets must sit at collection or item level; catalogs organize only"
    kinds = ("catalog",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if "assets" in node.data:
            yield self.finding(
                node,
                "catalog declares assets; assets (data files) must sit at collection or item level",
                json_pointer="/assets",
                fix_hint="move the assets onto the collection (or item) that owns the data",
            )


class ChecksumMultihashRule(Rule):
    """file:checksum values are multihash-encoded."""

    id = "PTL-AST-004"
    spec_ids = ("PORTO-CORE-029",)
    default_severity = Severity.ERROR
    description = "file:checksum must be multihash-encoded, not a raw digest string"
    kinds = ("collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, key, asset in _assets_of(node):
            checksum = asset.get("file:checksum")
            if checksum is None:
                continue  # PTL-AST-003 reports the absence
            if not is_well_formed_multihash(checksum):
                yield self.finding(
                    node,
                    f"asset '{key}' file:checksum is not a well-formed multihash",
                    json_pointer=f"{pointer}/file:checksum",
                    fix_hint="prefix the digest with the multihash code and length, "
                    "e.g. '1220' + sha256 hex",
                    actual=checksum,
                )
