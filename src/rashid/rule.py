"""Rule abstraction for the metadata validation pass."""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterable
from typing import Any, ClassVar

from rashid.catalog import CatalogGraph, Kind, Node
from rashid.model import Finding, Severity


class Rule(ABC):
    """A single validation rule.

    A rule visits nodes of the kinds in ``kinds`` and yields zero or more
    findings per node; no findings means the node passes. A rule with
    ``kinds = ()`` is graph-level: it runs once via ``check_graph``.
    """

    id: ClassVar[str]
    default_severity: ClassVar[Severity]
    description: ClassVar[str]
    kinds: ClassVar[tuple[Kind, ...]] = ()
    #: Requirement IDs from the spec's requirements manifest
    #: (``specs/portolan/requirements.yaml``) that this rule enforces.
    #: ``tests/unit/test_spec_coverage.py`` gates on these: every
    #: ``enforcement: validator`` MUST/SHOULD in the manifest must be cited
    #: by at least one check. Empty only when a rule enforces spec text that
    #: carries no RFC 2119 keyword.
    spec_ids: ClassVar[tuple[str, ...]] = ()

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        return ()

    def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
        return ()

    def finding(
        self,
        node: Node,
        message: str,
        *,
        json_pointer: str | None = None,
        fix_hint: str | None = None,
        expected: Any | None = None,
        actual: Any | None = None,
        data: dict[str, Any] | None = None,
    ) -> Finding:
        """Build a finding for ``node`` with this rule's id and severity."""
        return Finding(
            rule_id=self.id,
            severity=self.default_severity,
            message=message,
            path=str(node.path),
            object_id=node.id,
            json_pointer=json_pointer,
            fix_hint=fix_hint,
            expected=expected,
            actual=actual,
            data=data,
        )
