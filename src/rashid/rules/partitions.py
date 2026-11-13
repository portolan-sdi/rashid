"""Partition rules (spec: formats.md, Partitioned Collections).

formats.md: "Partitioning MUST follow the partition extension v1.0.0. Declare
its schema in `stac_extensions`, and set `partition:scheme`, `partition:keys`,
and `partition:glob`." And: "`partition:glob` MUST be an absolute URL with a
scheme that supports listing. Publishers SHOULD use `s3://`, `gs://`, or
`az://`."

A collection is taken to be partitioned when it declares any ``partition:*``
field or the partition extension URI — the signals the Portolan partition
extension writes.
"""

from __future__ import annotations

from collections.abc import Iterable

from rashid._href import split_href
from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity, json_pointer
from rashid.rule import Rule

# Version-tolerant prefix of the partition extension's schema URI, matching
# how the web-map-links declaration is checked in the viz rules.
_PARTITION_EXTENSION_PREFIX = "https://schemas.portolan-sdi.org/incubating/partition/"


class PartitionFieldsRule(Rule):
    """A partitioned collection declares the extension and its required fields.

    One finding per missing piece: the extension URI in ``stac_extensions``,
    and each of ``partition:scheme``, ``partition:keys``, ``partition:glob``.
    A glob mentioned only in the description does not satisfy the field.
    """

    id = "PTL-PRT-001"
    spec_ids = ("PORTO-FMT-017",)
    default_severity = Severity.ERROR
    description = (
        "a partitioned collection must declare the partition extension and carry"
        " partition:scheme, partition:keys, and partition:glob"
    )
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if not is_partitioned(node):
            return
        scheme = node.data.get("partition:scheme")
        if not isinstance(scheme, str) or not scheme.strip():
            yield self.finding(
                node,
                "partitioned collection declares no partition:scheme",
                json_pointer=json_pointer("partition:scheme"),
                fix_hint="add partition:scheme naming the directory layout, e.g. 'hive'",
            )
        keys = node.data.get("partition:keys")
        if not isinstance(keys, list) or not keys:
            yield self.finding(
                node,
                "partitioned collection declares no partition:keys",
                json_pointer=json_pointer("partition:keys"),
                fix_hint='add partition:keys listing the partition columns, e.g. ["year"]',
            )
        glob = node.data.get("partition:glob")
        if not isinstance(glob, str) or not glob.strip():
            yield self.finding(
                node,
                "partitioned collection declares no partition:glob, the normative"
                " bulk-access path for its partitions",
                json_pointer=json_pointer("partition:glob"),
                fix_hint="add partition:glob, e.g. 's3://bucket/data/*.parquet'",
            )
        if not _declares_extension(node):
            yield self.finding(
                node,
                "partition:* fields used without declaring the partition extension"
                " schema in stac_extensions",
                json_pointer=json_pointer("stac_extensions"),
                fix_hint=f"add '{_PARTITION_EXTENSION_PREFIX}v1.0.0/schema.json'"
                " to stac_extensions",
            )


def is_partitioned(node: Node) -> bool:
    return any(key.startswith("partition:") for key in node.data) or _declares_extension(node)


def _declares_extension(node: Node) -> bool:
    extensions = node.data.get("stac_extensions")
    if not isinstance(extensions, list):
        return False
    return any(
        isinstance(uri, str) and uri.startswith(_PARTITION_EXTENSION_PREFIX) for uri in extensions
    )


class PartitionGlobSchemeRule(Rule):
    """``partition:glob`` is an absolute URL that clients can list.

    An ERROR under PORTO-FMT-064: a relative path, an ``http(s)://`` URL, a
    Windows drive path, a URL with no host or bucket, or a ``file`` URL
    without an absolute path.
    A WARNING under PORTO-FMT-075: any other scheme than ``s3``, ``gs``, or
    ``az``.
    """

    id = "PTL-PRT-002"
    spec_ids = ("PORTO-FMT-064", "PORTO-FMT-075")
    default_severity = Severity.ERROR
    description = "partition:glob must be an absolute URL that clients can list"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        glob = node.data.get("partition:glob")
        if not isinstance(glob, str) or not glob.strip():
            return  # PTL-PRT-001 reports a missing glob
        parsed = split_href(glob)
        scheme = parsed.scheme.lower() if parsed is not None else ""
        bucket = parsed.hostname if parsed is not None else None
        path = parsed.path if parsed is not None else ""
        # A one-letter scheme is a Windows drive letter, not a URL scheme.
        unlistable = len(scheme) < 2 or scheme in ("http", "https")
        located = path.startswith("/") if scheme == "file" else bool(bucket)
        if unlistable or not located:
            yield self.finding(
                node,
                "partition:glob is not an absolute URL that clients can list, such as"
                " s3://bucket/path; a relative path or HTTP URL cannot be listed",
                json_pointer=json_pointer("partition:glob"),
                fix_hint=_GLOB_HINT,
                actual=glob,
            )
        elif scheme not in _RECOMMENDED_SCHEMES:
            yield self.finding(
                node,
                f"partition:glob uses scheme '{scheme}'; readers support s3, gs, and az",
                severity=Severity.WARNING,
                json_pointer=json_pointer("partition:glob"),
                fix_hint=_GLOB_HINT,
                actual=glob,
            )


_RECOMMENDED_SCHEMES = ("s3", "gs", "az")
_GLOB_HINT = "use the absolute bucket URL, e.g. 's3://bucket/data/*.parquet'"
