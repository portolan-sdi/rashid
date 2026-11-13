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

from rashid.model import Severity
from rashid.rule import Rule
from rashid.rules.assets import (
    AssetFieldsRule,
    AssetFileFieldsRule,
    AssetHrefSchemeRule,
    CatalogAssetsRule,
    ChecksumMultihashRule,
    CogMediaTypeRule,
    CollectionDataRule,
    DataAssetSingleFileRule,
    ItemDataRule,
    PMTilesDataRule,
)
from rashid.rules.bbox import BboxValidRule
from rashid.rules.catalogs import SubcatalogFanoutRule
from rashid.rules.classification import (
    BandPlacementRule,
    ClassificationExtensionRule,
    ClassListRule,
    ClassValueRangeRule,
    ItemAssetClassesRule,
    NodataClassRule,
)
from rashid.rules.collections import (
    CollectionIdRule,
    MissingItemTreeRule,
    NestedCollectionRule,
    RasterSceneItemRule,
    SingleFileCollectionRule,
)
from rashid.rules.conformance import (
    ExtensionVersionRule,
    SchemaUriConsistencyRule,
    SchemaUriDeclaredRule,
    VersionExtensionRule,
)
from rashid.rules.files import (
    AgentsLinkRule,
    ReadmeContentRule,
    ReadmeLinkRule,
    ReadmeSectionsRule,
    RequiredFilesRule,
)
from rashid.rules.item_mirror import ItemMirrorPresentRule, ItemMirrorRegistrationRule
from rashid.rules.license import (
    LicenseDeclaredRule,
    NoProprietaryLicenseRule,
    OtherLicenseLinkRule,
)
from rashid.rules.links import (
    ChildLinkCompletenessRule,
    ContainmentStaysInLanguageRule,
    IconMediaTypeRule,
    IconRelativeHrefRule,
    IconTitleRule,
    LinkResolutionRule,
    RequiredLinksRule,
    StructuralLinkTypeRule,
)
from rashid.rules.partitions import PartitionFieldsRule, PartitionGlobSchemeRule
from rashid.rules.provenance import (
    MirrorCanonicalLinkRule,
    MirrorUpdatedRule,
    MirrorViaLinkRule,
    OfficialNoUpstreamLinksRule,
)
from rashid.rules.providers import HostContactRule, ProducerPresentRule, SingleHostRule
from rashid.rules.temporal import DatetimePresentRule, DatetimeValidRule
from rashid.rules.titles import HumanReadableTitleRule, LinkTitleRule, TitleDescriptionRule
from rashid.rules.versions import VersionChainRule
from rashid.rules.viz import (
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


GEN_MISSING_ROOT = "PTL-GEN-000"
GEN_UNPARSEABLE = "PTL-GEN-001"
GEN_PARTIAL_TREE = "PTL-GEN-002"
STR_INVALID = "PTL-STR-001"
STR_UNAVAILABLE = "PTL-STR-000"
SCH_INVALID = "PTL-SCH-001"
SCH_UNAVAILABLE = "PTL-SCH-000"
EXT_INVALID = "PTL-EXT-001"
EXT_UNAVAILABLE = "PTL-EXT-000"
EXT_UNVALIDATED = "PTL-EXT-002"
DAT_UNAVAILABLE = "PTL-DAT-000"
DAT_CHECKSUM = "PTL-DAT-001"
DAT_SIZE = "PTL-DAT-002"
DAT_FORMAT = "PTL-DAT-003"
DAT_COG = "PTL-DAT-004"
DAT_CONSISTENCY = "PTL-DAT-005"
DAT_ORDERING = "PTL-DAT-006"
DAT_ROWGROUP_STATS = "PTL-DAT-007"
DAT_ROWGROUP_SIZE = "PTL-DAT-008"
DAT_COG_STATS = "PTL-DAT-009"
DAT_VALID_PERCENT = "PTL-DAT-010"
DAT_OVERVIEWS = "PTL-DAT-011"
DAT_GEOPARQUET_VERSION = "PTL-DAT-012"
DAT_TILE_SIZE = "PTL-DAT-013"
DAT_PARTITION_SCHEMA = "PTL-DAT-014"
DAT_TABULAR = "PTL-DAT-015"
DAT_MIRROR = "PTL-DAT-016"
DAT_VECTOR_COLUMNS = "PTL-DAT-017"
DAT_OVERVIEW_FORMAT = "PTL-DAT-018"
DAT_SCENE_MASK = "PTL-DAT-019"
DAT_COLUMN_DESCRIPTIONS = "PTL-DAT-020"
DAT_CLASS_COLORS = "PTL-DAT-021"
DAT_PALETTE_CLASSES = "PTL-DAT-022"
LIV_UNAVAILABLE = "PTL-LIV-000"
LIV_RANGE = "PTL-LIV-001"
LIV_HEAD_LENGTH = "PTL-LIV-002"
LIV_CORS_ORIGIN = "PTL-LIV-003"
LIV_CORS_EXPOSE = "PTL-LIV-004"
LIV_CORS_PREFLIGHT = "PTL-LIV-005"
LIV_LINK_TARGET = "PTL-LIV-006"
LIV_SELF_BASE = "PTL-LIV-007"
LIV_OFF_HTTPS = "PTL-LIV-008"

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
    ClassValueRangeRule(),
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
    "PTL-CLS-001": "Declaring the extension a field belongs to is a STAC rule with no manifest ID.",
}

