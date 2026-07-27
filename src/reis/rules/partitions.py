"""Partition rules (spec: formats.md, Partitioned Collections).

formats.md: "Partitioning MUST be described per the partition extension
(v1.0.0), with its schema declared in `stac_extensions` and its required
fields carried — `partition:scheme`, `partition:keys`, and `partition:glob`."
And: "`partition:glob` is the normative bulk-access path; the collection
description SHOULD also mention the glob for human readers, but validators
read only the field."

A collection is taken to be partitioned when it declares any ``partition:*``
field or the partition extension URI — the signals the Portolan partition
extension writes.
"""

from __future__ import annotations

from collections.abc import Iterable

from reis.catalog import CatalogGraph, Node
from reis.model import Finding, Severity
from reis.rule import Rule

# Version-tolerant prefix of the partition extension's schema URI, matching
# how the web-map-links declaration is checked in the viz rules.
_PARTITION_EXTENSION_PREFIX = "https://schemas.portolan-sdi.org/incubating/partition/"


class PartitionFieldsRule(Rule):
    """A partitioned collection declares the extension and its required fields.

    One finding per missing piece: the extension URI in ``stac_extensions``,
    and each of ``partition:scheme``, ``partition:keys``, ``partition:glob``.
    The glob field alone is normative; a glob mentioned only in the
    description does not satisfy it ("validators read only the field").
    """

    id = "PTL-PRT-001"
    default_severity = Severity.ERROR
    description = (
        "a partitioned collection must declare the partition extension and carry"
        " partition:scheme, partition:keys, and partition:glob"
    )
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if not _is_partitioned(node):
            return
        scheme = node.data.get("partition:scheme")
        if not isinstance(scheme, str) or not scheme.strip():
            yield self.finding(
                node,
                "partitioned collection declares no partition:scheme",
                json_pointer="/partition:scheme",
            )
        keys = node.data.get("partition:keys")
        if not isinstance(keys, list) or not keys:
            yield self.finding(
                node,
                "partitioned collection declares no partition:keys",
                json_pointer="/partition:keys",
            )
        glob = node.data.get("partition:glob")
        if not isinstance(glob, str) or not glob.strip():
            yield self.finding(
                node,
                "partitioned collection declares no partition:glob, the normative"
                " bulk-access path for its partitions",
                json_pointer="/partition:glob",
                fix_hint="add partition:glob, e.g. 's3://bucket/data/*.parquet'",
            )
        if not _declares_extension(node):
            yield self.finding(
                node,
                "partition:* fields used without declaring the partition extension"
                " schema in stac_extensions",
                json_pointer="/stac_extensions",
                fix_hint=f"add '{_PARTITION_EXTENSION_PREFIX}v1.0.0/schema.json'"
                " to stac_extensions",
            )


def _is_partitioned(node: Node) -> bool:
    return any(key.startswith("partition:") for key in node.data) or _declares_extension(node)


def _declares_extension(node: Node) -> bool:
    extensions = node.data.get("stac_extensions")
    if not isinstance(extensions, list):
        return False
    return any(
        isinstance(uri, str) and uri.startswith(_PARTITION_EXTENSION_PREFIX) for uri in extensions
    )
