# Rules

This page lists every rule that rashid applies. For an overview of each validation pass, see the [README](../README.md).

## Understand Findings

Each finding contains a stable rule ID in the form `PTL-<GROUP>-<NNN>`. It also contains a severity, a message, and the path of the affected file.

When applicable, a finding also contains:

- A `json_pointer` that locates the problem in the file.
- An imperative `fix_hint` that describes the repair.
- Structured `expected` and `actual` values for concrete comparisons, such as checksums.

Every metadata error includes a fix hint. If a JSON location exists, the error also includes a pointer. A coverage test enforces these requirements.

These fields support an agent-based repair cycle: check the catalog, apply the hints, and check it again.

Severities follow the Portolan specification. MUST requirements produce errors, and SHOULD requirements produce warnings. The following rules need more context or use a different severity:

- `PTL-CNF-002` produces a warning when a schema URI differs from the root catalog's URI. The specification makes this exception because a mixed-version catalog remains valid.
- `PTL-CNF-004` produces a warning when an extension version is behind the registry. The registry can advance after a catalog pins the current version, so the catalog is behind rather than broken.
- `PTL-TTL-002` produces a warning because its machine-generated title check is heuristic. A severity override can promote it.
- `PTL-PRO-002` produces an info finding when a mirror lacks a `canonical` link. Metadata cannot show whether the upstream publisher provides STAC.
- `PTL-EXT-002` produces an info finding when an object declares an extension the profile's registry does not list. The finding records that rashid did not validate against that schema. It does not claim the object is wrong.
- `PTL-EXT-000` produces a warning when one extension schema cannot be fetched or resolved. rashid reports it once for each URI and validates every other extension.
- `PTL-VIZ-004` produces an info finding when a large vector lacks a visual derivative. The render-path MUST depends on whether source rendering is viable, which metadata cannot prove. The 100 MB threshold is also heuristic.

`PTL-CNF-004` reads `src/rashid/_schemas/extension-registry.json`. This file contains the profile's registry table from `stac/README.md` in the specification.

At vendor time, `scripts/vendor_spec_fixtures.py` parses that table. The rule ignores extensions that the registry does not list. `PTL-CNF-001` and `PTL-CNF-002` handle the Portolan schema URI's version.

The same file drives `PTL-EXT-001` and the schema vendoring. `scripts/vendor_stac_schemas.py` reads the registry and fetches the `$ref` closure of every pinned extension into `src/rashid/_schemas/extensions/`. A version pinned in the specification arrives with its schema on the next spec sync. rashid needs no code change to validate against it.

A registered extension declared at a version the registry does not pin is validated against the schema of the version it declares. rashid fetches that schema over https unless the package retains a compatibility version. Contacts v0.1.1 remains packaged beside v1.0.0. A newer schema would report fields that the declared version never defined. A skip would let a catalog turn its schema errors into a `PTL-CNF-004` warning when it declares an older version.

## Understand Special Cases

An alternate-language tree translates a catalog but is not part of that catalog. The specification defines this behavior in `core.md` under Alternate-Language Trees.

`CatalogGraph.translation_roots()` finds a translation root. It follows the root catalog's `rel:"alternate"` links with the `application/json` media type.

Four rules account for these separate trees:

- `PTL-LNK-001` does not require a translation root to have a `parent` link.
- `PTL-LNK-002` does not require the catalog to have a `child` link to a translation root.
- `PTL-COL-003` checks ID uniqueness within one language tree. A collection can keep the same ID in each language.
- `PTL-LNK-010` reports a `child` or `item` link that enters a translation tree.

`PTL-COL-001` applies only to the exact structure that the specification prohibits. That structure has one item, one data asset on the item, and no data asset on the collection.

The rule permits several items, several data files, and partitioned collections. If collection assets omit `roles`, rashid cannot decide whether the rule applies. `PTL-AST-001` reports that omission instead.

`PTL-COL-005` reports a collection that owes items but publishes none. Two signals show this fault, and each one is sufficient:

- Two or more raster scene files sit in the collection directory, and no asset of the collection declares them.
- The collection registers an `items.parquet` mirror. That registration is itself a claim that items exist.

The disk signal takes precedence when both apply, so one collection produces one finding. The message names the requirements that the structure breaks.

