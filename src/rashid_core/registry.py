"""Check identity and pass membership.

Metadata rules declare their identity on the Rule type; this module registers
those implementations once. Other passes declare their IDs, citations, strongest
severity, and skip-gate policy here. Runners and reporting read this owner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from rashid_core import ids
from rashid_core.model import Severity
from rashid_core.rule import Rule
from rashid_core.rules.assets import (
    AssetFieldsRule,
    AssetFileFieldsRule,
    AssetHrefSchemeRule,
    AssetTitleDescriptionRule,
    CatalogAssetsRule,
    ChecksumMultihashRule,
    CogMediaTypeRule,
    CollectionDataRule,
    DataAssetSingleFileRule,
    ItemDataRule,
    PMTilesDataRule,
)
from rashid_core.rules.bbox import BboxValidRule
from rashid_core.rules.catalogs import SubcatalogFanoutRule
from rashid_core.rules.classification import (
    BandPlacementRule,
    ClassificationExtensionRule,
    ClassListRule,
    ItemAssetClassesRule,
    NodataClassRule,
)
from rashid_core.rules.collections import (
    CollectionIdRule,
    MissingItemTreeRule,
    NestedCollectionRule,
    RasterSceneItemRule,
    SingleFileCollectionRule,
)
from rashid_core.rules.conformance import (
    AppliedProfileVersionRule,
    ExtensionVersionRule,
    SchemaUriConsistencyRule,
    SchemaUriDeclaredRule,
    VersionExtensionRule,
)
from rashid_core.rules.files import (
    AgentsLinkRule,
    ReadmeContentRule,
    ReadmeLinkRule,
    ReadmeSectionsRule,
    RequiredFilesRule,
)
from rashid_core.rules.item_mirror import ItemMirrorPresentRule, ItemMirrorRegistrationRule
from rashid_core.rules.license import (
    LicenseDeclaredRule,
    NoProprietaryLicenseRule,
    OtherLicenseLinkRule,
)
from rashid_core.rules.links import (
    ChildLinkCompletenessRule,
    ContainmentStaysInLanguageRule,
    IconMediaTypeRule,
    IconRelativeHrefRule,
    IconTitleRule,
    LinkResolutionRule,
    RequiredLinksRule,
    StructuralLinkTypeRule,
)
from rashid_core.rules.partitions import PartitionFieldsRule, PartitionGlobSchemeRule
from rashid_core.rules.provenance import (
    MirrorCanonicalLinkRule,
    MirrorUpdatedRule,
    MirrorViaLinkRule,
    OfficialNoUpstreamLinksRule,
)
from rashid_core.rules.providers import HostContactRule, ProducerPresentRule, SingleHostRule
from rashid_core.rules.temporal import DatetimePresentRule, DatetimeValidRule
from rashid_core.rules.titles import HumanReadableTitleRule, LinkTitleRule, TitleDescriptionRule
from rashid_core.rules.versions import VersionChainRule
from rashid_core.rules.viz import (
    DefaultStyleRoleRule,
    LargeVectorWithoutVisualRule,
    MosaicOverviewRule,
    OverviewDatetimeRule,
    PMTilesRegistrationRule,
    StyleMediaTypeRule,
    StylesForDerivativeRule,
    ThumbnailRule,
)

__all__ = ["CHECKS", "CheckInfo", "RegistryError", "describe"]

PassName = Literal["metadata", "runner", "structural", "extensions", "schema", "data", "live"]

#: Check families that enforce an incubating part of the spec. The spec marks
#: such a requirement ``status: incubating`` and states that it is outside the
#: conformance surface of the current Portolan version, so a store that breaks
#: one is not nonconformant. These checks therefore cap at WARNING, however
#: strong the requirement's own RFC 2119 keyword is. :func:`_build` refuses an
#: ERROR declaration here, and :func:`rashid_core.runner.cap_incubating` caps the
#: findings themselves, so neither a new check nor a severity override can
#: turn an incubating requirement into a conformance failure.
INCUBATING_FAMILIES = frozenset({"ZAR"})
# A check ID is PTL-<family>-<number>.
_ID_PARTS = 3


def is_incubating(check_id: str) -> bool:
    """Whether ``check_id`` belongs to a family that enforces incubating spec text."""
    parts = check_id.split("-")
    return len(parts) == _ID_PARTS and parts[1] in INCUBATING_FAMILIES


@dataclass(frozen=True)
class CheckInfo:
    """A check's identity and the pass that can emit it.

    severity is the strongest finding the check can emit. spec_note explains
    an intentional lack of manifest citations. enables_pass selects the checks
    whose disabling skips the entire pass; diagnostics never enable it, and
    extension information alone does not enable extension schema validation.
    """

    id: str
    description: str
    severity: Severity
    spec_ids: tuple[str, ...] = ()
    stage: PassName = "metadata"
    enables_pass: bool = True
    spec_note: str | None = None


class RegistryError(RuntimeError):
    """A check identity is duplicated or incomplete."""


DEFAULT_RULES: tuple[Rule, ...] = (
    RequiredFilesRule(),
    AgentsLinkRule(),
    ReadmeLinkRule(),
    ReadmeContentRule(),
    ReadmeSectionsRule(),
    TitleDescriptionRule(),
    HumanReadableTitleRule(),
    LinkTitleRule(),
    RequiredLinksRule(),
    ChildLinkCompletenessRule(),
    StructuralLinkTypeRule(),
    LinkResolutionRule(),
    IconMediaTypeRule(),
    IconTitleRule(),
    IconRelativeHrefRule(),
    ContainmentStaysInLanguageRule(),
    BboxValidRule(),
    DatetimePresentRule(),
    DatetimeValidRule(),
    ProducerPresentRule(),
    SingleHostRule(),
    HostContactRule(),
    LicenseDeclaredRule(),
    OtherLicenseLinkRule(),
    NoProprietaryLicenseRule(),
    AssetFieldsRule(),
    AssetHrefSchemeRule(),
    DataAssetSingleFileRule(),
    AssetTitleDescriptionRule(),
    CogMediaTypeRule(),
    AssetFileFieldsRule(),
    ChecksumMultihashRule(),
    CatalogAssetsRule(),
    CollectionDataRule(),
    ItemDataRule(),
    PMTilesDataRule(),
    SchemaUriDeclaredRule(),
    SchemaUriConsistencyRule(),
    VersionExtensionRule(),
    ExtensionVersionRule(),
    AppliedProfileVersionRule(),
    VersionChainRule(),
    MirrorViaLinkRule(),
    MirrorCanonicalLinkRule(),
    MirrorUpdatedRule(),
    OfficialNoUpstreamLinksRule(),
    ThumbnailRule(),
    StylesForDerivativeRule(),
    PMTilesRegistrationRule(),
    StyleMediaTypeRule(),
    DefaultStyleRoleRule(),
    LargeVectorWithoutVisualRule(),
    MosaicOverviewRule(),
    OverviewDatetimeRule(),
    PartitionFieldsRule(),
    PartitionGlobSchemeRule(),
    ClassificationExtensionRule(),
    ClassListRule(),
    NodataClassRule(),
    BandPlacementRule(),
    ItemAssetClassesRule(),
    SingleFileCollectionRule(),
    RasterSceneItemRule(),
    MissingItemTreeRule(),
    NestedCollectionRule(),
    CollectionIdRule(),
    SubcatalogFanoutRule(),
    ItemMirrorPresentRule(),
    ItemMirrorRegistrationRule(),
)


_METADATA_SPEC_NOTES = {
    "PTL-CNF-004": "The extension registry pin sentence has no manifest ID.",
    "PTL-VER-001": "The delegated version extension navigation semantics have no manifest ID.",
    "PTL-PRO-004": "The official-catalog upstream-link sentence has no RFC 2119 keyword.",
    "PTL-CLS-001": "The extension registry row for Classification, a SHOULD for classified"
    " rasters with classification:classes, has no manifest ID.",
}

_PASS_CHECKS: tuple[CheckInfo, ...] = (
    CheckInfo(
        ids.GEN_MISSING_ROOT,
        "the catalog root has a readable catalog.json",
        Severity.ERROR,
        ("PORTO-CORE-001", "PORTO-CORE-013"),
        stage="runner",
    ),
    CheckInfo(
        ids.GEN_UNPARSEABLE,
        "every object file parses as JSON",
        Severity.ERROR,
        ("PORTO-CORE-001",),
        stage="runner",
    ),
    CheckInfo(
        ids.GEN_PARTIAL_TREE,
        "the whole tree was read, so every check could see every file",
        Severity.WARNING,
        (),
        stage="runner",
        enables_pass=False,
        spec_note="An incomplete crawl reports pass availability, not a spec requirement.",
    ),
    CheckInfo(
        ids.STR_UNAVAILABLE,
        "the STAC structural pass could run",
        Severity.WARNING,
        (),
        stage="structural",
        enables_pass=False,
        spec_note="An unavailable structural pass enforces no requirement.",
    ),
    CheckInfo(
        ids.STR_INVALID,
        "every object is valid STAC 1.1.0",
        Severity.ERROR,
        ("PORTO-CORE-001", "PORTO-CORE-013", "PORTO-CORE-019", "PORTO-FMT-036"),
        stage="structural",
    ),
    CheckInfo(
        ids.EXT_UNAVAILABLE,
        "the extension schema pass and each declared schema could load",
        Severity.WARNING,
        (),
        stage="extensions",
        enables_pass=False,
        spec_note="An unavailable extension schema reports pass availability.",
    ),
    CheckInfo(
        ids.EXT_INVALID,
        "every object satisfies the schemas of the extensions it declares",
        Severity.ERROR,
        (),
        stage="extensions",
        spec_note="The delegated extension-validation sentence has no manifest ID.",
    ),
    CheckInfo(
        ids.EXT_UNVALIDATED,
        "every declared extension was validated against its schema",
        Severity.INFO,
        (),
        stage="extensions",
        enables_pass=False,
        spec_note="An unvalidated extension is informational and has no manifest ID.",
    ),
    CheckInfo(
        ids.SCH_UNAVAILABLE,
        "the Portolan profile schema pass could run",
        Severity.WARNING,
        (),
        stage="schema",
        enables_pass=False,
        spec_note="An unavailable profile schema pass enforces no requirement.",
    ),
    CheckInfo(
        ids.SCH_INVALID,
        "every object matches the Portolan profile schema it declares",
        Severity.ERROR,
        ("PORTO-CORE-006",),
        stage="schema",
    ),
    CheckInfo(
        ids.SCH_UNUSED_MAP,
        "every profile schema mapping applies to a validated document",
        Severity.WARNING,
        (),
        stage="schema",
        enables_pass=False,
        spec_note="An unused schema map reports caller configuration, not a spec requirement.",
    ),
    CheckInfo(
        ids.DAT_UNAVAILABLE,
        "the data pass could run",
        Severity.WARNING,
        (),
        stage="data",
        enables_pass=False,
        spec_note="An unavailable data pass enforces no requirement.",
    ),
    CheckInfo(
        ids.DAT_CHECKSUM,
        "asset bytes hash to the declared file:checksum",
        Severity.ERROR,
        ("PORTO-CORE-030",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_SIZE,
        "asset byte count matches the declared file:size",
        Severity.ERROR,
        ("PORTO-CORE-030",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_FORMAT,
        "asset bytes are in the format the declared media type names",
        Severity.ERROR,
        ("PORTO-CORE-026", "PORTO-FMT-001"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_COG,
        "every raster asset is a valid cloud-optimized GeoTIFF",
        Severity.ERROR,
        ("PORTO-FMT-001", "PORTO-FMT-023", "PORTO-FMT-024", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_CONSISTENCY,
        "asset footprint and CRS agree with the declaring metadata",
        Severity.WARNING,
        ("PORTO-FMT-036",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_ORDERING,
        "GeoParquet rows are spatially ordered, measured with the incubating method",
        Severity.WARNING,
        ("PORTO-FMT-006",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_ROWGROUP_STATS,
        "GeoParquet row groups carry spatial statistics",
        Severity.ERROR,
        ("PORTO-FMT-007", "PORTO-FMT-008", "PORTO-FMT-043", "PORTO-FMT-045", "PORTO-FMT-004"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_ROWGROUP_SIZE,
        "GeoParquet row groups stay within the size ceiling",
        Severity.ERROR,
        ("PORTO-FMT-009", "PORTO-FMT-043", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_COG_STATS,
        "every COG band carries embedded min/max/mean/stddev statistics",
        Severity.ERROR,
        ("PORTO-FMT-026", "PORTO-FMT-027", "PORTO-FMT-028", "PORTO-FMT-030", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_VALID_PERCENT,
        "a COG band with nodata declares its valid percent",
        Severity.ERROR,
        ("PORTO-FMT-031", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_OVERVIEWS,
        "a raster larger than one internal tile carries internal overviews",
        Severity.ERROR,
        ("PORTO-FMT-024", "PORTO-FMT-025", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_GEOPARQUET_VERSION,
        "the geo metadata declares GeoParquet 1.1 or 2.x",
        Severity.ERROR,
        ("PORTO-FMT-004", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_TILE_SIZE,
        "internal tiles are square and no larger than 512x512",
        Severity.ERROR,
        ("PORTO-FMT-024", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_PARTITION_SCHEMA,
        "every partition file shares a single Parquet schema",
        Severity.ERROR,
        ("PORTO-FMT-021", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_TABULAR,
        "a plain-Parquet data asset declares table:columns and temporal extent",
        Severity.WARNING,
        ("PORTO-FMT-037", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_MIRROR,
        "an item mirror reproduces the collection's items",
        Severity.ERROR,
        ("PORTO-FMT-042",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_VECTOR_COLUMNS,
        "a GeoParquet data asset's columns are documented with table:columns",
        Severity.WARNING,
        ("PORTO-FMT-046", "PORTO-FMT-047", "PORTO-FMT-048", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_OVERVIEW_FORMAT,
        "a collection overview COG is 8-bit RGB with an optional alpha band",
        Severity.WARNING,
        ("PORTO-FMT-068",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_SCENE_MASK,
        "an imagery scene declares nodata or carries an alpha band or mask",
        Severity.WARNING,
        ("PORTO-FMT-073",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_COLUMN_DESCRIPTIONS,
        "every table:columns entry of a data asset carries a description",
        Severity.WARNING,
        ("PORTO-FMT-077",),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_CLASS_COLORS,
        "a class color_hint matches the COG color table, and the class list fits the file's bands",
        Severity.ERROR,
        ("PORTO-FMT-050", "PORTO-FMT-054"),
        stage="data",
    ),
    CheckInfo(
        ids.DAT_NATIVE_CRS,
        "a native geometry column gives its CRS as inline PROJJSON or a projjson:<key> reference",
        Severity.WARNING,
        ("PORTO-FMT-081",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_STORE_VERSION,
        "a Zarr store asset points at a readable Zarr v3 store, or an Icechunk asset"
        " at a version 2 repository",
        Severity.WARNING,
        ("PORTO-ZARR-001", "PORTO-ZARR-030"),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_CONVENTIONS,
        "the store declares the spatial, proj, and multiscales conventions and conforms to them",
        Severity.WARNING,
        ("PORTO-ZARR-002",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_ASSET,
        "the asset declares the Zarr v3 or Icechunk media type and its href names a group"
        " or the repository root",
        Severity.WARNING,
        ("PORTO-ZARR-004", "PORTO-ZARR-027", "PORTO-ZARR-028"),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_TRANSFORM,
        "every renderable array resolves an affine spatial:transform",
        Severity.WARNING,
        ("PORTO-ZARR-006",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_AXIS_ORDER,
        "the spatial axes are the last two dimensions of every renderable array, Y before X",
        Severity.WARNING,
        ("PORTO-ZARR-007",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_ROW_MAJOR,
        "arrays are stored row-major, with no transpose codec reordering them",
        Severity.WARNING,
        ("PORTO-ZARR-008",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_CRS,
        "the store attributes carry proj:code, or proj:projjson instead",
        Severity.WARNING,
        ("PORTO-ZARR-009", "PORTO-ZARR-010"),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_PYRAMID,
        "a store wider than 4 chunks carries a pyramid whose coarsest level fits in 4",
        Severity.WARNING,
        ("PORTO-ZARR-011", "PORTO-ZARR-012"),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_LAYOUT_FIELDS,
        "every multiscales layout entry carries spatial:transform and spatial:shape",
        Severity.WARNING,
        ("PORTO-ZARR-013",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_LEVEL_STEP,
        "pyramid levels step by a factor of 2",
        Severity.WARNING,
        ("PORTO-ZARR-014",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_VARIABLES,
        "a store with more than one data variable declares cube:variables",
        Severity.WARNING,
        ("PORTO-ZARR-016",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_DISPLAY_RANGE,
        "every renderable array carries band statistics or a renders rescale",
        Severity.WARNING,
        ("PORTO-ZARR-017",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_NODATA,
        "a renderable array declares nodata on its bands entry",
        Severity.WARNING,
        ("PORTO-ZARR-018",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_READ_AMPLIFICATION,
        "a client reads no more than 8 samples for every sample it draws",
        Severity.WARNING,
        ("PORTO-ZARR-020",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_NONSPATIAL_CHUNK,
        "non-spatial dimensions have a chunk length of 1",
        Severity.WARNING,
        ("PORTO-ZARR-021",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_CHUNK_SIZE,
        "chunks are 256 to 1024 samples on X and Y, whatever the shard spans",
        Severity.WARNING,
        ("PORTO-ZARR-023", "PORTO-ZARR-024"),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_SHARD_SPAN,
        "a shard spans the non-spatial dimensions by a product of 8 or less",
        Severity.WARNING,
        ("PORTO-ZARR-025",),
        stage="data",
    ),
    CheckInfo(
        ids.ZAR_CODECS,
        "every codec in the chain has a JavaScript implementation",
        Severity.WARNING,
        ("PORTO-ZARR-026",),
        stage="data",
    ),
    CheckInfo(
        ids.LIV_UNAVAILABLE,
        "the live pass could reach a host to probe",
        Severity.WARNING,
        (),
        stage="live",
        enables_pass=False,
        spec_note="An unavailable live host enforces no requirement.",
    ),
    CheckInfo(
        ids.LIV_RANGE,
        "the server honors ranged GET of a partial-read file with 206 and Accept-Ranges: bytes",
        Severity.ERROR,
        ("PORTO-CORE-043", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_HEAD_LENGTH,
        "HEAD of a partial-read file returns a Content-Length matching the declared file:size",
        Severity.ERROR,
        ("PORTO-CORE-043", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_CORS_ORIGIN,
        "the server allows the requesting CORS origin for metadata and asset files",
        Severity.ERROR,
        ("PORTO-CORE-086", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_CORS_EXPOSE,
        "the server exposes the range headers a browser reads from a partial-read file",
        Severity.ERROR,
        ("PORTO-CORE-045", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_CORS_PREFLIGHT,
        "CORS preflight for a partial-read file allows Range and the conditional headers",
        Severity.ERROR,
        ("PORTO-CORE-045", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_LINK_TARGET,
        "every link target under the publish base answers HEAD with 2xx",
        Severity.ERROR,
        ("PORTO-CORE-035", "PORTO-CORE-036", "PORTO-CORE-086", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_SELF_BASE,
        "the root self link names the base the catalog is published under",
        Severity.WARNING,
        ("PORTO-CORE-081",),
        stage="live",
    ),
    CheckInfo(
        ids.LIV_OFF_HTTPS,
        "no asset or link target on the probed hosts redirects off https",
        Severity.ERROR,
        ("PORTO-CORE-035", "PORTO-CORE-086", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        ids.ZAR_CORS,
        "the host of a Zarr store permits a cross-origin read of its metadata",
        Severity.WARNING,
        ("PORTO-ZARR-003",),
        stage="live",
    ),
)


def _build(
    rules: Sequence[Rule] = DEFAULT_RULES,
    pass_checks: Sequence[CheckInfo] = _PASS_CHECKS,
) -> dict[str, CheckInfo]:
    """Build the registry without losing duplicate IDs to a dict literal."""
    metadata = (
        CheckInfo(
            rule.id,
            rule.description,
            rule.default_severity,
            rule.spec_ids,
            spec_note=_METADATA_SPEC_NOTES.get(rule.id),
        )
        for rule in rules
    )
    checks: dict[str, CheckInfo] = {}
    for info in (*metadata, *pass_checks):
        if info.id in checks:
            raise RegistryError(f"duplicate rule id {info.id}")
        if not info.description:
            raise RegistryError(f"rule {info.id} has no description")
        if not info.spec_ids and not info.spec_note:
            raise RegistryError(f"rule {info.id} has no spec metadata")
        if is_incubating(info.id) and info.severity is Severity.ERROR:
            raise RegistryError(
                f"rule {info.id} enforces incubating spec text and may not declare an error"
            )
        checks[info.id] = info
    return checks


CHECKS: Mapping[str, CheckInfo] = MappingProxyType(_build())


def check_ids(stage: PassName, *, enables_pass_only: bool = False) -> frozenset[str]:
    """IDs a pass can emit, optionally limited to those that keep it enabled."""
    return frozenset(
        info.id
        for info in CHECKS.values()
        if info.stage == stage and (not enables_pass_only or info.enables_pass)
    )


def describe(rule_id: str) -> str:
    """The one-line description of ``rule_id``, or ``""`` when unknown.

    Reporting code renders whatever ids a report carries, including ids from a
    future version or a caller's own rule, so an unknown id yields an empty
    column rather than raising.
    """
    info = CHECKS.get(rule_id)
    return info.description if info is not None else ""
