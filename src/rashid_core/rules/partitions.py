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

import re
from collections.abc import Iterable

from rashid_core._href import BUCKET_SCHEMES, bucket_glob, split_href
from rashid_core._stac import PARTITION_EXTENSION_PREFIX, declared_extensions, is_partitioned
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.model import Finding, Severity, json_pointer
from rashid_core.rule import Rule

# formats.md requires the partition extension v1.0.0. A v1.x minor or patch
# release is compatible with it.
_PARTITION_V1 = re.compile(re.escape(PARTITION_EXTENSION_PREFIX) + r"v1\.\d+\.\d+/schema\.json")


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
        declared = declared_extensions(node)
        if not declared:
            yield self.finding(
                node,
                "partition:* fields used without declaring the partition extension"
                " schema in stac_extensions",
                json_pointer=json_pointer("stac_extensions"),
                fix_hint=f"add '{_PARTITION_V1_URI}' to stac_extensions",
            )
        elif not any(_PARTITION_V1.fullmatch(uri) for _, uri in declared):
            index, uri = declared[0]
            yield self.finding(
                node,
                "the partition extension is declared at a version other than v1.x;"
                " the spec requires v1.0.0",
                json_pointer=json_pointer("stac_extensions", index),
                fix_hint=f"replace '{uri}' with '{_PARTITION_V1_URI}'",
                expected=_PARTITION_V1_URI,
                actual=uri,
            )


_PARTITION_V1_URI = f"{PARTITION_EXTENSION_PREFIX}v1.0.0/schema.json"


class PartitionGlobSchemeRule(Rule):
    """``partition:glob`` is an absolute URL that clients can list.

    :func:`rashid_core._href.bucket_glob` decides what a client can list. An
    ``s3``, ``gs``, or ``az`` URL with a bucket and a key pattern passes.
    An ERROR under PORTO-FMT-064: a relative path, an ``http(s)://`` URL, a
    ``file`` URL, which names a path on one machine, a Windows drive path,
    or a URL with no host, bucket, or key pattern.
    A WARNING under PORTO-FMT-075: any other scheme with a host.
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
        if bucket_glob(glob) is not None:
            return
        parsed = split_href(glob)
        host = parsed.hostname if parsed is not None else None
        scheme = parsed.scheme.lower() if parsed is not None else ""
        # A one-letter scheme is a Windows drive letter, not a URL scheme.
        unlistable = len(scheme) < 2 or scheme in (*_UNLISTABLE, *BUCKET_SCHEMES)
        if unlistable or not host:
            yield self.finding(
                node,
                "partition:glob is not an absolute URL that clients can list, such as"
                " s3://bucket/path; a relative path, a file URL, or an HTTP URL cannot"
                " be listed",
                json_pointer=json_pointer("partition:glob"),
                fix_hint=_GLOB_HINT,
                actual=glob,
            )
        else:
            yield self.finding(
                node,
                f"partition:glob uses scheme '{scheme}'; readers support s3, gs, and az",
                severity=Severity.WARNING,
                json_pointer=json_pointer("partition:glob"),
                fix_hint=_GLOB_HINT,
                actual=glob,
            )


# Schemes no client can list from elsewhere: HTTP has no listing, and a file
# URL names a path on the publisher's own machine.
_UNLISTABLE = ("http", "https", "file")
_GLOB_HINT = "use the absolute bucket URL, e.g. 's3://bucket/data/*.parquet'"
