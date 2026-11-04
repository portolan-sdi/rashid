"""Classification rules (STAC classification extension, categorical rasters).

A categorical raster names what each pixel value means with
``classification:classes``. STAC 1.1 moved per-band metadata into the core
``bands`` array, and that is where a Portolan raster carries its class list,
next to the band's ``nodata`` and ``data_type`` (portolan-spec#41).

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
- ``PTL-CLS-003`` a class marked ``nodata`` has the band's ``nodata`` value.
- ``PTL-CLS-004`` every class value fits the band's ``data_type``.
- ``PTL-CLS-005`` the list sits on a band in STAC 1.1 ``bands``.
- ``PTL-CLS-006`` a collection of scene items repeats its items' class lists
  in ``item_assets``.

Whether a ``color_hint`` agrees with the colour table embedded in the COG
(``PTL-DAT-018``), and whether a palette band has classes at all
(``PTL-DAT-019``, an INFO hint), are bytes questions, so they live in the data pass, which
reads the lists through :func:`asset_class_lists`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, TypeGuard

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
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
                yield f"{self.pointer}/{index}", entry


def asset_class_lists(pointer: str, asset: dict[str, Any]) -> list[ClassList]:
    """Every class list on one asset: its own, and each band's."""
    nodata = asset.get("nodata", _ABSENT)
    data_type = asset.get("data_type", _ABSENT)
    found: list[ClassList] = []
    if CLASSES_FIELD in asset:
        found.append(
            ClassList(f"{pointer}/{CLASSES_FIELD}", asset[CLASSES_FIELD], nodata, data_type)
        )
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if not isinstance(bands, list):
            continue
        for index, band in enumerate(bands):
            if isinstance(band, dict) and CLASSES_FIELD in band:
                found.append(
                    ClassList(
                        f"{pointer}/{field}/{index}/{CLASSES_FIELD}",
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
                yield f"/{field}/{key}", asset


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
    spec_ids = ("PORTO-FMT-052", "PORTO-FMT-053")

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
            pointer = f"{found.pointer}/{index}"
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
                    json_pointer=f"{pointer}/title",
                    fix_hint="set title to the class's human-readable label, e.g. 'Permanent water'",
                )
            yield from self._check_color_hint(node, pointer, entry)
            if "nodata" in entry and not isinstance(entry["nodata"], bool):
                yield self.finding(
                    node,
                    f"class {pointer} has a nodata flag that is not a boolean",
                    json_pointer=f"{pointer}/nodata",
                    fix_hint="set nodata to true or false",
                )

    def _check_value(
        self, node: Node, pointer: str, entry: dict[str, Any], seen: dict[int, str]
    ) -> Iterator[Finding]:
        value = entry.get("value")
        if not _is_integer(value):
            yield self.finding(
                node,
                f"class {pointer} has no integer value",
                json_pointer=f"{pointer}/value",
                fix_hint="set value to the integer pixel value the class covers",
                actual=value,
            )
            return
        if value in seen:
            yield self.finding(
                node,
                f"class {pointer} repeats value {value} of class {seen[value]}",
                json_pointer=f"{pointer}/value",
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
                json_pointer=f"{pointer}/name",
                fix_hint="set name to a short machine-readable id such as 'permanent-water',"
                " and put the readable label in title",
                actual=name,
            )
            return
        if name in seen:
            yield self.finding(
                node,
                f"class {pointer} repeats name '{name}' of class {seen[name]}",
                json_pointer=f"{pointer}/name",
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
            json_pointer=f"{pointer}/color_hint",
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
    """A class marked ``nodata`` names the band's own nodata value.

    A viewer draws nodata pixels as empty and leaves them out of the legend.
    A nodata class whose value is not the band's nodata contradicts the band,
    and no viewer can honour both.
    """

    id = "PTL-CLS-003"
    default_severity = Severity.ERROR
    description = "a class marked nodata has the value of its band's nodata"
    kinds = ("collection", "item")
    spec_ids = ("PORTO-FMT-057",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for found in class_lists(node):
            for pointer, entry in found.entries():
                value = entry.get("value")
                if entry.get("nodata") is not True or not _is_integer(value):
                    continue
                if found.nodata is _ABSENT:
                    yield self.finding(
                        node,
                        f"class {pointer} is marked nodata, but its band declares no nodata value",
                        json_pointer=f"{pointer}/nodata",
                        fix_hint=f"set the band's nodata to {value}, or drop the class's"
                        " nodata flag",
                        expected=value,
                    )
                elif not _same_number(found.nodata, value):
                    yield self.finding(
                        node,
                        f"class {pointer} is marked nodata with value {value}, but its band's"
                        f" nodata is {found.nodata!r}",
                        json_pointer=f"{pointer}/value",
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
                        json_pointer=f"{pointer}/value",
                        fix_hint=f"use a value between {low} and {high}, or correct the"
                        " band's data_type",
                        expected=[low, high],
                        actual=value,
                    )


def _band_has_classes(asset: dict[str, Any], index: int) -> bool:
    bands = asset.get("bands")
    if not isinstance(bands, list) or index >= len(bands):
        return False
    band = bands[index]
    return isinstance(band, dict) and CLASSES_FIELD in band


class BandPlacementRule(Rule):
    """A class list sits on the band it describes, in STAC 1.1 ``bands``.

    The band carries the ``nodata`` and ``data_type`` the classes are read
    against, and a multi-band asset needs to say which band is classified. A
    legacy ``raster:bands`` list passes when the same band in ``bands`` carries
    the classes too, since it then only mirrors them for older readers.
    """

    id = "PTL-CLS-005"
    default_severity = Severity.ERROR
    description = "a classification:classes list sits on its band in the STAC 1.1 bands array"
    kinds = ("collection", "item")
    spec_ids = ("PORTO-FMT-050",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, container in _containers(node):
            for found in asset_class_lists(pointer, container):
                if found.band_field == "bands":
                    continue
                if found.band_index is not None:
                    if _band_has_classes(container, found.band_index):
                        continue
                    target = f"bands/{found.band_index}"
                else:
                    target = "the band it describes in bands"
                yield self.finding(
                    node,
                    f"{found.pointer} is not on a band in the STAC 1.1 bands array",
                    json_pointer=found.pointer,
                    fix_hint=f"move the list onto {target}, next to that band's nodata",
                )


class ItemAssetClassesRule(Rule):
    """A collection of scene items repeats the class lists in ``item_assets``.

    A client builds a legend from the collection alone, without opening an
    item, only when the collection's ``item_assets`` template carries the
    classes its items' bands declare.
    """

    id = "PTL-CLS-006"
    default_severity = Severity.WARNING
    description = "a collection carries its items' band class lists in item_assets"
    kinds = ("collection",)
    spec_ids = ("PORTO-FMT-051",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        templates = node.data.get("item_assets")
        templates = templates if isinstance(templates, dict) else {}
        missing: dict[tuple[str, int], str] = {}
        for item in graph.items_of(node):
            assets = item.data.get("assets")
            if not isinstance(assets, dict):
                continue
            for key, asset in assets.items():
                if not isinstance(asset, dict):
                    continue
                for found in asset_class_lists("", asset):
                    if found.band_field != "bands" or found.band_index is None:
                        continue
                    template = templates.get(key)
                    if (key, found.band_index) in missing or (
                        isinstance(template, dict) and _band_has_classes(template, found.band_index)
                    ):
                        continue
                    missing[(key, found.band_index)] = item.path.as_posix()
        for (key, index), example in missing.items():
            yield self.finding(
                node,
                f"items such as {example} carry classes on asset '{key}' band {index},"
                f" but item_assets/{key} does not",
                json_pointer=f"/item_assets/{key}",
                fix_hint=f"copy the class list into item_assets/{key}/bands/{index}"
                f"/{CLASSES_FIELD}",
            )
