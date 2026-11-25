"""Storage, Zarr, and table metadata checks without the geospatial stack."""

from __future__ import annotations

from typing import Any

from rashid_core import _trace
from rashid_core._media import is_zarr_store_asset
from rashid_core._stac import (
    asset_refers_to_self,
    assets_of,
    is_source_asset,
    roles_of,
    table_declarations,
)
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.data._results import DataDefect
from rashid_core.data.reader import AssetReader
from rashid_core.data.storage import check_bytes, expected_format
from rashid_core.data.zarr import check_zarr
from rashid_core.ids import DAT_COLUMN_DESCRIPTIONS
from rashid_core.model import Severity, json_pointer


def check_node(
    node: Node, reader: AssetReader, graph: CatalogGraph | None = None
) -> list[DataDefect]:
    """Verify storage, Zarr assets, and table metadata on one object."""
    defects = check_collection_column_descriptions(node)
    for _pointer, key, asset in assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href or asset_refers_to_self(node, href, graph):
            continue
        with _trace.span("data.asset"):
            _trace.count("data.assets")
            defects.extend(check_asset(node, key=key, asset=asset, reader=reader))
    return defects


def check_asset(
    node: Node, *, key: str, asset: dict[str, Any], reader: AssetReader
) -> list[DataDefect]:
    """Check one asset's bytes, or the metadata inside a Zarr prefix."""
    if is_zarr_store_asset(asset) and not is_source_asset(asset):
        # A prefix has no bytes of its own; read the zarr.json documents inside it.
        return check_zarr(node, key=key, asset=asset, reader=reader)
    media_type = asset.get("type")
    expected = expected_format(media_type) if isinstance(media_type, str) else None
    defects = check_bytes(key, asset=asset, expected=expected, node=node, reader=reader)
    if expected == "parquet":
        # Metadata only: an unreachable file still gets this check.
        defects.extend(check_column_descriptions(node, key, asset))
    return defects


def check_column_descriptions(node: Node, key: str, asset: dict[str, Any]) -> list[DataDefect]:
    """Every ``table:columns`` entry of a data asset has a description.

    formats.md, Vector Data: "Every entry in ``table:columns`` SHOULD include a
    ``description``" (PORTO-FMT-077). A name and a type are already in the
    Parquet footer. A mirror whose source has no descriptions notes this in the
    collection README (PORTO-FMT-078) and still gets this warning. The check
    is presence only: it does not detect placeholders (PORTO-FMT-079). It
    reads no file.
    """
    if "data" not in roles_of(asset):
        return []
    # Every declaration counts: object, properties, and the asset itself.
    columns = [
        column
        for holder in table_declarations(node, asset)
        if isinstance(holder.get("table:columns"), list)
        for column in holder["table:columns"]
    ]
    return _bare_columns(columns, f"asset '{key}'", key)


def check_collection_column_descriptions(node: Node) -> list[DataDefect]:
    """PTL-DAT-020 for the ``table:columns`` a collection declares on itself.

    A collection with no data asset, such as one that publishes through
    ``partition:glob`` or whose items contain the files, still documents its
    columns on itself (PORTO-FMT-046), and the per-asset check never sees
    them. With a data asset, that check reads the collection's columns too
    and owns the finding.
    """
    columns = node.data.get("table:columns")
    if node.kind != "collection" or not isinstance(columns, list):
        return []
    if has_data_asset(node):
        return []
    subject = (
        "partitioned collection"
        if isinstance(node.data.get("partition:glob"), str)
        else "collection"
    )
    return _bare_columns(columns, subject, "", json_pointer("table:columns"))


def has_data_asset(node: Node) -> bool:
    """Whether any asset on ``node`` has the ``data`` role."""
    return any("data" in roles_of(asset) for _pointer, _key, asset in assets_of(node))


def _bare_columns(
    columns: list[Any], subject: str, key: str, pointer: str | None = None
) -> list[DataDefect]:
    bare = [
        str(column.get("name", "?"))
        for column in columns
        if isinstance(column, dict)
        and not (isinstance(text := column.get("description"), str) and text.strip())
    ]
    if not bare:
        return []  # also no declaration: PTL-DAT-015 and PTL-DAT-017 own it
    shown = ", ".join(bare[:5])
    more = f", and {len(bare) - 5} more" if len(bare) > 5 else ""
    return [
        DataDefect(
            DAT_COLUMN_DESCRIPTIONS,
            Severity.WARNING,
            f"{subject}: {len(bare)} of {len(columns)} table:columns entries carry "
            f"no description ({shown}{more}). If the dataset is a mirror and the "
            "source has no descriptions, note this in the collection README.",
            key,
            json_pointer=pointer,
        )
    ]
