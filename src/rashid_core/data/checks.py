"""Storage and Zarr dispatch, available without the geospatial stack."""

from __future__ import annotations

from typing import Any

from rashid_core import _trace
from rashid_core._media import is_zarr_store_asset
from rashid_core._stac import asset_refers_to_self, assets_of, is_source_asset
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.data._results import DataDefect
from rashid_core.data.reader import AssetReader
from rashid_core.data.storage import check_bytes, expected_format
from rashid_core.data.zarr import check_zarr


def check_node(
    node: Node, reader: AssetReader, graph: CatalogGraph | None = None
) -> list[DataDefect]:
    """Verify the storage metadata and Zarr assets on one object."""
    defects: list[DataDefect] = []
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
    return check_bytes(key, asset=asset, expected=expected, node=node, reader=reader)
