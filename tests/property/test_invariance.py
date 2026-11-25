"""Invariance: a valid catalog stays valid under changes the spec says do not matter.

A generated catalog validates clean. It still does after any composition of
the forms a ``MAY`` permits (``tests/property/permitted.py``) followed by a
re-serialization of every JSON file that RFC 8259 says changes nothing.

Not transformed, for want of a sentence that permits it: the order of a
``links`` array. Neither the Portolan spec nor STAC 1.1.0
(``commons/links.md``) says link order carries no meaning. Provider order does
carry meaning: PORTO-CORE-047 puts the host last.
"""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rashid_core import ValidationOptions, validate
from tests.property.permitted import FORMS, GEOPARQUET_FORMS, METADATA_ONLY, ZARR_FORMS
from tests.property.strategies import CatalogPlan, catalogs

pytestmark = pytest.mark.integration


def _shuffled(value: Any, rng: random.Random) -> Any:
    """RFC 8259, section 4: an object is an unordered collection of members,
    so every object's members are reordered at every depth."""
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        return {key: _shuffled(value[key], rng) for key in keys}
    if isinstance(value, list):
        return [_shuffled(member, rng) for member in value]
    return value


def _rename_asset_keys(data: dict[str, Any], rng: random.Random) -> None:
    """STAC 1.1.0 commons/assets.md: "In general, the keys don't have any
    meaning and are considered to be non-descriptive unique identifiers." core.md,
    Visualization Styles, repeats it: STAC assets are an unordered JSON object
    whose keys carry no meaning a client is expected to understand."""
    assets = data.get("assets")
    if isinstance(assets, dict):
        data["assets"] = {
            f"asset-{rng.randrange(10**9)}-{i}": v for i, v in enumerate(assets.values())
        }


@st.composite
def _formats(draw: st.DrawFn) -> dict[str, Any]:
    """RFC 8259, section 2: insignificant whitespace is allowed before or after
    any of the six structural characters, and around the JSON text. Section 7:
    any character may be escaped, so non-ASCII text may be written as \\u escapes."""
    space = st.sampled_from(["", " ", "\n", "\t", "\r\n  "])
    return {
        "indent": draw(st.sampled_from([None, 0, 1, 2, 4, "\t"])),
        "separators": (draw(space) + "," + draw(space), draw(space) + ":" + draw(space)),
        "ensure_ascii": draw(st.booleans()),
        "padding": (draw(space), draw(space)),
    }


def _reformat(root: Path, rng: random.Random, style: dict[str, Any], rename_keys: bool) -> None:
    before, after = style["padding"]
    for path in sorted(root.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if rename_keys and "stac_version" in data:
            _rename_asset_keys(data, rng)
        text = json.dumps(
            _shuffled(data, rng),
            indent=style["indent"],
            separators=style["separators"],
            ensure_ascii=style["ensure_ascii"],
        )
        path.write_text(before + text + after, encoding="utf-8")


def _report(root: Path, metadata_only: bool = False) -> list[tuple[str, str, str]]:
    report = validate(root, options=ValidationOptions(data=not metadata_only))
    return [(f.rule_id, f.path, f.message) for f in report.findings]


@given(catalogs())
def test_a_generated_catalog_is_clean(plan: CatalogPlan) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert _report(plan.write(Path(tmp) / "catalog")) == []


#: Compositions of permitted forms, each drawn from one data family.
_COMPOSITIONS = st.sampled_from([ZARR_FORMS, GEOPARQUET_FORMS]).flatmap(
    lambda excluded: st.lists(st.sampled_from(sorted(set(FORMS) - excluded)), unique=True)
)


@given(
    catalogs(),
    _COMPOSITIONS,
    st.booleans(),
    _formats(),
    st.randoms(use_true_random=False),
)
def test_permitted_changes_keep_it_clean(
    plan: CatalogPlan,
    forms: list[str],
    rename_keys: bool,
    style: dict[str, Any],
    rng: random.Random,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = plan.write(Path(tmp) / "catalog")
        for requirement in forms:
            FORMS[requirement](root)
        _reformat(root, rng, style, rename_keys)
        assert _report(root, metadata_only=bool(METADATA_ONLY & set(forms))) == []
