"""Classification rules (STAC classification extension, categorical rasters).

A categorical raster names what each pixel value means with
``classification:classes``. A single-band raster puts its class list on the
data asset; a multi-band raster puts it on the band it describes, in the STAC
1.1 ``bands`` array. Either way the list is next to the ``nodata`` it is read
against (PORTO-FMT-050).

The classification extension's own schema (v2.0.0) applies its class object
only to an object's top level, to ``raster:bands``, and to ``mlm:output``, and
``PTL-EXT-001`` validates those places. A class list inside core ``bands`` is
never reached, so ``PTL-CLS-002`` applies the class object there. A legacy
``raster:bands`` list that mirrors a list in ``bands`` or on the asset is left
to the list it mirrors, so one mistake earns one finding.

- ``PTL-CLS-001`` the object declares the classification extension.
- ``PTL-CLS-002`` every class has a ``title``; under core ``bands``, each
  class also follows the extension's class object.
- ``PTL-CLS-003`` a class is marked ``nodata`` if and only if its value is
  the band's ``nodata``.
- ``PTL-CLS-005`` the list is on a single-band data asset, or on its band in
  STAC 1.1 ``bands`` for a multi-band asset.
- ``PTL-CLS-006`` a collection of scene items has its items' class lists in
  the same place in ``item_assets``.

Whether a ``color_hint`` agrees with the colour table embedded in the COG
(``PTL-DAT-021``) is a bytes question, so it lives in the data pass, which
reads the lists through :func:`asset_class_lists`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib import resources
from typing import Any, TypeGuard

from rashid import _json
from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity, json_pointer
from rashid.rule import Rule
from rashid.rules._common import is_source_asset

CLASSES_FIELD = "classification:classes"
BAND_FIELDS = ("bands", "raster:bands")

# Version-tolerant prefix of the extension's schema URI, as the partition rule
# matches its own. The hint names the current release.
_EXTENSION_PREFIX = "https://stac-extensions.github.io/classification/"
_EXTENSION_URI = f"{_EXTENSION_PREFIX}v2.0.0/schema.json"

_ABSENT = object()

_CLASS_SCHEMA = "_schemas/extensions/stac-extensions.github.io/classification/v2.0.0/schema.json"


@lru_cache(maxsize=1)
def _list_schema() -> dict[str, Any]:
    """The vendored extension's schema for a ``classification:classes`` array."""
    schema = _json.loads(resources.files("rashid").joinpath(_CLASS_SCHEMA).read_bytes())
    definitions: dict[str, Any] = schema["definitions"]
    listed: dict[str, Any] = dict(definitions["fields"]["properties"][CLASSES_FIELD])
    listed["items"] = definitions["class_object"]
    return listed


# shortcut: reads only the keywords the v2.0.0 class list uses (type,
# required, pattern, minimum, maximum, minItems); extend it if a later schema
# adds others. The jsonschema validator PTL-EXT-001 uses is not loaded in the
# metadata pass.
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _type_error(value: Any, kind: str) -> bool:
    if kind == "integer":
        return not is_integer(value)
    # JSON true is a Python int; only the boolean type admits it.
    return not isinstance(value, _JSON_TYPES[kind]) or (
        isinstance(value, bool) and kind != "boolean"
    )


def class_object_errors(entry: Any) -> Iterator[tuple[str | None, str]]:
    """(field or None for the class, reason) for each way a class breaks the class object."""
    rule = _list_schema()["items"]
    if _type_error(entry, "object"):
        yield None, "is not an object"
        return
    for field in rule["required"]:
        if field not in entry:
            yield None, f"has no {field}"
    for field, limits in rule["properties"].items():
        if field not in entry:
            continue
        value = entry[field]
        if _type_error(value, limits["type"]):
            yield field, f"is not a JSON {limits['type']}"
        elif "pattern" in limits and not re.search(limits["pattern"], value):
            yield field, f"does not match {limits['pattern']}"
        elif "minimum" in limits and value < limits["minimum"]:
            yield field, f"is below the minimum of {limits['minimum']}"
        elif "maximum" in limits and value > limits["maximum"]:
            yield field, f"is above the maximum of {limits['maximum']}"


