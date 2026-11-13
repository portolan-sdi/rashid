"""Classification rules (STAC classification extension, categorical rasters).

A categorical raster names what each pixel value means with
``classification:classes``. A single-band raster puts its class list on the
data asset; a multi-band raster puts it on the band it describes, in the STAC
1.1 ``bands`` array. Either way the list sits next to the ``nodata`` and
``data_type`` it is read against (PORTO-FMT-050).

The classification extension's own schema (v2.0.0) applies its class object
only to an object's top level, to ``raster:bands``, and to ``mlm:output``. A
class list inside core ``bands`` is never reached, and the extension is not in
the profile's registry, so ``PTL-EXT-001`` does not validate it either. These
rules check every class list wherever it sits, legacy ``raster:bands``
included:

- ``PTL-CLS-001`` the object declares the classification extension.
- ``PTL-CLS-002`` the list follows the extension's class object: an integer
  ``value`` and a ``name`` of letters, digits, ``-`` and ``_``, each unique
  within the list, a ``title``, and a ``color_hint`` of six hex digits
  without ``#``.
- ``PTL-CLS-003`` a class is marked ``nodata`` if and only if its value is
  the band's ``nodata``.
- ``PTL-CLS-004`` every class value fits the band's ``data_type``.
- ``PTL-CLS-005`` the list sits on a single-band data asset, or on its band
  in STAC 1.1 ``bands`` for a multi-band asset.
- ``PTL-CLS-006`` a collection of scene items repeats its items' class lists
  in ``item_assets``.

Whether a ``color_hint`` agrees with the colour table embedded in the COG
(``PTL-DAT-021``), and whether a palette band has classes at all
(``PTL-DAT-022``, an INFO hint), are bytes questions, so they live in the data pass, which
reads the lists through :func:`asset_class_lists`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, TypeGuard

from rashid import _json
from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity, json_pointer
from rashid.rule import Rule

CLASSES_FIELD = "classification:classes"
BAND_FIELDS = ("bands", "raster:bands")

# Version-tolerant prefix of the extension's schema URI, as the partition rule
# matches its own. The hint names the current release.
_EXTENSION_PREFIX = "https://stac-extensions.github.io/classification/"
_EXTENSION_URI = f"{_EXTENSION_PREFIX}v2.0.0/schema.json"

# The class object's patterns in the extension's v2.0.0 schema.
_NAME_PATTERN = re.compile(r"^[0-9A-Za-z\-_]+$")
_COLOR_HINT_PATTERN = re.compile(r"^[0-9A-Fa-f]{6}$")

# STAC common metadata data types with an integer range. Floating-point and
# complex types hold every class value a catalog would plausibly declare.
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "int8": (-(2**7), 2**7 - 1),
    "int16": (-(2**15), 2**15 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint8": (0, 2**8 - 1),
    "uint16": (0, 2**16 - 1),
    "uint32": (0, 2**32 - 1),
    "uint64": (0, 2**64 - 1),
}

_ABSENT = object()

_CLASS_SCHEMA = "_schemas/extensions/stac-extensions.github.io/classification/v2.0.0/schema.json"
# Fields the rules above check with their own messages.
_HAND_CHECKED = frozenset({"value", "name", "title", "color_hint", "nodata"})
_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


@lru_cache(maxsize=1)
def _class_properties() -> dict[str, dict[str, Any]]:
    """The field schemas of the vendored extension's class object."""
    schema = _json.loads(resources.files("rashid").joinpath(_CLASS_SCHEMA).read_bytes())
    properties: dict[str, dict[str, Any]] = schema["definitions"]["class_object"]["properties"]
    return properties


