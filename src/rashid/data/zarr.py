"""Incubating Zarr profile checks: does a Zarr store read like a raster?

``specs/incubating/zarr.md`` describes the store Portolan intends to accept as
raster data beside COG. Nothing in it is part of the conformance surface of the
current Portolan version, so every check here caps at WARNING; see
:func:`rashid.runner.cap_incubating`.

The checks read metadata only. A Zarr v3 store states its whole shape in
``zarr.json`` documents — ``zarr_format``, ``node_type``, ``shape``,
``chunk_grid``, ``codecs``, ``dimension_names``, and the convention attributes
under ``attributes`` — so every validator-enforced requirement of the profile
is decidable without opening one chunk. A root that carries
``consolidated_metadata`` states its descendants' metadata too, which makes the
usual store one request.

Reads go through the pass's :class:`~rashid.data.reader.AssetReader`, by asking
it for the ``zarr.json`` *inside* the store rather than for the store itself: an
asset href names a key prefix, which is a directory on disk and nothing
fetchable over HTTP. That keeps one resolution path for local trees, catalogs
read over https, and ``--data-scope local``, which declines the store as it
declines any asset outside the tree. An HTTP store cannot be listed, so the
children that consolidated metadata does not name are discovered from the
``multiscales`` ``layout`` and from the variable names the STAC asset declares
in ``cube:variables`` and ``bands``.

The conventions the profile cites identify themselves by a permanent UUID, and
a real store was found naming the proj convention's UUID beside a schema URL
from a different repository, so identity is read from the UUID first and the
name second.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from rashid import _json, _trace
from rashid._href import store_member_href
from rashid.catalog import Node
from rashid.data import DataDefect
from rashid.data.reader import AssetReader
from rashid.model import Severity
from rashid.registry import (
    ZAR_ASSET,
    ZAR_AXIS_ORDER,
    ZAR_CHUNK_SIZE,
    ZAR_CODECS,
    ZAR_CONVENTIONS,
    ZAR_CRS,
    ZAR_DISPLAY_RANGE,
    ZAR_LAYOUT_FIELDS,
    ZAR_LEVEL_STEP,
    ZAR_NODATA,
    ZAR_NONSPATIAL_CHUNK,
    ZAR_PYRAMID,
    ZAR_READ_AMPLIFICATION,
    ZAR_ROW_MAJOR,
    ZAR_SHARD_SPAN,
    ZAR_STORE_VERSION,
    ZAR_TRANSFORM,
    ZAR_VARIABLES,
)
from rashid.rules._common import media_parameters

log = logging.getLogger(__name__)

#: The Zarr version the profile accepts.
ZARR_FORMAT = 3

#: The media-type parameter that names it.
ZARR_VERSION_PARAMETER = "3"

#: Convention identity, UUID first then name, as each convention's README
#: defines it. A UUID "MUST NOT change across versions", while schema and spec
#: URLs carry the version tag and are rewritten, so the UUID is the stable key.
SPATIAL_CONVENTION = ("689b58e2-cf7b-45e0-9fff-9cfc0883d6b4", "spatial")
PROJ_CONVENTION = ("f17cb550-5864-4468-aeb7-f3180cfb622f", "proj")
MULTISCALES_CONVENTION = ("d35379db-88df-4056-af3a-620245f8e347", "multiscales")

#: incubating/zarr.md, Codecs: the codecs with a JavaScript implementation.
JS_CODECS = frozenset(
    {
        "blosc",
        "lz4",
        "zstd",
        "gzip",
        "zlib",
        "shuffle",
        "delta",
        "scale_offset",
        "transpose",
        "bytes",
        "crc32c",
        "sharding_indexed",
    }
)

#: incubating/zarr.md, Overviews: the chunk count a level may cover its extent in.
COARSEST_CHUNKS = 4

#: incubating/zarr.md, Chunking: the read amplification ceiling, and the
#: sample range a chunk should span on X and Y.
MAX_READ_FACTOR = 8
MIN_CHUNK_SAMPLES = 256
MAX_CHUNK_SAMPLES = 1024

#: Levels SHOULD step by 2; a level's shape is a ceiling division, so the
#: measured ratio of two small levels lands beside 2 rather than on it.
_STEP_TOLERANCE = 0.2

#: Metadata documents read per store, and bytes read per document. A store
#: with consolidated metadata costs one request; one without costs a request
#: per node the layout and the STAC variable names name. The caps stop a
#: pathological hierarchy from turning one asset into thousands of requests.
_MAX_DOCUMENTS = 64
_MAX_BYTES = 8 << 20
_MAX_DEPTH = 8

#: Dimension names that mark the Y and X axes when no `spatial:dimensions`
#: names them. The profile's grid rules still have a subject in that case, and
#: PTL-ZAR-002 reports the missing declaration on its own.
_Y_NAMES = frozenset({"y", "lat", "latitude", "northing"})
_X_NAMES = frozenset({"x", "lon", "long", "longitude", "easting"})

#: Dimension names that mark a band axis, for the display-range exemption.
_BAND_NAMES = frozenset({"band", "bands", "channel", "channels", "rgb", "rgba"})

#: 8-bit sample types, for the same exemption.
_EIGHT_BIT = frozenset({"uint8", "int8"})


@dataclass(frozen=True)
class _Meta:
    """One Zarr node's metadata document, with its path inside the store."""

    path: str
    data: dict[str, Any]

    @property
    def attributes(self) -> dict[str, Any]:
        raw = self.data.get("attributes")
        return raw if isinstance(raw, dict) else {}

    @property
    def is_array(self) -> bool:
        return self.data.get("node_type") == "array"

    @property
    def name(self) -> str:
        """The node's own name, without the path that leads to it."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def subject(self) -> str:
        """How a finding names this node; an asset href may name the root itself."""
        return f"array '{self.path}'" if self.path else "the root array"

    @property
    def dimensions(self) -> list[str]:
        raw = self.data.get("dimension_names")
        if not isinstance(raw, list):
            return []
        return [name if isinstance(name, str) else "" for name in raw]


@dataclass(frozen=True)
class _Fault:
    """One check's complaint about one array, before collapsing to a defect."""

    rule_id: str
    severity: Severity
    message: str


