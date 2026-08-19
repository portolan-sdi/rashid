"""The default Portolan metadata-pass rule set."""

from __future__ import annotations

from rashid.rule import Rule
from rashid.rules.assets import (
    AssetFieldsRule,
    AssetFileFieldsRule,
    AssetHrefSchemeRule,
    CatalogAssetsRule,
    ChecksumMultihashRule,
    CogMediaTypeRule,
)
from rashid.rules.bbox import BboxValidRule
from rashid.rules.catalogs import SubcatalogFanoutRule
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
    NoSelfLinkRule,
    RelativeLinksRule,
    RequiredLinksRule,
    StructuralLinkTypeRule,
)
from rashid.rules.partitions import PartitionFieldsRule
from rashid.rules.provenance import (
    MirrorCanonicalLinkRule,
    MirrorUpdatedRule,
    MirrorViaLinkRule,
    OfficialNoUpstreamLinksRule,
)
from rashid.rules.providers import HostContactRule, ProducerPresentRule, SingleHostRule
from rashid.rules.temporal import DatetimePresentRule, DatetimeValidRule
from rashid.rules.titles import HumanReadableTitleRule, LinkTitleRule, TitleDescriptionRule
from rashid.rules.viz import (
    DefaultStyleRoleRule,
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
    CogMediaTypeRule(),
    AssetFileFieldsRule(),
    ChecksumMultihashRule(),
    CatalogAssetsRule(),
    SchemaUriDeclaredRule(),
    SchemaUriConsistencyRule(),
    VersionExtensionRule(),
    ExtensionVersionRule(),
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
    PartitionFieldsRule(),
    SingleFileCollectionRule(),
    RasterSceneItemRule(),
    MissingItemTreeRule(),
    NestedCollectionRule(),
    CollectionIdRule(),
    SubcatalogFanoutRule(),
    ItemMirrorPresentRule(),
    ItemMirrorRegistrationRule(),
)

__all__ = ["DEFAULT_RULES"]