The mirror signal reads metadata only. It therefore applies when the `data` extra is absent, when the Parquet file is unreadable, and when the asset is remote. `PTL-DAT-016` stands back from a collection with no items and keeps its row-level comparisons for collections that have them.

Any declaration exempts a file, including a `source` original and an alternate representation. The rule counts undeclared files only, so it never overlaps `PTL-COL-004`. The following structures are also exempt:

- A collection with one scene file. The specification makes a lone COG a collection asset with no item directory.
- A partitioned collection. The specification permits partitions that are not items.
- A collection whose items sit under a catalog below it. Containment moves, but ownership does not.
- A collection in an alternate-language tree. Each tree is judged against its own directory and its own items.

`PTL-VIZ-001` reports when metadata cannot identify a collection as geospatial or tabular. The specification identifies tabular data through a Parquet geometry column, which requires the data pass.

If the collection has no thumbnail, the rule produces a warning and names the missing signals. The requirement is a MUST, so silence could imply that the collection passed. A collection with a thumbnail meets the requirement either way and produces no finding.

`PTL-VIZ-004` reports the same uncertainty at info severity when its own recommendation would otherwise apply.

The following metadata signals identify a geospatial collection:

- An item geometry.
- A spatial media type: PMTiles, COG, or COPC.
- A geometry column in `table:columns` on the collection or any non-`source` asset.

A declared column list without a geometry column identifies a tabular collection. The collection is then exempt from the thumbnail requirement.

The rule's message reports absent geometry evidence only. `PTL-COL-005` answers whether the collection owes items at all, so a structural fault never reaches the reader through a sentence about thumbnails.

## Rule Groups