class _Store:
    """The metadata documents of one store, read once each and cached.

    ``unreadable`` holds why the root could not be read and ``root_status``
    the HTTP status behind it, when the host gave one. The two differ in what
    they settle: a 404 for the root ``zarr.json`` is the host's answer that the
    store holds none, which is what a Zarr v2 store looks like, while a
    transport failure settles nothing at all.
    """

    def __init__(self, node: Node, href: str, reader: AssetReader) -> None:
        self._node = node
        self._href = href
        self._reader = reader
        self._cache: dict[str, _Meta | None] = {}
        self._budget = _MAX_DOCUMENTS
        self.unreadable: str | None = None
        self.root_status: int | None = None

    def read(self, path: str) -> _Meta | None:
        """The node at ``path``, from consolidated metadata or its own document."""
        if path in self._cache:
            return self._cache[path]
        meta = self._fetch(path)
        self._cache[path] = meta
        if meta is not None:
            self._absorb(meta, 0)
        return meta

    def _fetch(self, path: str) -> _Meta | None:
        if self._budget <= 0:
            log.debug("zarr: %s: metadata budget spent before %r", self._href, path)
            return None
        self._budget -= 1
        member = store_member_href(self._href, f"{path}/zarr.json" if path else "zarr.json")
        stream = self._reader.stream(self._node, member)
        if stream is None:
            self._note(path, "the store is not reachable from this catalog")
            return None
        _trace.count("zarr.metadata_reads")
        try:
            raw = _read_capped(stream)
            data = _json.loads(raw)
        except (OSError, ValueError) as exc:
            # rashid._store raises an OSError that carries the HTTP status it
            # read; a local read raises one that does not.
            self._note(path, str(exc), getattr(exc, "status", None))
            return None
        if not isinstance(data, dict):
            self._note(path, "zarr.json is not a JSON object")
            return None
        return _Meta(path, data)

    def _note(self, path: str, reason: str, status: int | None = None) -> None:
        if path or self.unreadable is not None:
            return
        self.unreadable = reason
        self.root_status = status

    def _absorb(self, meta: _Meta, depth: int) -> None:
        """Cache every descendant that ``meta``'s consolidated metadata names."""
        consolidated = meta.data.get("consolidated_metadata")
        members = consolidated.get("metadata") if isinstance(consolidated, dict) else None
        if not isinstance(members, dict) or depth >= _MAX_DEPTH:
            return
        for name, child in members.items():
            if not isinstance(name, str) or not isinstance(child, dict):
                continue
            path = f"{meta.path}/{name}" if meta.path else name
            if path in self._cache:
                continue
            self._cache[path] = child_meta = _Meta(path, child)
            self._absorb(child_meta, depth + 1)

    def known(self) -> list[_Meta]:
        """Every node read so far, in path order."""
        return [meta for _path, meta in sorted(self._cache.items()) if meta is not None]


def _read_capped(stream: Iterator[bytes]) -> bytes:
    """The document's bytes, refusing one larger than ``_MAX_BYTES``."""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in stream:
            total += len(chunk)
            if total > _MAX_BYTES:
                raise OSError(f"zarr.json exceeds {_MAX_BYTES} bytes")
            chunks.append(chunk)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return b"".join(chunks)


# --- store shape -----------------------------------------------------------


@dataclass(frozen=True)
class _Level:
    """One resolution level: its layout entry, its group path, and its arrays."""

    entry: dict[str, Any]
    path: str
    arrays: tuple[_Meta, ...]

    @property
    def extent(self) -> tuple[int, int] | None:
        shape = _int_pair(self.entry.get("spatial:shape"))
        if shape is not None:
            return shape
        return None


def _layout(root: _Meta) -> list[dict[str, Any]]:
    """The multiscales layout entries declared on the store root.

    Only the multiscales convention's own spelling counts: a ``multiscales``
    object holding a ``layout`` array of entries that name an ``asset``. Stores
    in the wild also carry an older ``multiscales`` list of ``datasets`` with a
    ``path`` and a ``factor``, which is a different model; the profile cites
    the convention, so such a store declares no layout and PTL-ZAR-002 reports
    the undeclared convention.
    """
    multiscales = root.attributes.get("multiscales")
    raw = multiscales.get("layout") if isinstance(multiscales, dict) else None
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("asset")]


