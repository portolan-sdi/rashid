"""The default Portolan metadata-pass rule set."""

from __future__ import annotations

from reis.rule import Rule
from reis.rules.assets import (
    AssetFieldsRule,
    AssetFileFieldsRule,
    AssetHrefSchemeRule,
    ChecksumMultihashRule,
)
from reis.rules.bbox import BboxValidRule
from reis.rules.collections import SingleFileCollectionRule
from reis.rules.conformance import SchemaUriConsistencyRule, SchemaUriDeclaredRule
from reis.rules.files import AgentsLinkRule, ReadmeLinkRule, RequiredFilesRule
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
from reis.rules.partitions import PartitionGlobRule
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
    StylesForDerivativeRule,
    ThumbnailRule,
)

DEFAULT_RULES: tuple[Rule, ...] = (
    RequiredFilesRule(),
    AgentsLinkRule(),
    ReadmeLinkRule(),
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
    SchemaUriDeclaredRule(),
    SchemaUriConsistencyRule(),
    MirrorViaLinkRule(),
    MirrorCanonicalLinkRule(),
    MirrorUpdatedRule(),
    OfficialNoUpstreamLinksRule(),
    ThumbnailRule(),
    StylesForDerivativeRule(),
    PMTilesRegistrationRule(),
    LargeVectorWithoutVisualRule(),
    PartitionGlobRule(),
    SingleFileCollectionRule(),
)

__all__ = ["DEFAULT_RULES"]