_PASS_CHECKS: tuple[CheckInfo, ...] = (
    CheckInfo(
        GEN_MISSING_ROOT,
        "the catalog root has a readable catalog.json",
        Severity.ERROR,
        ("PORTO-CORE-001", "PORTO-CORE-013"),
        stage="runner",
    ),
    CheckInfo(
        GEN_UNPARSEABLE,
        "every object file parses as JSON",
        Severity.ERROR,
        ("PORTO-CORE-001",),
        stage="runner",
    ),
    CheckInfo(
        GEN_PARTIAL_TREE,
        "the whole tree was read, so every check could see every file",
        Severity.WARNING,
        (),
        stage="runner",
        enables_pass=False,
        spec_note="An incomplete crawl reports pass availability, not a spec requirement.",
    ),
    CheckInfo(
        STR_UNAVAILABLE,
        "the STAC structural pass could run",
        Severity.WARNING,
        (),
        stage="structural",
        enables_pass=False,
        spec_note="An unavailable structural pass enforces no requirement.",
    ),
    CheckInfo(
        STR_INVALID,
        "every object is valid STAC 1.1.0",
        Severity.ERROR,
        ("PORTO-CORE-001", "PORTO-CORE-013", "PORTO-CORE-019", "PORTO-FMT-036"),
        stage="structural",
    ),
    CheckInfo(
        EXT_UNAVAILABLE,
        "the extension schema pass and each declared schema could load",
        Severity.WARNING,
        (),
        stage="extensions",
        enables_pass=False,
        spec_note="An unavailable extension schema reports pass availability.",
    ),
    CheckInfo(
        EXT_INVALID,
        "every object satisfies the schemas of the extensions it declares",
        Severity.ERROR,
        (),
        stage="extensions",
        spec_note="The delegated extension-validation sentence has no manifest ID.",
    ),
    CheckInfo(
        EXT_UNVALIDATED,
        "every declared extension was validated against its schema",
        Severity.INFO,
        (),
        stage="extensions",
        enables_pass=False,
        spec_note="An unvalidated extension is informational and has no manifest ID.",
    ),
    CheckInfo(
        SCH_UNAVAILABLE,
        "the Portolan profile schema pass could run",
        Severity.WARNING,
        (),
        stage="schema",
        enables_pass=False,
        spec_note="An unavailable profile schema pass enforces no requirement.",
    ),
    CheckInfo(
        SCH_INVALID,
        "every object matches the Portolan profile schema it declares",
        Severity.ERROR,
        ("PORTO-CORE-006",),
        stage="schema",
    ),
    CheckInfo(
        DAT_UNAVAILABLE,
        "the data pass could run",
        Severity.WARNING,
        (),
        stage="data",
        enables_pass=False,
        spec_note="An unavailable data pass enforces no requirement.",
    ),
    CheckInfo(
        DAT_CHECKSUM,
        "asset bytes hash to the declared file:checksum",
        Severity.ERROR,
        ("PORTO-CORE-030",),
        stage="data",
    ),
    CheckInfo(
        DAT_SIZE,
        "asset byte count matches the declared file:size",
        Severity.ERROR,
        ("PORTO-CORE-030",),
        stage="data",
    ),
    CheckInfo(
        DAT_FORMAT,
        "asset bytes are in the format the declared media type names",
        Severity.ERROR,
        ("PORTO-CORE-026", "PORTO-FMT-001"),
        stage="data",
    ),
    CheckInfo(
        DAT_COG,
        "every raster asset is a valid cloud-optimized GeoTIFF",
        Severity.ERROR,
        ("PORTO-FMT-001", "PORTO-FMT-023", "PORTO-FMT-024", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_CONSISTENCY,
        "asset footprint and CRS agree with the declaring metadata",
        Severity.WARNING,
        ("PORTO-FMT-036",),
        stage="data",
    ),
    CheckInfo(
        DAT_ORDERING,
        "GeoParquet rows are spatially ordered",
        Severity.ERROR,
        ("PORTO-FMT-006", "PORTO-FMT-043", "PORTO-FMT-044", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_ROWGROUP_STATS,
        "GeoParquet row groups carry spatial statistics",
        Severity.ERROR,
        ("PORTO-FMT-007", "PORTO-FMT-008", "PORTO-FMT-043", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_ROWGROUP_SIZE,
        "GeoParquet row groups stay within the size ceiling",
        Severity.ERROR,
        ("PORTO-FMT-009", "PORTO-FMT-043", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_COG_STATS,
        "every COG band carries embedded min/max/mean/stddev statistics",
        Severity.ERROR,
        ("PORTO-FMT-026", "PORTO-FMT-027", "PORTO-FMT-028", "PORTO-FMT-030", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_VALID_PERCENT,
        "a COG band with nodata declares its valid percent",
        Severity.ERROR,
        ("PORTO-FMT-031", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_OVERVIEWS,
        "a raster larger than one internal tile carries internal overviews",
        Severity.ERROR,
        ("PORTO-FMT-024", "PORTO-FMT-025", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_GEOPARQUET_VERSION,
        "the geo metadata declares GeoParquet 1.1 or 2.x",
        Severity.ERROR,
        ("PORTO-FMT-004", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_TILE_SIZE,
        "internal tiles are square and no larger than 512x512",
        Severity.ERROR,
        ("PORTO-FMT-024", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_PARTITION_SCHEMA,
        "every partition file shares a single Parquet schema",
        Severity.ERROR,
        ("PORTO-FMT-021", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_TABULAR,
        "a plain-Parquet data asset declares table:columns and temporal extent",
        Severity.WARNING,
        ("PORTO-FMT-037", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_MIRROR,
        "an item mirror reproduces the collection's items",
        Severity.ERROR,
        ("PORTO-FMT-042",),
        stage="data",
    ),
    CheckInfo(
        DAT_VECTOR_COLUMNS,
        "a GeoParquet data asset's columns are documented with table:columns",
        Severity.WARNING,
        ("PORTO-FMT-046", "PORTO-FMT-047", "PORTO-FMT-048", "PORTO-FMT-045"),
        stage="data",
    ),
    CheckInfo(
        DAT_OVERVIEW_FORMAT,
        "a collection overview COG is 8-bit RGB with an optional alpha band",
        Severity.WARNING,
        ("PORTO-FMT-068",),
        stage="data",
    ),
    CheckInfo(
        DAT_SCENE_MASK,
        "an imagery scene declares nodata or carries an alpha band or mask",
        Severity.WARNING,
        ("PORTO-FMT-073",),
        stage="data",
    ),
    CheckInfo(
        DAT_COLUMN_DESCRIPTIONS,
        "every table:columns entry of a data asset carries a description",
        Severity.WARNING,
        ("PORTO-FMT-077",),
        stage="data",
    ),
    CheckInfo(
        DAT_CLASS_COLORS,
        "a class list agrees with the COG it describes: its band, nodata, and color table",
        Severity.ERROR,
        ("PORTO-FMT-050", "PORTO-FMT-054", "PORTO-FMT-057"),
        stage="data",
    ),
    CheckInfo(
        DAT_PALETTE_CLASSES,
        "a COG band with an embedded color table declares classification:classes",
        Severity.INFO,
        (),
        stage="data",
        spec_note="A palette only hints at classes; PORTO-FMT-049 is enforcement: process.",
    ),
    CheckInfo(
        LIV_UNAVAILABLE,
        "the live pass could reach a host to probe",
        Severity.WARNING,
        (),
        stage="live",
        enables_pass=False,
        spec_note="An unavailable live host enforces no requirement.",
    ),
    CheckInfo(
        LIV_RANGE,
        "the server honors ranged GET of a partial-read file with 206 and Accept-Ranges: bytes",
        Severity.ERROR,
        ("PORTO-CORE-043", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        LIV_HEAD_LENGTH,
        "HEAD of a partial-read file returns a Content-Length matching the declared file:size",
        Severity.ERROR,
        ("PORTO-CORE-043", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        LIV_CORS_ORIGIN,
        "the server allows the requesting CORS origin for metadata and asset files",
        Severity.ERROR,
        ("PORTO-CORE-086", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        LIV_CORS_EXPOSE,
        "the server exposes the range headers a browser reads from a partial-read file",
        Severity.ERROR,
        ("PORTO-CORE-045", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        LIV_CORS_PREFLIGHT,
        "CORS preflight for a partial-read file allows Range and the conditional headers",
        Severity.ERROR,
        ("PORTO-CORE-045", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        LIV_LINK_TARGET,
        "every link target under the publish base answers HEAD with 2xx",
        Severity.ERROR,
        ("PORTO-CORE-035", "PORTO-CORE-036", "PORTO-CORE-086", "PORTO-CORE-073"),
        stage="live",
    ),
    CheckInfo(
        LIV_SELF_BASE,
        "the root self link names the base the catalog is published under",
        Severity.WARNING,
        ("PORTO-CORE-081",),
        stage="live",
    ),
    CheckInfo(
        LIV_OFF_HTTPS,
        "no asset or link target on the probed hosts redirects off https",
        Severity.ERROR,
        ("PORTO-CORE-023", "PORTO-CORE-035", "PORTO-CORE-073"),
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
