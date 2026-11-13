"""Local partition enumeration and schema comparisons."""

from __future__ import annotations

import posixpath
from pathlib import Path

from rashid._href import is_absolute_href
from rashid.catalog import Node
from rashid.data import DataDefect, _files
from rashid.data._files import AssetFiles
from rashid.data.geoparquet import check_geoparquet
from rashid.data.reader import AssetReader, Locator
from rashid.model import Severity, json_pointer
from rashid.registry import (
    DAT_PARTITION_SCHEMA,
    DAT_UNAVAILABLE,
)
from rashid.rules._common import (
    assets_of,
)

# --- partition schema consistency ------------------------------------------


def partition_files(node: Node) -> list[tuple[str, Path]]:
    """Local files the collection's ``partition:glob`` matches, sorted.

    Only a relative glob is expanded, against the collection's own directory.
    A conformant glob is an absolute bucket URL (``s3://``), which rashid does
    not list, so it yields nothing, as does any other absolute pattern or one
    that normalizes outside the catalog. PTL-PRT-002 reports the relative glob.
    Each entry pairs the relative match, which names the file in a message,
    with its path on disk.
    """
    pattern = node.data.get("partition:glob")
    if not isinstance(pattern, str) or not pattern.strip():
        return []
    if is_absolute_href(pattern):
        return []  # remote or absolute: not listable from here
    normalized = posixpath.normpath(pattern)
    if normalized.startswith(".."):
        return []  # escapes the catalog tree
    base = node.abs_path.parent
    return _files.partition_files(base, normalized)


def check_partitions(
    node: Node, reader: AssetReader
) -> tuple[list[DataDefect], list[DataDefect], int]:
    """Read each partition's schema and GeoParquet checks through one handle.

    Only the column maps survive each file, so an arbitrarily large glob does
    not retain an open descriptor per partition. Declared assets are compared
    for schema here; their format findings come from the asset pass alone.
    """
    files = partition_files(node)
    if not files:
        return [], [], 0
    schemas: list[tuple[str, dict[str, str]]] = []
    already_read = _asset_paths(node, reader)
    found: list[DataDefect] = []
    for match, path in files:
        declared = str(path) in already_read
        with AssetFiles(Locator(is_remote=False, source=str(path))) as opened:
            try:
                parquet = opened.parquet()
            except Exception as exc:
                found.append(
                    DataDefect(
                        DAT_UNAVAILABLE,
                        Severity.WARNING,
                        f"partition file '{match}' data validation could not run: {exc}",
                        match,
                    )
                )
                continue
            schemas.append((match, {field.name: str(field.type) for field in parquet.schema_arrow}))
            if not declared:
                found.extend(check_geoparquet(match, opened, subject=f"partition file '{match}'"))
    return _partition_schema_defects(schemas), found, len(files)


def _partition_schema_defects(schemas: list[tuple[str, dict[str, str]]]) -> list[DataDefect]:
    """Every listed partition MUST share the first readable file's schema."""
    if len(schemas) < 2:
        return []  # nothing to compare
    reference_name, reference = schemas[0]
    defects: list[DataDefect] = []
    for name, schema in schemas[1:]:
        difference = _schema_difference(reference, schema)
        if difference is None:
            continue
        defects.append(
            DataDefect(
                DAT_PARTITION_SCHEMA,
                Severity.ERROR,
                f"partition file '{name}' does not share '{reference_name}'s "
                f"Parquet schema: {difference}",
                "",
                json_pointer=json_pointer("partition:glob"),
            )
        )
    return defects


def _asset_paths(node: Node, reader: AssetReader) -> set[str]:
    """Local paths the per-asset loop already reads on this node.

    A glob wide enough to match a declared asset would otherwise fault the same
    file twice, once per pass.
    """
    paths: set[str] = set()
    for _pointer, _key, asset in assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            continue
        located = reader.locate(node, href)
        if located is not None and not located.is_remote:
            paths.add(located.source)
    return paths


def _schema_difference(reference: dict[str, str], other: dict[str, str]) -> str | None:
    """A one-line description of how two column maps diverge, or None if equal."""
    missing = sorted(set(reference) - set(other))
    extra = sorted(set(other) - set(reference))
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing column(s) {missing}")
        if extra:
            parts.append(f"extra column(s) {extra}")
        return ", ".join(parts)
    for name in reference:
        if reference[name] != other[name]:
            return f"column '{name}' is {other[name]}, expected {reference[name]}"
    return None