def _variable_names(asset: dict[str, Any], declarations: list[dict[str, Any]]) -> list[str]:
    """Variable names the STAC object declares, for discovering arrays by name.

    ``cube:variables`` keys name them directly. A ``bands`` entry names one
    too, and the STAC Zarr Best Practices spell a band inside a variable as
    ``reflectance[band=blue]``, so the variable is the part before the bracket.
    """
    names: list[str] = []
    for holder in declarations:
        variables = holder.get("cube:variables")
        if isinstance(variables, dict):
            names.extend(name for name in variables if isinstance(name, str))
    for band in _bands(asset):
        name = band.get("name")
        if isinstance(name, str) and name:
            names.append(name.split("[", 1)[0])
    return list(dict.fromkeys(name for name in names if name and "/" not in name))


def _discover(store: _Store, root: _Meta, names: list[str]) -> list[_Level]:
    """The store's levels, finest first, with the arrays under each.

    Group paths come from the layout; a store with no pyramid has one level at
    the root. Arrays come from consolidated metadata where the root has it, and
    otherwise from one read per declared variable name under each level.
    """
    entries = _layout(root)
    paths = [str(entry.get("asset", "")).strip("/") for entry in entries] or [""]
    for path in paths:
        if store.read(path) is None:
            continue
        for name in names:
            store.read(f"{path}/{name}" if path else name)
    known = store.known()
    levels = [
        _Level(entry, path, tuple(_arrays_under(known, path)))
        for entry, path in zip(entries or [{}], paths, strict=True)
    ]
    return sorted(levels, key=_level_rank, reverse=True)


def _arrays_under(known: list[_Meta], path: str) -> Iterator[_Meta]:
    prefix = f"{path}/" if path else ""
    for meta in known:
        if not meta.is_array or not meta.path.startswith(prefix):
            continue
        if path and "/" in meta.path[len(prefix) :]:
            continue  # a deeper level's array, not this one's
        yield meta


def _level_rank(level: _Level) -> int:
    """How fine a level is, in samples, for ordering finest to coarsest.

    A layout entry states its own ``spatial:shape``; a level that omits it,
    and a store with no pyramid, are measured from the arrays themselves.
    """
    extent = level.extent
    if extent is not None:
        return extent[0] * extent[1]
    shapes = [_spatial_shape(array) for array in level.arrays]
    return max((shape[0] * shape[1] for shape in shapes if shape is not None), default=0)


# --- metadata accessors ----------------------------------------------------


def _numbers(value: object, count: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int | float) for item in value):
        return None
    return [float(item) for item in value]


def _int_pair(value: object) -> tuple[int, int] | None:
    pair = _numbers(value, 2)
    if pair is None or any(item <= 0 or item != int(item) for item in pair):
        return None
    return (int(pair[0]), int(pair[1]))


def _ancestors(path: str) -> Iterator[str]:
    """Every ancestor group path of ``path``, nearest first, ending at the root."""
    remaining = path
    while "/" in remaining:
        remaining = remaining.rpartition("/")[0]
        yield remaining
    yield ""


def _inherited(store: _Store, array: _Meta, key: str, entry: dict[str, Any]) -> Any:
    """A ``spatial:`` value for ``array``: its own, its level's, then its ancestors'.

    The spatial convention makes a group-level value a default for its child
    arrays, and its own multiscales example puts ``spatial:dimensions`` on the
    pyramid root while the arrays sit two levels below, so the search walks the
    whole ancestor chain rather than stopping at the direct parent.
    """
    if key in array.attributes:
        return array.attributes[key]
    if key in entry:
        return entry[key]
    for ancestor in _ancestors(array.path):
        group = store.read(ancestor)
        if group is not None and key in group.attributes:
            return group.attributes[key]
    return None


def _proj_value(store: _Store, array: _Meta, key: str) -> Any:
    """A ``proj:`` value for ``array``: its own, else its direct parent group's.

    The proj convention inherits from a group to its direct child arrays only.
    """
    if key in array.attributes:
        return array.attributes[key]
    parent = next(_ancestors(array.path))
    group = store.read(parent)
    return group.attributes.get(key) if group is not None else None


def _declared_axes(store: _Store, array: _Meta, entry: dict[str, Any]) -> tuple[str, str] | None:
    """The Y and X dimension names ``spatial:dimensions`` gives, when it matches."""
    raw = _inherited(store, array, "spatial:dimensions", entry)
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    if any(not isinstance(name, str) for name in raw):
        return None
    dimensions = array.dimensions
    if raw[0] not in dimensions or raw[1] not in dimensions:
        return None
    return (raw[0], raw[1])


def _guessed_axes(array: _Meta) -> tuple[str, str] | None:
    """The Y and X dimension names read off well-known spellings."""
    dimensions = array.dimensions
    y = next((name for name in dimensions if name.lower() in _Y_NAMES), None)
    x = next((name for name in dimensions if name.lower() in _X_NAMES), None)
    return (y, x) if y is not None and x is not None else None


