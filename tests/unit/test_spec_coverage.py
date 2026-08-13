"""The spec-coverage gate: every machine-checkable requirement is enforced.

The spec's requirements manifest (``specs/portolan/requirements.yaml``,
vendored to ``tests/fixtures/requirements.yaml`` by
``scripts/vendor_spec_fixtures.py``) assigns every normative statement a
stable ID, an RFC 2119 severity, and an enforcement class: ``validator``
(rashid must cite it) or ``process`` (not machine-checkable). Each rashid check
declares the manifest IDs it enforces — ``Rule.spec_ids`` for metadata rules,
a module-level ``SPEC_IDS`` registry for the structural, schema, runner, data,
and live checks — and ``rashid.registry.CHECKS`` joins both into one id-space,
carrying the strongest severity each check can emit.

This module gates the join in both directions:

- every ``enforcement: validator`` requirement at MUST or SHOULD is cited by
  at least one check;
- every ``enforcement: validator`` requirement at MAY is either cited by a
  check or covered by a permission test in ``test_may_permissions.py``, which
  asserts rashid reports nothing for the form the requirement permits;
- every cited ID exists in the manifest;
- severities map: a MUST is cited by at least one ERROR-severity check, a
  SHOULD by at least one WARNING-or-stronger check, except where the spec
  itself pins a softer severity (the documented exception table);
- every check that cites spec IDs is exercised by at least one test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import rashid.data as data_pass
import rashid.live as live_pass
import rashid.runner as runner
import rashid.schema as schema_pass
import rashid.structural as structural_pass
import tests.unit.test_may_permissions as may_permissions
from rashid.model import Severity
from rashid.registry import CHECKS

pytestmark = pytest.mark.unit

_TESTS_DIR = Path(__file__).resolve().parent.parent
_MANIFEST = _TESTS_DIR / "fixtures" / "requirements.yaml"

# Manifest entries keep a fixed field order (id, severity, enforcement, file,
# quote), possibly with comment lines between fields; the spec repo's
# check_requirements.py validates the file upstream, so a plain scan is
# reliable and keeps rashid free of a YAML dependency.
_COMMENTS = r"(?:\s*#[^\n]*\n)*"
_ENTRY = re.compile(
    r"- id: (PORTO-(?:CORE|FMT)-\d{3})\s*\n"
    + _COMMENTS
    + r"\s+severity: (MUST|SHOULD|MAY)\s*\n"
    + _COMMENTS
    + r"\s+enforcement: (validator|process)\s*\n"
)

# MUST requirements deliberately not enforced by any ERROR-severity check.
# Every entry quotes the spec text that mandates the softer severity; an
# entry that stops being needed fails the gate so the table cannot rot.
_SEVERITY_EXCEPTIONS: dict[str, str] = {
    # core.md, Conformance and Versioning: "A validator MUST flag any catalog
    # or collection whose declared URI differs from the root catalog's; such
    # a mismatch is a warning, not an error, and a mixed-version catalog
    # remains valid." The MUST is to flag; the spec pins the finding to
    # warning (PTL-CNF-002).
    "PORTO-CORE-010": "spec pins the schema-URI mismatch to a warning",
    # core.md, Human-Readable Titles: "titles MUST be human-readable" paired
    # with "A validator checks readability heuristically; it MUST flag a
    # title that fails the check, and because heuristics misfire, the finding
    # is a warning, not an error." (PTL-TTL-002)
    "PORTO-CORE-038": "spec pins the heuristic readability finding to a warning",
    # Same clause: PORTO-CORE-040's own MUST ("it MUST flag") is discharged by
    # PTL-TTL-002 flagging at the warning severity the sentence prescribes.
    "PORTO-CORE-040": "spec pins the heuristic readability finding to a warning",
}

_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


def _manifest_entries() -> list[tuple[str, str, str]]:
    """(id, severity, enforcement) triples from the vendored manifest."""
    entries = _ENTRY.findall(_MANIFEST.read_text(encoding="utf-8"))
    assert len(entries) > 100, "manifest scan found suspiciously few entries"
    return entries


def _checks() -> dict[str, tuple[tuple[str, ...], Severity]]:
    """Every check: id -> (cited manifest IDs, strongest severity)."""
    return {info.id: (info.spec_ids, info.severity) for info in CHECKS.values()}


def _citations() -> dict[str, list[tuple[str, Severity]]]:
    """Manifest ID -> the (check id, severity) pairs citing it."""
    cited: dict[str, list[tuple[str, Severity]]] = {}
    for check_id, (spec_ids, severity) in _checks().items():
        for spec_id in spec_ids:
            cited.setdefault(spec_id, []).append((check_id, severity))
    return cited


def test_manifest_ids_are_unique() -> None:
    ids = [entry[0] for entry in _manifest_entries()]
    assert len(ids) == len(set(ids))


def test_every_validator_must_and_should_is_cited() -> None:
    cited = _citations()
    missing = [
        f"{req_id} ({severity})"
        for req_id, severity, enforcement in _manifest_entries()
        if enforcement == "validator" and severity in ("MUST", "SHOULD") and req_id not in cited
    ]
    assert not missing, f"validator requirements cited by no check: {missing}"


def _validator_mays() -> list[str]:
    """Every ``MAY`` requirement the manifest assigns to the validator."""
    return [
        req_id
        for req_id, severity, enforcement in _manifest_entries()
        if enforcement == "validator" and severity == "MAY"
    ]


def test_every_validator_may_is_cited_or_has_a_permission_test() -> None:
    """A MAY is a permission, so the proof is that rashid does not report it.

    Two discharges count. A check may cite the requirement, which is how a MAY
    that permits a validator to *say* something is covered (``PORTO-CORE-055``,
    ``PORTO-CORE-066``). Otherwise a permission test must supply the permitted
    form and assert an empty report.
    """
    cited = _citations()
    covered = set(cited) | set(may_permissions.PERMISSION_TESTS)
    missing = [req_id for req_id in _validator_mays() if req_id not in covered]
    assert not missing, (
        "validator MAY requirements with neither a citation nor a permission test: "
        f"{missing}; add one to tests/unit/test_may_permissions.py"
    )


def test_every_permission_test_named_in_the_registry_exists() -> None:
    """The registry cannot outlive the tests it names.

    Deleting a permission test leaves its entry pointing at nothing, which
    fails here; deleting the entry too fails the coverage gate above. Either
    way the requirement cannot lose its cover silently.
    """
    for req_id, name in may_permissions.PERMISSION_TESTS.items():
        test = getattr(may_permissions, name, None)
        assert callable(test), f"{req_id} names a permission test that does not exist: {name}"
        assert name.startswith("test_"), f"{req_id} names {name}, which pytest will not collect"


def test_permission_tests_claim_only_validator_mays() -> None:
    """An entry for a requirement that is no longer a validator MAY is stale."""
    mays = set(_validator_mays())
    stale = [req_id for req_id in may_permissions.PERMISSION_TESTS if req_id not in mays]
    assert not stale, f"permission tests claim requirements that are not validator MAYs: {stale}"


def test_every_cited_id_exists_in_the_manifest() -> None:
    known = {entry[0] for entry in _manifest_entries()}
    unknown = [
        f"{check_id} cites {spec_id}"
        for check_id, (spec_ids, _severity) in _checks().items()
        for spec_id in spec_ids
        if spec_id not in known
    ]
    assert not unknown, f"checks cite IDs absent from the manifest: {unknown}"


def test_severity_mapping() -> None:
    cited = _citations()
    floors = {"MUST": _RANK[Severity.ERROR], "SHOULD": _RANK[Severity.WARNING]}
    weak: list[str] = []
    for req_id, severity, enforcement in _manifest_entries():
        if enforcement != "validator" or severity not in floors or req_id not in cited:
            continue
        strongest = max(_RANK[s] for _c, s in cited[req_id])
        if req_id in _SEVERITY_EXCEPTIONS:
            # An exception that is no longer needed must be removed.
            assert severity == "MUST", f"{req_id}: exceptions are for MUST entries"
            assert strongest < _RANK[Severity.ERROR], f"stale severity exception: {req_id}"
            assert strongest >= _RANK[Severity.WARNING], f"{req_id} not even warned about"
            continue
        if strongest < floors[severity]:
            weak.append(f"{req_id} ({severity}) strongest citation is below the floor")
    assert not weak, weak


def _constant_aliases() -> dict[str, set[str]]:
    """Check id -> the module-constant names tests may reference it by."""
    aliases: dict[str, set[str]] = {}
    for module in (runner, structural_pass, schema_pass, data_pass, live_pass):
        for name, value in vars(module).items():
            if isinstance(value, str) and re.fullmatch(r"PTL-[A-Z]{3}-\d{3}", value):
                aliases.setdefault(value, set()).add(name)
    return aliases


def test_every_citing_check_is_exercised_by_a_test() -> None:
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(_TESTS_DIR.rglob("*.py"))
        if path.name != Path(__file__).name
    )
    aliases = _constant_aliases()
    untested = [
        check_id
        for check_id, (spec_ids, _severity) in _checks().items()
        if spec_ids
        and check_id not in corpus
        and not any(name in corpus for name in aliases.get(check_id, ()))
    ]
    assert not untested, f"checks citing spec IDs but referenced by no test: {untested}"