def _optional_field_errors(entry: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """(field, reason) for each other class field the extension's schema rejects.

    shortcut: reads only type, minimum and maximum, the keywords those fields
    use in v2.0.0; extend it if a later schema adds others.
    """
    for field, rule in _class_properties().items():
        if field in _HAND_CHECKED or field not in entry:
            continue
        value, kind = entry[field], rule.get("type", "")
        # Draft-07 counts an integral float such as 40.0 as an integer.
        integral = isinstance(value, float) and value.is_integer()
        if (
            not isinstance(value, _JSON_TYPES.get(kind, object))
            and not (kind == "integer" and integral)
        ) or (isinstance(value, bool) and kind != "boolean"):
            yield field, f"is not a JSON {kind}"
        elif "minimum" in rule and value < rule["minimum"]:
            yield field, f"is below the minimum of {rule['minimum']}"
        elif "maximum" in rule and value > rule["maximum"]:
            yield field, f"is above the maximum of {rule['maximum']}"


@dataclass(frozen=True)
class ClassList:
    """One ``classification:classes`` array and the band facts that bind it.

    ``nodata`` and ``data_type`` are the band's own values, or the enclosing
    asset's when the band leaves them out, as STAC 1.1 inherits them.
    ``_ABSENT`` marks a value declared at neither level. ``band_field`` and
    ``band_index`` locate the band in its asset's band list, so the data pass
    can pair it with the band of the same position in the file; both are None
    for a list declared outside a band list.
    """

    pointer: str
    classes: Any
    nodata: Any
    data_type: Any
    band_field: str | None = None
    band_index: int | None = None

    def entries(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """(pointer, class object) for each class that is an object."""
        if not isinstance(self.classes, list):
            return
        for index, entry in enumerate(self.classes):
            if isinstance(entry, dict):
                yield json_pointer(index, prefix=self.pointer), entry


def asset_class_lists(pointer: str, asset: dict[str, Any]) -> list[ClassList]:
    """Every class list on one asset: its own, and each band's."""
    nodata = asset.get("nodata", _ABSENT)
    data_type = asset.get("data_type", _ABSENT)
    found: list[ClassList] = []
    if CLASSES_FIELD in asset:
        found.append(
            ClassList(
                json_pointer(CLASSES_FIELD, prefix=pointer),
                asset[CLASSES_FIELD],
                nodata,
                data_type,
            )
        )
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if not isinstance(bands, list):
            continue
        for index, band in enumerate(bands):
            if isinstance(band, dict) and CLASSES_FIELD in band:
                found.append(
                    ClassList(
                        json_pointer(field, index, CLASSES_FIELD, prefix=pointer),
                        band[CLASSES_FIELD],
                        band.get("nodata", nodata),
                        band.get("data_type", data_type),
                        field,
                        index,
                    )
                )
    return found


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
        found.extend(asset_class_lists(pointer, container))
    return found


def _declares_extension(node: Node) -> bool:
    extensions = node.data.get("stac_extensions")
    if not isinstance(extensions, list):
        return False
    return any(isinstance(uri, str) and uri.startswith(_EXTENSION_PREFIX) for uri in extensions)


def _is_integer(value: Any) -> TypeGuard[int]:
    # JSON true is a Python int; the schema's integer type excludes it.
    return isinstance(value, int) and not isinstance(value, bool)


class ClassificationExtensionRule(Rule):
    """An object that carries class lists declares the classification extension."""

    id = "PTL-CLS-001"
    default_severity = Severity.ERROR
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
            json_pointer="/stac_extensions",
            fix_hint=f"add '{_EXTENSION_URI}' to stac_extensions",
        )


class ClassListRule(Rule):
    """Every class list follows the extension's class object.

    The schema's ``uniqueItems`` compares whole objects, so two classes with
    the same value and different names pass it. A class value is a pixel
    value, and a pixel cannot belong to two classes, so values must be unique;
    names identify classes to machines, so they must be unique too.
    """

    id = "PTL-CLS-002"
    default_severity = Severity.ERROR
    description = (
        "each classification:classes entry has a unique integer value, a unique"
        " name of letters, digits, '-' and '_', a title, and a six-hex-digit color_hint"
    )
    kinds = ("collection", "item")
    spec_ids = ("PORTO-FMT-052",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for found in class_lists(node):
            yield from self._check_list(node, found)

    def _check_list(self, node: Node, found: ClassList) -> Iterator[Finding]:
        if not isinstance(found.classes, list) or not found.classes:
            yield self.finding(
                node,
                f"{found.pointer} is not a non-empty array of class objects",
                json_pointer=found.pointer,
                fix_hint="list one object per class, each with a value and a name",
            )
            return
        values: dict[int, str] = {}
        names: dict[str, str] = {}
        for index, entry in enumerate(found.classes):
            pointer = json_pointer(index, prefix=found.pointer)
            if not isinstance(entry, dict):
                yield self.finding(
                    node,
                    f"class {pointer} is not an object",
                    json_pointer=pointer,
                    fix_hint="replace the entry with an object carrying value and name",
                )
                continue
            yield from self._check_value(node, pointer, entry, values)
            yield from self._check_name(node, pointer, entry, names)
            if not isinstance(entry.get("title"), str) or not entry["title"].strip():
                yield self.finding(
                    node,
                    f"class {pointer} has no title",
                    json_pointer=json_pointer("title", prefix=pointer),
                    fix_hint="set title to the class's human-readable label, e.g. 'Permanent water'",
                )
            yield from self._check_color_hint(node, pointer, entry)
            yield from self._check_fields(
                node, pointer, entry, in_bands=found.band_field == "bands"
            )

    def _check_fields(
        self, node: Node, pointer: str, entry: dict[str, Any], *, in_bands: bool
    ) -> Iterator[Finding]:
        if "nodata" in entry and not isinstance(entry["nodata"], bool):
            yield self.finding(
                node,
                f"class {pointer} has a nodata flag that is not a boolean",
                json_pointer=json_pointer("nodata", prefix=pointer),
                fix_hint="set nodata to true or false",
            )
        # The extension's schema checks these fields on assets and raster:bands
        # (PTL-EXT-001) but never reaches core bands.
        if not in_bands:
            return
        for field, reason in _optional_field_errors(entry):
            yield self.finding(
                node,
                f"class {pointer} {field} {reason}",
                json_pointer=json_pointer(field, prefix=pointer),
                fix_hint=f"correct {field} as the classification extension's class object defines it",
                actual=entry[field],
            )

    def _check_value(
        self, node: Node, pointer: str, entry: dict[str, Any], seen: dict[int, str]
    ) -> Iterator[Finding]:
        value = entry.get("value")
        if not _is_integer(value):
            yield self.finding(
                node,
                f"class {pointer} has no integer value",
                json_pointer=json_pointer("value", prefix=pointer),
                fix_hint="set value to the integer pixel value the class covers",
                actual=value,
            )
            return
        if value in seen:
            yield self.finding(
                node,
                f"class {pointer} repeats value {value} of class {seen[value]}",
                json_pointer=json_pointer("value", prefix=pointer),
                fix_hint="give each class its own pixel value, or merge the two classes",
                actual=value,
            )
            return
        seen[value] = pointer

    def _check_name(
        self, node: Node, pointer: str, entry: dict[str, Any], seen: dict[str, str]
    ) -> Iterator[Finding]:
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_PATTERN.match(name):
            yield self.finding(
                node,
                f"class {pointer} has no name of letters, digits, '-' and '_'",
                json_pointer=json_pointer("name", prefix=pointer),
                fix_hint="set name to a short machine-readable id such as 'permanent-water',"
                " and put the readable label in title",
                actual=name,
            )
            return
        if name in seen:
            yield self.finding(
                node,
                f"class {pointer} repeats name '{name}' of class {seen[name]}",
                json_pointer=json_pointer("name", prefix=pointer),
                fix_hint="give each class its own name",
                actual=name,
            )
            return
        seen[name] = pointer

    def _check_color_hint(
        self, node: Node, pointer: str, entry: dict[str, Any]
    ) -> Iterator[Finding]:
        if "color_hint" not in entry:
            return
        hint = entry["color_hint"]
        if isinstance(hint, str) and _COLOR_HINT_PATTERN.match(hint):
            return
        fix = "write color_hint as six hex digits with no '#', e.g. '0000FF'"
        if isinstance(hint, str) and _COLOR_HINT_PATTERN.match(hint.lstrip("#")):
            fix = f"drop the '#': '{hint.lstrip('#').upper()}'"
        yield self.finding(
            node,
            f"class {pointer} has a color_hint that is not six hex digits",
            json_pointer=json_pointer("color_hint", prefix=pointer),
            fix_hint=fix,
            actual=hint,
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
            for pointer, entry in found.entries():
                value = entry.get("value")
                if not _is_integer(value):
                    continue
                if entry.get("nodata") is not True:
                    if _same_number(found.nodata, value):
                        yield self.finding(
                            node,
                            f"class {pointer} has value {value}, its band's nodata, but is not"
                            " marked nodata",
                            json_pointer=json_pointer("nodata", prefix=pointer),
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


class ClassValueRangeRule(Rule):
    """Every class value is a value the band's integer data type can hold.

    A class outside the range describes pixels the file cannot contain, which
    usually means the value or the data type is wrong.
    """

    id = "PTL-CLS-004"
    default_severity = Severity.ERROR
    description = "every class value fits the range of its band's data_type"
    kinds = ("collection", "item")
    # A value the band cannot hold is not the integer pixel value the class
    # object requires.
    spec_ids = ("PORTO-FMT-052",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for found in class_lists(node):
            limits = (
                _INTEGER_RANGES.get(found.data_type) if isinstance(found.data_type, str) else None
            )
            if limits is None:
                continue
            low, high = limits
            for pointer, entry in found.entries():
                value = entry.get("value")
                if _is_integer(value) and not low <= value <= high:
                    yield self.finding(
                        node,
                        f"class {pointer} has value {value}, outside the {found.data_type}"
                        f" range {low}..{high}",
                        json_pointer=json_pointer("value", prefix=pointer),
                        fix_hint=f"use a value between {low} and {high}, or correct the"
                        " band's data_type",
                        expected=[low, high],
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


def _band_count(asset: dict[str, Any]) -> int:
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
    that place has the classes too, since it then only mirrors them.
    """
    multi = _band_count(asset) > 1
    if multi and found.band_index is None:
        return "the band it describes in bands"
    home = found.band_index if multi else None
    if found.band_field == ("bands" if multi else None):
        return None
    if found.band_field == "raster:bands" and _has_classes(asset, home):
        return None
    return f"bands/{home}" if multi else "the asset"


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


class ItemAssetClassesRule(Rule):
    """A collection of scene items repeats the class lists in ``item_assets``.

    A client builds a legend from the collection alone, without opening an
    item, only when the collection's ``item_assets`` template has the classes
    its items' assets declare, in the same place.
    """

    id = "PTL-CLS-006"
    default_severity = Severity.WARNING
    description = "a collection repeats its items' class lists in item_assets"
    kinds = ("collection",)
    spec_ids = ("PORTO-FMT-051",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        templates = node.data.get("item_assets")
        templates = templates if isinstance(templates, dict) else {}
        missing: dict[tuple[str, int | None], str] = {}
        for item in graph.items_of(node):
            assets = item.data.get("assets")
            if not isinstance(assets, dict):
                continue
            for key, asset in assets.items():
                if not isinstance(asset, dict):
                    continue
                for found in asset_class_lists("", asset):
                    if found.band_field == "raster:bands":
                        continue
                    template = templates.get(key)
                    if (key, found.band_index) in missing or (
                        isinstance(template, dict) and _has_classes(template, found.band_index)
                    ):
                        continue
                    missing[(key, found.band_index)] = item.path.as_posix()
        for (key, index), example in missing.items():
            where = "the asset" if index is None else f"band {index}"
            place = "" if index is None else f"bands/{index}/"
            yield self.finding(
                node,
                f"items such as {example} have classes on asset '{key}' {where},"
                f" but item_assets/{key} does not",
                json_pointer=json_pointer("item_assets", key),
                fix_hint=f"copy the class list into item_assets/{key}/{place}{CLASSES_FIELD}",
            )
