"""The default Portolan metadata-pass rule set."""

from __future__ import annotations

from reis.rule import Rule
from reis.rules.assets import (
    AssetFieldsRule,
    AssetFileFieldsRule,
    AssetHrefSchemeRule,
    CatalogAssetsRule,
    ChecksumMultihashRule,
)
from reis.rules.bbox import BboxValidRule
from reis.rules.collections import (
    CollectionIdRule,
    NestedCollectionRule,
    SingleFileCollectionRule,
)
from reis.rules.conformance import (
    SchemaUriConsistencyRule,
    SchemaUriDeclaredRule,
    VersionExtensionRule,
)
from reis.rules.files import (
    AgentsLinkRule,
    ReadmeContentRule,
    ReadmeLinkRule,
    ReadmeSectionsRule,
    RequiredFilesRule,
)
from reis.rules.license import (
    LicenseDeclaredRule,
    NoProprietaryLicenseRule,
    OtherLicenseLinkRule,
)
from reis.rules.links import (
    ChildLinkCompletenessRule,
    LinkResolutionRule,
    NoSelfLinkRule,
    RelativeLinksRule,
    RequiredLinksRule,
    StructuralLinkTypeRule,
)
from reis.rules.partitions import PartitionFieldsRule
from reis.rules.provenance import (
    MirrorCanonicalLinkRule,
    MirrorUpdatedRule,
    MirrorViaLinkRule,
    OfficialNoUpstreamLinksRule,
)
from reis.rules.providers import HostContactRule, ProducerPresentRule, SingleHostRule
from reis.rules.temporal import DatetimePresentRule, DatetimeValidRule
from reis.rules.titles import HumanReadableTitleRule, LinkTitleRule, TitleDescriptionRule
from reis.rules.viz import (
    LargeVectorWithoutVisualRule,
    PMTilesRegistrationRule,
    StyleMediaTypeRule,
    StylesForDerivativeRule,
    ThumbnailRule,
)

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
    RelativeLinksRule(),
    NoSelfLinkRule(),
    LinkResolutionRule(),
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
    AssetFileFieldsRule(),
    ChecksumMultihashRule(),
    CatalogAssetsRule(),
    SchemaUriDeclaredRule(),
    SchemaUriConsistencyRule(),
    VersionExtensionRule(),
    MirrorViaLinkRule(),
    MirrorCanonicalLinkRule(),
    MirrorUpdatedRule(),
    OfficialNoUpstreamLinksRule(),
    ThumbnailRule(),
    StylesForDerivativeRule(),
    PMTilesRegistrationRule(),
    StyleMediaTypeRule(),
    LargeVectorWithoutVisualRule(),
    PartitionFieldsRule(),
    SingleFileCollectionRule(),
    NestedCollectionRule(),
    CollectionIdRule(),
)

__all__ = ["DEFAULT_RULES"]