def _axes(store: _Store, array: _Meta, entry: dict[str, Any]) -> tuple[str, str] | None:
    return _declared_axes(store, array, entry) or _guessed_axes(array)


def _renderable(store: _Store, level: _Level) -> list[_Meta]:
    """The level's arrays that carry both spatial dimensions."""
    return [array for array in level.arrays if _axes(store, array, level.entry) is not None]


def _shape(array: _Meta) -> list[int]:
    raw = array.data.get("shape")
    if not isinstance(raw, list):
        return []
    return [int(item) for item in raw if isinstance(item, int) and not isinstance(item, bool)]


def _shard_shape(array: _Meta) -> list[int]:
    """The chunk grid's chunk shape: the shard, when the array is sharded."""
    grid = array.data.get("chunk_grid")
    configuration = grid.get("configuration") if isinstance(grid, dict) else None
    raw = configuration.get("chunk_shape") if isinstance(configuration, dict) else None
    if not isinstance(raw, list):
        return []
    return [int(item) for item in raw if isinstance(item, int) and not isinstance(item, bool)]


def _codecs(array: _Meta) -> list[dict[str, Any]]:
    raw = array.data.get("codecs")
    if not isinstance(raw, list):
        return []
    return [codec for codec in raw if isinstance(codec, dict)]


def _sharding(array: _Meta) -> dict[str, Any] | None:
    """The ``sharding_indexed`` codec's configuration, when the array is sharded."""
    for codec in _codecs(array):
        if codec.get("name") == "sharding_indexed":
            configuration = codec.get("configuration")
            return configuration if isinstance(configuration, dict) else {}
    return None


def _chunk_shape(array: _Meta) -> list[int]:
    """The inner chunk shape: the profile's "chunk" for a sharded array."""
    configuration = _sharding(array)
    raw = configuration.get("chunk_shape") if configuration else None
    if isinstance(raw, list):
        inner = [int(i) for i in raw if isinstance(i, int) and not isinstance(i, bool)]
        if inner:
            return inner
    return _shard_shape(array)


def _spatial_shape(array: _Meta) -> tuple[int, int] | None:
    """The array's extent on its two spatial axes, Y then X."""
    axes = _guessed_axes(array)
    shape = _shape(array)
    if axes is None or len(shape) != len(array.dimensions):
        return None
    dimensions = array.dimensions
    return (shape[dimensions.index(axes[0])], shape[dimensions.index(axes[1])])


def _codec_names(array: _Meta) -> list[str]:
    """Every codec name in the chain, the sharded inner chain included."""
    names: list[str] = []
    for codec in _codecs(array):
        name = codec.get("name")
        if isinstance(name, str):
            names.append(name)
        configuration = codec.get("configuration")
        inner = configuration.get("codecs") if isinstance(configuration, dict) else None
        for nested in inner if isinstance(inner, list) else []:
            nested_name = nested.get("name") if isinstance(nested, dict) else None
            if isinstance(nested_name, str):
                names.append(nested_name)
    return names


def _transposes(array: _Meta) -> list[list[int]]:
    """Every ``transpose`` codec order in the chain."""
    orders: list[list[int]] = []
    for codec in _codecs(array):
        configuration = codec.get("configuration")
        nested = configuration.get("codecs") if isinstance(configuration, dict) else None
        chain = (
            [codec, *(c for c in nested if isinstance(c, dict))]
            if isinstance(nested, list)
            else [codec]
        )
        for member in chain:
            if member.get("name") != "transpose":
                continue
            member_config = member.get("configuration")
            order = member_config.get("order") if isinstance(member_config, dict) else None
            if isinstance(order, list):
                orders.append([int(i) for i in order if isinstance(i, int)])
    return orders


# --- STAC accessors --------------------------------------------------------


def _bands(asset: dict[str, Any]) -> list[dict[str, Any]]:
    raw = asset.get("bands")
    if not isinstance(raw, list):
        return []
    return [band for band in raw if isinstance(band, dict)]