| Group | Rules | Checks |
|---|---|---|
| `PTL-GEN` | 000–002 | Requires a root `catalog.json` and parseable object files. Rule `002` warns when rashid read the catalog over https by following links. A crawl reads only the documents that links point to, so `PTL-LNK-002` cannot report an object without an inbound link, and `PTL-COL-005` cannot report scene files beside a collection. Rule `002` also warns when the crawl stopped at the document limit or when the fetch of a document failed. |
| `PTL-LNK` | 001–003, 006–010 | Checks required structural links, child and item completeness, link types, and resolved target objects. Resolves an absolute link through the published base the root `self` link names. Requires a `rel:"icon"` logo to have a renderable media type, title, and relative `href`. Keeps `child` and `item` links out of alternate-language trees. |
| `PTL-TTL` | 001–003 | Requires non-empty titles and descriptions, human-readable titles, and titles on child and item links. |
| `PTL-BBX` | 001 | Requires finite, sentinel-free WGS84 bounding boxes with south less than or equal to north. Supports 2D and 3D boxes. |
| `PTL-TMP` | 001–002 | Requires an item datetime or interval, RFC 3339 values, and a start before or equal to the end. |
| `PTL-PRV` | 001–003 | Requires at least one producer and exactly one host. The host must appear last and include a URL or email address. |
| `PTL-LIC` | 001–003 | Requires an SPDX identifier or `other`, a license link for `other`, and no `proprietary` value. |
| `PTL-FIL` | 001–005 | Requires `AGENTS.md` and `README.md` on disk. Requires Markdown links with `rel:"agents"` and `rel:"describedby"`. Requires a non-empty README with a title heading. Warns heuristically when a collection README omits license or provenance information. |
| `PTL-AST` | 001–009 | Checks asset `href`, media type, and roles. Requires the COG media type for primary raster data and HTTPS instead of S3 URLs. Requires `file:size` and multihash-encoded `file:checksum`; their absence produces a warning under the SHOULD requirement. Rule `003` exempts assets that refer to their containing STAC object and reports either field on them as an error. Prohibits assets on catalogs. Rule `007` requires each collection to refer to its data: an asset with the `data` role on the collection or on one of its items, or a `partition:glob`. PMTiles alone does not supply primary data. Rule `008` requires each item to carry an asset with the `data` role. Rule `009` requires PMTiles assets to carry `visual` and reference GeoParquet through their collection or its items, or through `partition:glob`. |
| `PTL-VER` | 001 | Warns about inconsistent version navigation. See [Version chains](#version-chains). |
| `PTL-CNF` | 001–004 | Requires a versioned Portolan schema URI that agrees with the root catalog. Requires the STAC version extension for dataset versioning. Requires each registered extension to use the version in the profile's extension registry. |
| `PTL-PRO` | 001–004 | Checks mirror `via` and `canonical` links plus the `updated` synchronization time. Prohibits upstream links on official catalogs. |
| `PTL-VIZ` | 001–006 | Requires thumbnails on geospatial collections, style assets for visual derivatives, and `rel:"pmtiles"` registration for PMTiles. Reports large vectors without a visual derivative. Requires the MapLibre style media type in PMTiles collections. Requires the `default` role on one of several styles. |
| `PTL-STR` | 000–001 | Checks STAC 1.1.0 core structure offline with the included core schemas. Rule `000` warns when the pass cannot run. |
| `PTL-SCH` | 000–001 | Checks every object against its declared Portolan profile schema. Rule `000` warns when the pass cannot run. |
| `PTL-EXT` | 000–002 | Checks every object against the schemas of the STAC extensions it declares. Pinned versions use packaged schemas. A registered extension at another version uses its fetched schema. Rule `000` warns when the pass or one schema cannot load. Rule `002` reports once per catalog that an unregistered extension was not validated. `--schema-allow-network` fetches and validates it instead. |
| `PTL-DAT` | 000–017 | Compares asset bytes with metadata and format MUST requirements. Checks `file:checksum`, `file:size`, format signatures, bounding boxes, and coordinate reference systems. Requires valid COGs with internal overviews, embedded band statistics, and valid percentages for nodata bands. Requires GeoParquet 1.1 or 2.x, spatial ordering, per-row-group statistics, and suitable row-group size. Statistics can use a bounding-box covering column or native 2.x `GeospatialStatistics`. Requires square internal tiles no larger than 512 pixels. Requires one Parquet schema across local partition files. Applies the tabular SHOULD requirements for `table:columns` and `extent.temporal` to plain-Parquet collection assets with temporal columns. Compares an item mirror's row count, IDs, geometry, datetime, and bounding box with the collection's items. Rule `017` recommends `table:columns` for GeoParquet data assets, on the collection, item properties, or each asset. Rule `000` warns when the geospatial stack cannot load. Source and alternate assets are exempt from format MUST requirements. Plain Parquet is exempt from GeoParquet rules. Those rules apply to an item mirror like any other spatial table. |
| `PTL-LIV` | 000–008 | Checks the hosting server's Data Storage MUST requirements for absolute HTTPS asset URLs. For each host, checks ranged GET support through `206` and `Accept-Ranges: bytes`, `Content-Length` on HEAD, CORS origins, exposed headers, and preflight support. Preflight must allow `GET`, `HEAD`, `Range`, and conditional-request headers. HEAD size must match `file:size`. Source and alternate assets are exempt. The publish base comes from the catalog URL, from `--live-base-url`, or from the root catalog's absolute `self` link. With a base, relative asset hrefs are probed too, and rule `006` sends one HEAD for each distinct link target under the base and reports each target that does not answer `2xx`. This covers `child`, `item`, `parent`, `root`, `self`, `agents`, `describedby`, and every other link that resolves under the base. Links to other hosts are not probed. Rule `007` warns when the root `self` link names a location other than the base the catalog was checked under. Rule `008` reports an asset or link target that redirects to a URL that is not `https`. The probe does not follow that redirect, so no request leaves `https`. When the first asset on a host redirects that way, the range and CORS probes use the next asset whose HEAD stays on `https`. Rule `000` warns when no such asset exists. Rule `000` warns when no asset is available to probe or a host is unavailable. HEAD or range responses of `401` or `403` give `PTL-LIV-000` "asset inaccessible" warnings instead of size or range errors; [#267](https://github.com/portolan-sdi/rashid/issues/267) tracks authenticated checking. |
| `PTL-PRT` | 001–002 | Requires a partitioned collection to declare the partition extension and provide `partition:scheme`, `partition:keys`, and `partition:glob`. Rejects HTTP and HTTPS partition globs. |
| `PTL-COL` | 001–005 | Requires a single-file collection to expose data through a collection asset instead of one item. Prohibits nested collections. Requires collection IDs to follow the naming convention and remain unique within their language tree. Requires raster scenes to appear on items instead of the collection. Requires a collection to publish the items that its scene files or its item mirror imply. |
| `PTL-CAT` | 001 | Recommends subcatalogs when a catalog or collection has at least 20 ungrouped collections or items. Subcatalogs do not count as ungrouped children. |
| `PTL-ZAR` | 001–019 | Checks a Zarr store asset against the incubating Zarr profile. Reads only Zarr metadata, never chunk data. Requires Zarr v3, the `spatial`, `proj`, and `multiscales` conventions, the `application/vnd.zarr; version=3` media type on an asset whose `href` names a group, an affine `spatial:transform` on every renderable array with the spatial axes last and Y before X, row-major storage, `proj:code` or `proj:projjson` in the store attributes, a multiscale pyramid whose coarsest level fits in 4 chunks, `cube:variables` for more than one data variable, a display range, a declared `nodata`, chunking that reads no more than 8 samples per drawn sample, chunks of 256 to 1024 samples on X and Y, and codecs with a JavaScript implementation. Rule `003` probes the store's cross-origin policy from the live pass. Rule `001` reports an unreadable store as information. See [Incubating Checks](#incubating-checks). |
| `PTL-MIR` | 001–002 | Recommends an `items.parquet` mirror for a raster collection with scene items. Requires a published mirror to be a collection asset with the `collection-mirror` role and `application/vnd.apache.parquet` media type, as the stac-geoparquet specification defines. |

`rashid.registry.CHECKS` provides the same list in machine-readable form. It maps each check ID to its description, highest possible severity, specification requirements, and validation pass. Checks without a manifest requirement record the reason in `spec_note`.

The per-rule summary and JSON `summary.by_rule` object read this registry. Call `rashid.describe(rule_id)` to get a rule's description.

### Incubating Checks

The specification marks some requirements `status: incubating`. The Zarr profile in `specs/incubating/zarr.md` holds every one of them today. Such a requirement sits outside the conformance surface of the current Portolan version, so a store that breaks one is not nonconformant.

Every `PTL-ZAR` check therefore produces a warning at most, whatever RFC 2119 keyword its requirement carries. Two guards enforce that. `rashid.registry` rejects a check in an incubating family that declares an error severity, and `rashid.runner.cap_incubating` softens any error finding from such a check before the report is built. The cap runs after severity overrides, so `severity` configuration cannot promote an incubating finding either. A Zarr store with broken metadata produces warnings and the run still passes.

The catalog tree loads from a local directory. `CatalogGraph` in `src/rashid/catalog.py` provides the only metadata I/O layer and loads the tree in one pass. For a catalog URL, `src/rashid/remote.py` first fetches the tree into a temporary directory by following the `child`, `item`, and JSON `alternate` links from the root, and by fetching `AGENTS.md` and `README.md` beside each catalog and collection. `CatalogGraph` then loads that directory. The graph records the URL as `base_url`, so the data pass reads relative asset hrefs from the host and the live pass probes under the same base.

## Network Cost

Only the data and live passes use the network by default for a directory. A catalog URL adds the crawl, and turns the live pass on. Only the data pass transfers asset bytes.

| Pass | Requests | Data transferred |
|---|---|---|
| Crawl (catalog URL only) | One GET for each document that a `child`, `item`, or JSON `alternate` link points to, plus two for the `AGENTS.md` and `README.md` beside each catalog and collection. rashid sends up to 8 requests at a time and stops at 10000 documents. | The STAC JSON and Markdown of the tree. |
| Metadata | None | Catalog JSON from the local tree. |
| Structural | None | Nothing. The core schemas ship in the wheel. |
| Schema | Only with `--schema-allow-network` | One schema document when the package does not include the requested version. |
| Data | Zero or more requests for each remote asset | Asset bytes, as described below. |
| Live | Two requests for each host, plus one HEAD for each distinct asset URL. With a publish base, one HEAD for each distinct link target under the base that the crawl did not fetch. | Response headers and one byte for the range check. |

Most data checks read only the required bytes at known offsets. Examples include a COG header, a Parquet footer, or one geometry column. These checks use HTTP range requests or `/vsicurl/`.

Two checks can require the complete asset. `PTL-DAT-001` recomputes the multihash, and `PTL-DAT-002` counts the bytes.

rashid reads an asset in full when it has either of these values:

- A `file:checksum` that rashid can compute.
- An integer `file:size`.

If an asset has neither value, rashid reads only the 16-byte format signature. It does not fetch the asset when no media type is available to confirm.

`PORTO-CORE-028` defines `file:size` and `file:checksum` as SHOULD requirements. A publisher can omit them when it cannot process bytes on another host and remain conformant.

When these values exist, rashid verifies them. Therefore, complete byte validation can transfer every byte of large remote assets. The metadata claims require this transfer; the reader implementation does not.

## Limit Data Checks to Local Assets

`--data-scope all` is the default. It reads local files and remote HTTPS assets.

`--data-scope local` runs every data rule against assets inside the catalog tree. It leaves remote assets untouched and reads none of their bytes.

Use `local` when you own the metadata but do not control the asset host, as with a mirror. This option preserves checks for the catalog's files. Those checks include GeoParquet ordering, row-group statistics, and item-mirror comparisons for a collection's `items.parquet`.

`--no-data` omits all those checks. The catalog in [issue #86](https://github.com/portolan-sdi/rashid/issues/86) prompted the local scope option. Its 924 items identify 1,848 remote assets and declare 1.86 TB. The operator does not control that host. Its three local files total 1.2 MB.

Combine `--data-scope local` with `--live` to recover most checks for remote assets. `PTL-LIV-002` compares each declared `file:size` with the `Content-Length` from HEAD. This check verifies every size without transferring asset bytes. Only `file:checksum` requires the object itself.

The Python API accepts a reader factory instead of a scope flag:

```python
from rashid import ValidationOptions, validate
from rashid.data.reader import LocalOnlyReader

report = validate("path/to/catalog", options=ValidationOptions(data_reader_factory=LocalOnlyReader))
```

Pass the same factory to run the data pass directly:

```python
from rashid.data import validate_data
from rashid.data.reader import LocalOnlyReader

report = validate_data(graph, reader_factory=LocalOnlyReader)
```

Two settings omit the data pass instead of limiting it:

- `--no-data` skips the pass.
- A `RulesConfig` that disables every rule from `PTL-DAT-001` through `PTL-DAT-017` skips the pass.

If you disable only some data rules, the pass still runs and suppresses only those findings.

## Development

Install the development dependencies:

```bash
uv sync
```

Install the repository hooks:

```bash
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org). The commit message hook uses commitizen to enforce the format.

## Version chains

`PTL-VER-001` checks version links on catalogs, collections, and items, even when the extension declaration is missing. `PTL-CNF-003` checks version field declarations separately.

The [Portolan versioning requirement](https://github.com/portolan-sdi/portolan-spec/blob/main/specs/portolan/core.md#conformance-and-versioning) delegates dataset versioning to the [STAC version extension](https://github.com/stac-extensions/version). Its navigation semantics follow [RFC 5829](https://www.rfc-editor.org/rfc/rfc5829.html).

The rule produces warnings for:

- A predecessor, successor, or latest link within the catalog whose target is not a readable STAC object.
- A missing reciprocal link between a predecessor and successor, unless the partner link has a target that cannot be resolved.
- A cycle, including a self predecessor or successor.
- A latest link whose target has a successor in the known version graph.
- More than one latest link on the same resource, even when the targets are not loaded.
- A relative history link in a local catalog whose target file does not exist.

A version history may be a changelog, catalog, or collection. Branches and merges are allowed. A version may link to itself as latest.

Version labels need not match IDs or dates and may repeat within a collection. Unrelated histories may have different heads. Deprecation does not change the version graph.

Resolution uses the graph's existing href policy, including absolute STAC links under the language root's published self base. The metadata rule cannot check absolute history documents or targets outside that base because it does not fetch them. If an inverse link cannot be judged, the rule leaves that pair unchecked.

History-file existence is checked only for local catalogs. A remote crawl mirror omits arbitrary history documents, so their absence from the mirror does not show a broken link.

For remote catalogs, the rule checks reciprocity, cycles, and whether latest points to a head using only targets already loaded in the graph. It enforces the limit of one latest link without fetching missing targets.

`is-version-of` and `has-version` describe an overarching resource rather than predecessor and successor edges. They do not join the version graph.

The rule cites no numbered requirement: the manifest has no entry for the delegated extension's navigation semantics. Its stable ID uses a separate metadata group and does not consume a data-pass ID.