@dataclass(frozen=True)
class ClassList:
    """One ``classification:classes`` array and the band facts that bind it.

    ``nodata`` is the band's own value, or the enclosing asset's when the
    band leaves it out, as STAC 1.1 inherits it. ``_ABSENT`` marks a value
    declared at neither level. ``band_field`` and
    ``band_index`` locate the band in its asset's band list, so the data pass
    can pair it with the band of the same position in the file; both are None
    for a list declared outside a band list.
    """

    pointer: str
    classes: Any
    nodata: Any
    band_field: str | None = None
    band_index: int | None = None
    # On a source asset, which PORTO-FMT-045 exempts from Portolan's own
    # class requirements; the extension's class object still applies.
    source: bool = False

    def entries(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """(pointer, class object) for each class that is an object."""
        if not isinstance(self.classes, list):
            return
        for index, entry in enumerate(self.classes):
            if isinstance(entry, dict):
                yield json_pointer(index, prefix=self.pointer), entry


def asset_class_lists(pointer: str, asset: dict[str, Any]) -> list[ClassList]:
    """Every class list on one asset: its own, and each band's.

    A legacy ``raster:bands`` list is left out where it mirrors a list in
    ``bands`` or, for the asset's only band, on the asset.
    """
    nodata = asset.get("nodata", _ABSENT)
    found: list[ClassList] = []
    if CLASSES_FIELD in asset:
        found.append(
            ClassList(json_pointer(CLASSES_FIELD, prefix=pointer), asset[CLASSES_FIELD], nodata)
        )
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if not isinstance(bands, list):
            continue
        for index, band in enumerate(bands):
            if not isinstance(band, dict) or CLASSES_FIELD not in band:
                continue
            if field == "raster:bands" and _mirrors(asset, index, band):
                continue
            found.append(
                ClassList(
                    json_pointer(field, index, CLASSES_FIELD, prefix=pointer),
                    band[CLASSES_FIELD],
                    band.get("nodata", nodata),
                    field,
                    index,
                )
            )
    return found


def _home(asset: dict[str, Any], index: int) -> dict[str, Any] | None:
    """The ``bands`` entry, or the single-band asset, with classes for legacy band ``index``."""
    if _has_classes(asset, index):
        band: dict[str, Any] = asset["bands"][index]
        return band
    if index == 0 and band_count(asset) == 1 and CLASSES_FIELD in asset:
        return asset
    return None


def _mirrors(asset: dict[str, Any], index: int, legacy: dict[str, Any]) -> bool:
    """Whether a legacy ``raster:bands`` entry repeats the class list and nodata of its home."""
    home = _home(asset, index)
    if home is None or home.get(CLASSES_FIELD) != legacy[CLASSES_FIELD]:
        return False
    inherited = asset.get("nodata", _ABSENT)
    return bool(home.get("nodata", inherited) == legacy.get("nodata", inherited))


def _containers(node: Node) -> Iterator[tuple[str, dict[str, Any]]]:
    """(pointer, object) for every place a class list can sit on a node.

    The object's own fields (an item's ``properties``), each asset, and each
    ``item_assets`` template, whose classes items inherit.
    """
    if node.kind == "item" and isinstance(node.data.get("properties"), dict):
        yield "/properties", node.data["properties"]
    else:
        yield "", node.data
    for field in ("assets", "item_assets"):
        assets = node.data.get(field)
        if not isinstance(assets, dict):
            continue
        for key, asset in assets.items():
            if isinstance(asset, dict):
                yield json_pointer(field, key), asset


def class_lists(node: Node) -> list[ClassList]:
    """Every class list on a collection or item."""
    found: list[ClassList] = []
    for pointer, container in _containers(node):
        source = is_source_asset(container)
        found.extend(
            replace(listed, source=source) for listed in asset_class_lists(pointer, container)
        )
    return found


def _declares_extension(node: Node) -> bool:
    extensions = node.data.get("stac_extensions")
    if not isinstance(extensions, list):
        return False
    return any(isinstance(uri, str) and uri.startswith(_EXTENSION_PREFIX) for uri in extensions)


def _field_at(pointer: str, entry: dict[str, Any], field: str) -> str:
    """The pointer to a class field, or to the class when the field is absent."""
    return json_pointer(field, prefix=pointer) if field in entry else pointer


def _title_problem(entry: dict[str, Any]) -> bool:
    """Whether a class lacks the readable title PORTO-FMT-052 requires."""
    title = entry.get("title")
    return not isinstance(title, str) or not title.strip()


def is_integer(value: Any) -> TypeGuard[int]:
    """A JSON integer as draft-07 reads it: 1 or 1.0, but neither true nor 1.5."""
    # JSON true is a Python int; the schema's integer type excludes it.
    if isinstance(value, float):
        return value.is_integer()
    return isinstance(value, int) and not isinstance(value, bool)


class ClassificationExtensionRule(Rule):
    """An object with class lists declares the classification extension.

    The profile's extension registry (``stac/README.md``) lists the
    extension's schema URI for classified rasters with
    ``classification:classes``, at SHOULD.
    """

    id = "PTL-CLS-001"
    default_severity = Severity.WARNING
    description = "an object with classification:classes declares the classification extension"
    kinds = ("collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        lists = class_lists(node)
        if not lists or _declares_extension(node):
            return
        yield self.finding(
            node,
            f"{lists[0].pointer} uses classification:classes without declaring the"
            " classification extension schema in stac_extensions",
            json_pointer=json_pointer("stac_extensions") if "stac_extensions" in node.data else "",
            fix_hint=f"add '{_EXTENSION_URI}' to stac_extensions",
        )


class ClassListRule(Rule):
    """Every class has a title, and a list in core ``bands`` follows the class object.

    The title is Portolan's (PORTO-FMT-052). The extension's own rules are
    reported here only for a list in core ``bands``, which its schema never
    reaches; ``PTL-EXT-001`` reports them everywhere else.
    """

    id = "PTL-CLS-002"
    default_severity = Severity.ERROR
    description = (
        "each classification:classes entry has a title, and a list in core bands"
        " follows the classification extension's class object"
    )
    kinds = ("collection", "item")
    spec_ids = ("PORTO-FMT-052",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for found in class_lists(node):
            if found.band_field == "bands":
                yield from self._check_class_object(node, found)
            # PORTO-FMT-045 exempts a source asset from Portolan's own title.
            for pointer, entry in [] if found.source else found.entries():
                if _title_problem(entry):
                    yield self.finding(
                        node,
                        f"class {pointer} has no title",
                        json_pointer=_field_at(pointer, entry, "title"),
                        fix_hint="set title to the class's human-readable label,"
                        " e.g. 'Permanent water'",
                    )

    def _check_class_object(self, node: Node, found: ClassList) -> Iterator[Finding]:
        if _type_error(found.classes, "array") or len(found.classes) < _list_schema()["minItems"]:
            yield self.finding(
                node,
                f"{found.pointer} is not a non-empty array of class objects",
                json_pointer=found.pointer,
                fix_hint="list one object per class, each with a value and a name",
            )
            return
        seen: set[str] = set()
        for index, entry in enumerate(found.classes):
            pointer = json_pointer(index, prefix=found.pointer)
            # The schema's uniqueItems compares whole classes. Python's == calls
            # false and 0 equal, so compare the JSON text. shortcut: 1 and 1.0
            # then differ, which only misses a repeat; normalise numbers if it matters.
            text = json.dumps(entry, sort_keys=True)
            if text in seen:
                yield self.finding(
                    node,
                    f"class {pointer} repeats an earlier class",
                    json_pointer=pointer,
                    fix_hint="remove the repeated class",
                )
                continue
            seen.add(text)
            for field, reason in class_object_errors(entry):
                if field == "title" and not found.source:
                    continue  # the title check below reports it
                yield self.finding(
                    node,
                    f"class {pointer}{'' if field is None else ' ' + field} {reason}",
                    json_pointer=pointer if field is None else json_pointer(field, prefix=pointer),
                    fix_hint="correct the class as the classification extension's class"
                    " object defines it",
                    actual=entry if field is None else entry[field],
                )


def _same_number(nodata: Any, value: int) -> bool:
    # A nodata of "nan", "inf" or "-inf" names no integer, so no class value
    # can carry it.
    if isinstance(nodata, bool) or not isinstance(nodata, int | float):
        return False
    return nodata == value


class NodataClassRule(Rule):
    """A class is marked ``nodata`` if and only if its value is the band's nodata.

    A viewer draws nodata pixels as empty and leaves them out of the legend.
    A nodata class whose value is not the band's nodata contradicts the band,
    and so does a class at the band's nodata that is not marked, which the
    legend lists but the map never draws.
    """

    id = "PTL-CLS-003"
    default_severity = Severity.ERROR
    description = "a class is marked nodata if and only if its value is its band's nodata"
    kinds = ("collection", "item")
    spec_ids = ("PORTO-FMT-057",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for found in class_lists(node):
            if found.source:
                continue
            for pointer, entry in found.entries():
                value = entry.get("value")
                if not is_integer(value):
                    continue
                if entry.get("nodata") is not True:
                    if _same_number(found.nodata, value):
                        yield self.finding(
                            node,
                            f"class {pointer} has value {value}, its band's nodata, but is not"
                            " marked nodata",
                            json_pointer=_field_at(pointer, entry, "nodata"),
                            fix_hint="set nodata to true on the class",
                            expected=True,
                        )
                    continue
                if found.nodata is _ABSENT:
                    yield self.finding(
                        node,
                        f"class {pointer} is marked nodata, but its band declares no nodata value",
                        json_pointer=json_pointer("nodata", prefix=pointer),
                        fix_hint=f"set the band's nodata to {value}, or drop the class's"
                        " nodata flag",
                        expected=value,
                    )
                elif not _same_number(found.nodata, value):
                    yield self.finding(
                        node,
                        f"class {pointer} is marked nodata with value {value}, but its band's"
                        f" nodata is {found.nodata!r}",
                        json_pointer=json_pointer("value", prefix=pointer),
                        fix_hint="make the nodata class's value equal the band's nodata, or"
                        " drop the class's nodata flag",
                        expected=found.nodata,
                        actual=value,
                    )


def _has_classes(asset: dict[str, Any], index: int | None) -> bool:
    """Whether the asset (index None) or its band ``index`` in ``bands`` has classes."""
    if index is None:
        return CLASSES_FIELD in asset
    bands = asset.get("bands")
    if not isinstance(bands, list) or index >= len(bands):
        return False
    band = bands[index]
    return isinstance(band, dict) and CLASSES_FIELD in band


def band_count(asset: dict[str, Any]) -> int:
    """How many bands the asset declares; one when it lists none."""
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if isinstance(bands, list):
            return len(bands)
    return 1


def _misplaced(asset: dict[str, Any], found: ClassList) -> str | None:
    """Where a class list on an asset belongs, or None when it is already there.

    A single-band asset has the list itself; a multi-band asset has it on the
    band it describes in ``bands``. A legacy ``raster:bands`` list passes when
    that place has classes too.
    """
    multi = band_count(asset) > 1
    if multi and found.band_index is None:
        return "the band it describes in bands"
    if found.band_field == ("bands" if multi else None):
        return None
    if found.band_field == "raster:bands" and _home(asset, found.band_index or 0) is not None:
        return None
    return f"bands/{found.band_index}" if multi else "the asset"


class BandPlacementRule(Rule):
    """A class list sits on a single-band data asset, or on its band in ``bands``.

    The list is read against the ``nodata`` and ``data_type`` of the object
    it sits on. A single-band asset states them once, on the asset; a
    multi-band asset states them per band and has to say which band is
    classified. A list on the object itself rather than on an asset describes
    no file.
    """

    id = "PTL-CLS-005"
    default_severity = Severity.ERROR
    description = (
        "a classification:classes list sits on a single-band data asset, or on its band"
        " in the STAC 1.1 bands array of a multi-band one"
    )
    kinds = ("collection", "item")
    spec_ids = ("PORTO-FMT-050",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, container in _containers(node):
            is_asset = container is not node.data and container is not node.data.get("properties")
            if is_asset and is_source_asset(container):
                continue  # PORTO-FMT-045: format rules skip upstream source files
            for found in asset_class_lists(pointer, container):
                target = _misplaced(container, found) if is_asset else "the data asset"
                if target is None:
                    continue
                yield self.finding(
                    node,
                    f"{found.pointer} is not on {target}",
                    json_pointer=found.pointer,
                    fix_hint=f"move the list onto {target}, next to its nodata",
                )


def _same_place(template: ClassList, found: ClassList) -> bool:
    return template.band_field != "raster:bands" and template.band_index == found.band_index


class ItemAssetClassesRule(Rule):
    """A collection of scene items has its items' class lists in ``item_assets``.

    A client builds a legend from the collection alone, without opening an
    item, only when the collection's ``item_assets`` template has a class list
    in the same place as its items' assets (PORTO-FMT-051). An item list in
    the wrong place is ``PTL-CLS-005``'s.
    """

    id = "PTL-CLS-006"
    default_severity = Severity.WARNING
    description = "a collection has its items' class lists in the same place in item_assets"
    kinds = ("collection",)
    spec_ids = ("PORTO-FMT-051",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        templates = node.data.get("item_assets")
        templates = templates if isinstance(templates, dict) else None
        reported: set[tuple[str, int | None]] = set()
        for item in graph.items_of(node):
            assets = item.data.get("assets")
            if not isinstance(assets, dict):
                continue
            for key, asset in assets.items():
                # PORTO-FMT-045: format rules skip upstream source files.
                if not isinstance(asset, dict) or is_source_asset(asset):
                    continue
                for found in asset_class_lists("", asset):
                    place = (key, found.band_index)
                    # A legacy list that passes placement has its home list beside it.
                    if (
                        found.band_field == "raster:bands"
                        or place in reported
                        or _misplaced(asset, found) is not None
                    ):
                        continue
                    if not self._in_template(key, found, templates):
                        reported.add(place)
                        yield self._missing(node, item, key, found, templates)

    @staticmethod
    def _in_template(key: str, found: ClassList, templates: dict[str, Any] | None) -> bool:
        template = templates.get(key) if templates is not None else None
        if not isinstance(template, dict):
            return False
        return any(_same_place(listed, found) for listed in asset_class_lists("", template))

    def _missing(
        self,
        node: Node,
        item: Node,
        key: str,
        found: ClassList,
        templates: dict[str, Any] | None,
    ) -> Finding:
        place = f"asset '{key}'"
        target = ""
        if found.band_index is not None:
            place = f"band {found.band_index} of {place}"
            target = f"bands/{found.band_index}/"
        if templates is not None and isinstance(templates.get(key), dict):
            pointer = json_pointer("item_assets", key)
        elif templates is not None:
            pointer = json_pointer("item_assets")
        else:
            pointer = ""
        return self.finding(
            node,
            f"items such as {item.path.as_posix()} have classes on {place},"
            f" but item_assets/{key} does not",
            json_pointer=pointer,
            fix_hint=f"copy the class list into item_assets/{key}/{target}{CLASSES_FIELD}",
        )