def _band_for(asset: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The ``bands`` entry whose ``name`` equals the variable name."""
    return next((band for band in _bands(asset) if band.get("name") == name), None)


def _declarations(node: Node, asset: dict[str, Any]) -> list[dict[str, Any]]:
    """Where a STAC field may be declared: the asset, the object, its properties."""
    holders = [asset, node.data]
    properties = node.data.get("properties")
    if isinstance(properties, dict):
        holders.append(properties)
    return holders


def _has_rescale(declarations: list[dict[str, Any]]) -> bool:
    """Whether any ``renders`` entry carries a ``rescale``.

    The profile's open questions record that the render extension cannot tie
    an entry to one variable of a cube, so any entry's rescale counts.
    """
    for holder in declarations:
        renders = holder.get("renders")
        for entry in renders.values() if isinstance(renders, dict) else []:
            if isinstance(entry, dict) and entry.get("rescale") is not None:
                return True
    return False


# --- defect construction ---------------------------------------------------


def _defect(
    rule_id: str,
    severity: Severity,
    key: str,
    message: str,
    field: str | None = None,
) -> DataDefect:
    """One Zarr defect, capped at WARNING.

    Every Zarr finding passes through here, so no check in this module can
    emit an ERROR for a requirement the spec marks incubating. The registry
    refuses an ERROR declaration and :func:`rashid.runner.cap_incubating` caps
    the report, so a bypass of this function is caught twice over.
    """
    capped = Severity.WARNING if severity is Severity.ERROR else severity
    return DataDefect(rule_id, capped, f"asset '{key}': {message}", key, field)


def _collapse(key: str, faults: list[_Fault]) -> list[DataDefect]:
    """One defect per rule, naming the first array and how many more failed.

    A 12-level pyramid holds one array per level, so an unchecked loop would
    report the same grid defect a dozen times for one store.
    """
    groups: dict[str, list[_Fault]] = {}
    for fault in faults:
        groups.setdefault(fault.rule_id, []).append(fault)
    defects = []
    for rule_id, members in groups.items():
        first = members[0]
        message = first.message
        if len(members) > 1:
            message = f"{message}; {len(members) - 1} further array(s) fail this check"
        defects.append(_defect(rule_id, first.severity, key, message))
    return defects


# --- store-level checks ----------------------------------------------------


def _check_media_type(key: str, asset: dict[str, Any]) -> list[DataDefect]:
    """PTL-ZAR-004, from the STAC asset alone, so an unreadable store still gets it."""
    version = media_parameters(str(asset.get("type", ""))).get("version")
    if version == ZARR_VERSION_PARAMETER:
        return []
    return [
        _defect(
            ZAR_ASSET,
            Severity.ERROR,
            key,
            f"media type declares version {version!r}; the profile requires"
            f" 'application/vnd.zarr; version={ZARR_VERSION_PARAMETER}'",
            "type",
        )
    ]


def _check_root(key: str, root: _Meta) -> list[DataDefect]:
    """PTL-ZAR-001 and the group half of PTL-ZAR-004, read off the store root."""
    defects: list[DataDefect] = []
    declared = root.data.get("zarr_format")
    if declared != ZARR_FORMAT:
        defects.append(
            _defect(
                ZAR_STORE_VERSION,
                Severity.ERROR,
                key,
                f"store root declares zarr_format {declared!r}, not {ZARR_FORMAT}",
            )
        )
    if root.is_array:
        defects.append(
            _defect(
                ZAR_ASSET,
                Severity.ERROR,
                key,
                "href points at an array; a Zarr asset href must name a group",
                "href",
            )
        )
    return defects


def _check_missing_root(key: str, store: _Store) -> DataDefect:
    """PTL-ZAR-001 when the store root holds no readable ``zarr.json``.

    A 404 is the host's answer that no ``zarr.json`` is there, which is how a
    Zarr v2 store reads: it declares ``.zgroup`` and ``.zmetadata`` instead. A
    store that answers that way is not Zarr v3, so the requirement is broken
    rather than unchecked. Any other failure leaves the question open and is
    reported as information.
    """
    if store.root_status == 404:
        return _defect(
            ZAR_STORE_VERSION,
            Severity.ERROR,
            key,
            "the store root serves no zarr.json, so it is not a Zarr v3 store"
            " (Zarr v2 declares .zgroup and .zmetadata instead)",
            "href",
        )
    return _defect(
        ZAR_STORE_VERSION,
        Severity.INFO,
        key,
        f"Zarr store metadata could not be read ({store.unreadable});"
        " the profile's checks did not run",
    )


def _declares(root: _Meta, convention: tuple[str, str]) -> bool:
    """Whether ``zarr_conventions`` names a convention, by UUID then by name."""
    raw = root.attributes.get("zarr_conventions")
    uuid, name = convention
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("uuid", "")).lower() == uuid or entry.get("name") == name:
            return True
    return False


def _malformed_convention_fields(root: _Meta) -> list[str]:
    """The convention fields the store root declares but spells wrongly."""
    attributes = root.attributes
    problems: list[str] = []
    if "spatial:dimensions" in attributes:
        raw = attributes["spatial:dimensions"]
        if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(n, str) for n in raw):
            problems.append("spatial:dimensions is not two dimension names")
    if "spatial:transform" in attributes and _numbers(attributes["spatial:transform"], 6) is None:
        problems.append("spatial:transform is not six numbers")
    if "spatial:bbox" in attributes and _numbers(attributes["spatial:bbox"], 4) is None:
        problems.append("spatial:bbox is not four numbers")
    code = attributes.get("proj:code")
    if code is not None and (not isinstance(code, str) or len(code.split(":")) != 2):
        problems.append("proj:code is not an 'authority:code' string")
    if _declares(root, MULTISCALES_CONVENTION) and not _layout(root):
        problems.append("multiscales declares no layout array")
    return problems


def _check_conventions(key: str, root: _Meta, *, pyramid: bool) -> list[DataDefect]:
    """PTL-ZAR-002: the required conventions are declared and well formed."""
    required = [SPATIAL_CONVENTION, PROJ_CONVENTION]
    if pyramid:
        required.append(MULTISCALES_CONVENTION)
    missing = [name for _uuid, name in required if not _declares(root, (_uuid, name))]
    defects: list[DataDefect] = []
    if missing:
        defects.append(
            _defect(
                ZAR_CONVENTIONS,
                Severity.ERROR,
                key,
                "store root zarr_conventions does not declare " + ", ".join(missing),
            )
        )
    problems = _malformed_convention_fields(root)
    if problems:
        defects.append(
            _defect(
                ZAR_CONVENTIONS,
                Severity.ERROR,
                key,
                "store root does not conform to its conventions: " + "; ".join(problems),
            )
        )
    return defects


def _check_crs(key: str, store: _Store, levels: list[_Level]) -> list[DataDefect]:
    """PTL-ZAR-008: the store carries ``proj:code``, or ``proj:projjson`` instead.

    The store carries a field when its root does, or when every renderable
    array resolves one through the proj convention's group-to-array
    inheritance. Falling back to PROJJSON is what PORTO-ZARR-010 permits, so
    that case is reported as information rather than as a missed SHOULD.
    """
    root = store.read("")
    arrays = [array for level in levels for array in _renderable(store, level)]
    for field in ("proj:code", "proj:projjson"):
        at_root = root is not None and root.attributes.get(field) is not None
        on_arrays = bool(arrays) and all(_proj_value(store, a, field) is not None for a in arrays)
        if at_root or on_arrays:
            if field == "proj:code":
                return []
            return [
                _defect(
                    ZAR_CRS,
                    Severity.INFO,
                    key,
                    "store carries proj:projjson but no proj:code",
                )
            ]
    return [
        _defect(
            ZAR_CRS,
            Severity.WARNING,
            key,
            "store attributes carry neither proj:code nor proj:projjson",
        )
    ]


def _chunks_to_cover(store: _Store, array: _Meta, entry: dict[str, Any]) -> int | None:
    """How many chunks cover the array's extent, over its two spatial axes."""
    axes = _axes(store, array, entry)
    dimensions = array.dimensions
    shape = _shape(array)
    chunk = _chunk_shape(array)
    if axes is None or len(shape) != len(dimensions) or len(chunk) != len(dimensions):
        return None
    total = 1
    for name in axes:
        index = dimensions.index(name)
        if chunk[index] <= 0:
            return None
        total *= math.ceil(shape[index] / chunk[index])
    return total


def _level_chunks(store: _Store, level: _Level) -> int | None:
    """The largest chunk count any renderable array of the level needs."""
    counts = [_chunks_to_cover(store, a, level.entry) for a in _renderable(store, level)]
    sized = [count for count in counts if count is not None]
    return max(sized) if sized else None


def _check_pyramid(key: str, store: _Store, levels: list[_Level]) -> list[DataDefect]:
    """PTL-ZAR-009: a wide store carries a pyramid, and its coarsest level is small."""
    defects: list[DataDefect] = []
    finest = _level_chunks(store, levels[0]) if levels else None
    if len(levels) < 2 and finest is not None and finest > COARSEST_CHUNKS:
        defects.append(
            _defect(
                ZAR_PYRAMID,
                Severity.ERROR,
                key,
                f"the finest level needs {finest} chunks to cover its extent,"
                f" more than {COARSEST_CHUNKS}, and the store declares no multiscale pyramid",
            )
        )
        return defects
    coarsest = _level_chunks(store, levels[-1]) if levels else None
    if coarsest is not None and coarsest > COARSEST_CHUNKS:
        defects.append(
            _defect(
                ZAR_PYRAMID,
                Severity.ERROR,
                key,
                f"the coarsest level needs {coarsest} chunks to cover its extent,"
                f" more than {COARSEST_CHUNKS}",
            )
        )
    return defects


def _check_layout(key: str, levels: list[_Level]) -> list[DataDefect]:
    """PTL-ZAR-010 and PTL-ZAR-011: layout fields, and the step between levels."""
    if len(levels) < 2:
        return []
    defects: list[DataDefect] = []
    incomplete = [
        str(level.entry.get("asset", "?"))
        for level in levels
        if _numbers(level.entry.get("spatial:transform"), 6) is None
        or _int_pair(level.entry.get("spatial:shape")) is None
    ]
    if incomplete:
        defects.append(
            _defect(
                ZAR_LAYOUT_FIELDS,
                Severity.ERROR,
                key,
                "multiscales layout entries without both spatial:transform and"
                " spatial:shape: " + ", ".join(incomplete),
            )
        )
    defects.extend(_check_steps(key, levels))
    return defects


def _check_steps(key: str, levels: list[_Level]) -> list[DataDefect]:
    """PTL-ZAR-011: consecutive levels differ by a factor of 2 on each spatial axis."""
    offenders: list[str] = []
    for finer, coarser in zip(levels, levels[1:], strict=False):
        fine, coarse = finer.extent, coarser.extent
        if fine is None or coarse is None:
            continue
        ratios = [fine[axis] / coarse[axis] for axis in (0, 1) if coarse[axis]]
        if any(abs(ratio - 2.0) > _STEP_TOLERANCE for ratio in ratios):
            offenders.append(f"{finer.entry.get('asset', '?')}->{coarser.entry.get('asset', '?')}")
    if not offenders:
        return []
    return [
        _defect(
            ZAR_LEVEL_STEP,
            Severity.WARNING,
            key,
            "pyramid levels do not step by a factor of 2: " + ", ".join(offenders),
        )
    ]


def _check_variables(
    key: str, store: _Store, levels: list[_Level], declarations: list[dict[str, Any]]
) -> list[DataDefect]:
    """PTL-ZAR-012: more than one data variable requires ``cube:variables``."""
    if not levels:
        return []
    variables = sorted({array.name for array in _renderable(store, levels[0])})
    if len(variables) < 2:
        return []
    if any(isinstance(holder.get("cube:variables"), dict) for holder in declarations):
        return []
    return [
        _defect(
            ZAR_VARIABLES,
            Severity.ERROR,
            key,
            f"the store holds {len(variables)} data variables ({', '.join(variables)})"
            " but no cube:variables is declared",
        )
    ]


# --- per-array checks ------------------------------------------------------


def _grid_faults(store: _Store, array: _Meta, entry: dict[str, Any]) -> Iterator[_Fault]:
    """PTL-ZAR-005, PTL-ZAR-006, PTL-ZAR-007: transform, axis order, and C order."""
    if _numbers(_inherited(store, array, "spatial:transform", entry), 6) is None:
        yield _Fault(
            ZAR_TRANSFORM,
            Severity.ERROR,
            f"{array.subject} resolves no affine spatial:transform of six numbers",
        )
    transform_type = _inherited(store, array, "spatial:transform_type", entry)
    if transform_type is not None and transform_type != "affine":
        yield _Fault(
            ZAR_TRANSFORM,
            Severity.ERROR,
            f"{array.subject} declares spatial:transform_type {transform_type!r},"
            " and the profile requires an affine transform",
        )
    axes = _axes(store, array, entry)
    dimensions = array.dimensions
    if axes is not None and dimensions[-2:] != [axes[0], axes[1]]:
        yield _Fault(
            ZAR_AXIS_ORDER,
            Severity.ERROR,
            f"{array.subject} orders its dimensions {dimensions}; the spatial axes"
            f" must be the last two, Y ('{axes[0]}') before X ('{axes[1]}')",
        )
    for order in _transposes(array):
        if order != list(range(len(order))):
            yield _Fault(
                ZAR_ROW_MAJOR,
                Severity.ERROR,
                f"{array.subject} carries a transpose codec with order {order},"
                " so it is not stored row-major",
            )


def _chunk_faults(store: _Store, array: _Meta, entry: dict[str, Any]) -> Iterator[_Fault]:
    """PTL-ZAR-015 to PTL-ZAR-018: read amplification, chunk size, and shard span."""
    axes = _axes(store, array, entry)
    dimensions = array.dimensions
    chunk = _chunk_shape(array)
    if axes is None or len(chunk) != len(dimensions):
        return
    spatial = {dimensions.index(axes[0]), dimensions.index(axes[1])}
    extra = [(dimensions[i], chunk[i]) for i in range(len(chunk)) if i not in spatial]
    factor = math.prod(length for _name, length in extra)
    if factor > MAX_READ_FACTOR:
        yield _Fault(
            ZAR_READ_AMPLIFICATION,
            Severity.ERROR,
            f"{array.subject} chunks {factor} samples along its non-spatial"
            f" dimensions {[name for name, _ in extra]}, so a client reads {factor} samples"
            f" for every sample it draws, more than {MAX_READ_FACTOR}",
        )
    wide = [name for name, length in extra if length != 1]
    if wide:
        yield _Fault(
            ZAR_NONSPATIAL_CHUNK,
            Severity.WARNING,
            f"{array.subject} chunks its non-spatial dimensions {wide} by more than 1",
        )
    yield from _chunk_size_faults(array, chunk, axes, dimensions)
    shard = _shard_shape(array)
    if _sharding(array) is not None and len(shard) == len(dimensions):
        span = math.prod(shard[i] for i in range(len(shard)) if i not in spatial)
        if span > MAX_READ_FACTOR:
            yield _Fault(
                ZAR_SHARD_SPAN,
                Severity.ERROR,
                f"{array.subject} shards {span} samples across its non-spatial"
                f" dimensions, more than {MAX_READ_FACTOR}",
            )


def _chunk_size_faults(
    array: _Meta, chunk: list[int], axes: tuple[str, str], dimensions: list[str]
) -> Iterator[_Fault]:
    """PTL-ZAR-017: the inner chunk spans 256 to 1024 samples on X and Y.

    PORTO-ZARR-024 permits a shard to be larger, which is why only the inner
    chunk shape reaches this check.

    An axis shorter than the floor is exempt from it. The coarsest level of a
    pyramid MUST fit in 4 chunks (PORTO-ZARR-012), so a level a few hundred
    samples wide cannot also chunk at 256 or more; reading the floor literally
    would make the two requirements contradict each other on every pyramid
    deep enough to satisfy the second.
    """
    shape = _shape(array)
    extents = {
        name: shape[dimensions.index(name)] if len(shape) == len(dimensions) else 0 for name in axes
    }
    sizes = {name: chunk[dimensions.index(name)] for name in axes}
    outside = {
        name: size
        for name, size in sizes.items()
        if (size < MIN_CHUNK_SAMPLES and extents[name] > MIN_CHUNK_SAMPLES)
        or size > MAX_CHUNK_SAMPLES
    }
    if outside:
        yield _Fault(
            ZAR_CHUNK_SIZE,
            Severity.WARNING,
            f"{array.subject} chunks {outside} on its spatial axes;"
            f" chunks should be {MIN_CHUNK_SAMPLES} to {MAX_CHUNK_SAMPLES} samples on X and Y",
        )


def _codec_faults(array: _Meta) -> Iterator[_Fault]:
    """PTL-ZAR-019: every codec in the chain has a JavaScript implementation."""
    unsupported = sorted({name for name in _codec_names(array) if name not in JS_CODECS})
    if unsupported:
        yield _Fault(
            ZAR_CODECS,
            Severity.WARNING,
            f"{array.subject} uses codec(s) {', '.join(unsupported)}, which have no"
            " known JavaScript implementation",
        )


def _band_axis_length(array: _Meta) -> int:
    """The length of the array's band axis, or 0 when it has none."""
    shape = _shape(array)
    dimensions = array.dimensions
    if len(shape) != len(dimensions):
        return 0
    lengths = [shape[i] for i, name in enumerate(dimensions) if name.lower() in _BAND_NAMES]
    return max(lengths, default=0)


def _display_exempt(asset: dict[str, Any], arrays: list[_Meta]) -> bool:
    """Whether the store is 8-bit with three or more bands.

    The profile exempts such a store because its samples are display values
    already. "Three or more bands" is read from the STAC ``bands`` array first,
    and from a band axis of three or more on a renderable array otherwise,
    since an RGB or RGBA store commonly declares one variable over a band
    dimension rather than one band entry per channel.
    """
    if not any(str(array.data.get("data_type")) in _EIGHT_BIT for array in arrays):
        return False
    return len(_bands(asset)) >= 3 or any(_band_axis_length(a) >= 3 for a in arrays)


def _band_faults(
    array: _Meta, asset: dict[str, Any], *, rescaled: bool, exempt: bool
) -> Iterator[_Fault]:
    """PTL-ZAR-013 and PTL-ZAR-014: a display range, and a declared no-data value."""
    band = _band_for(asset, array.name)
    statistics = band.get("statistics") if band else None
    ranged = (
        isinstance(statistics, dict)
        and statistics.get("minimum") is not None
        and statistics.get("maximum") is not None
    )
    if not exempt and not ranged and not rescaled:
        yield _Fault(
            ZAR_DISPLAY_RANGE,
            Severity.ERROR,
            f"{array.subject} has no display range: its bands entry carries no"
            " statistics with minimum and maximum, and no renders entry carries a rescale",
        )
    has_alpha = any(b.get("name") == "alpha" for b in _bands(asset))
    if not has_alpha and (band is None or band.get("nodata") is None):
        yield _Fault(
            ZAR_NODATA,
            Severity.WARNING,
            f"{array.subject} declares no nodata on its bands entry,"
            " and the asset declares no 'alpha' band instead",
        )


def _array_faults(
    store: _Store,
    levels: list[_Level],
    asset: dict[str, Any],
    declarations: list[dict[str, Any]],
) -> list[_Fault]:
    """Every per-array complaint, over the renderable arrays of every level."""
    rescaled = _has_rescale(declarations)
    finest = _renderable(store, levels[0]) if levels else []
    exempt = _display_exempt(asset, finest)
    faults: list[_Fault] = []
    for level in levels:
        for array in _renderable(store, level):
            faults.extend(_grid_faults(store, array, level.entry))
            faults.extend(_chunk_faults(store, array, level.entry))
            faults.extend(_codec_faults(array))
    for array in finest:
        faults.extend(_band_faults(array, asset, rescaled=rescaled, exempt=exempt))
    return faults


# --- entry point -----------------------------------------------------------


def check_zarr(
    node: Node, *, key: str, asset: dict[str, Any], reader: AssetReader
) -> list[DataDefect]:
    """Check one Zarr store asset against the incubating Zarr profile.

    Returns ``PTL-ZAR-0xx`` defects, every one at WARNING or below. The media
    type is judged from the STAC asset, so it is reported even when the store
    itself cannot be opened; everything else needs the store's metadata.
    """
    href = asset.get("href")
    if not isinstance(href, str) or not href:
        return []  # PTL-AST-001 reports a missing href
    store = _Store(node, href, reader)
    with _trace.span("data.zarr"):
        defects = _check_media_type(key, asset)
        root = store.read("")
        if root is None:
            return [*defects, _check_missing_root(key, store)]
        declarations = _declarations(node, asset)
        levels = _discover(store, root, _variable_names(asset, declarations))
        defects.extend(_check_root(key, root))
        defects.extend(_check_pyramid(key, store, levels))
        pyramid = len(levels) > 1 or any(d.rule_id == ZAR_PYRAMID for d in defects)
        defects.extend(_check_conventions(key, root, pyramid=pyramid))
        defects.extend(_check_crs(key, store, levels))
        defects.extend(_check_layout(key, levels))
        defects.extend(_check_variables(key, store, levels, declarations))
        defects.extend(_collapse(key, _array_faults(store, levels, asset, declarations)))
    log.debug("zarr: %s asset %r: %d defect(s)", node.path, key, len(defects))
    return defects
