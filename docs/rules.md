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
- `PTL-VIZ-004` produces an info finding when a large vector lacks a visual derivative. The render-path MUST depends on whether source rendering is viable, which metadata cannot prove. The 100 MB threshold is also heuristic.

`PTL-CNF-004` reads `src/rashid/_schemas/extension-registry.json`. This file contains the profile's registry table from `stac/README.md` in the specification.

At vendor time, `scripts/vendor_spec_fixtures.py` parses that table. The rule ignores extensions that the registry does not list. `PTL-CNF-001` and `PTL-CNF-002` handle the Portolan schema URI's version.

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

`PTL-VIZ-001` reports when metadata cannot identify a collection as geospatial or tabular. The specification identifies tabular data through a Parquet geometry column, which requires the data pass.

If the collection has no thumbnail, the rule produces a warning and names the missing signals. The requirement is a MUST, so silence could imply that the collection passed. A collection with a thumbnail meets the requirement either way and produces no finding.

`PTL-VIZ-004` reports the same uncertainty at info severity when its own recommendation would otherwise apply.

The following metadata signals identify a geospatial collection:

- An item geometry.
- A spatial media type: PMTiles, COG, or COPC.
- A geometry column in `table:columns` on the collection or any non-`source` asset.

A declared column list without a geometry column identifies a tabular collection. The collection is then exempt from the thumbnail requirement.

## Rule Groups

| Group | Rules | Checks |
|---|---|---|
| `PTL-GEN` | 000–001 | Requires a root `catalog.json` and parseable object files. |
| `PTL-LNK` | 001–010 | Checks required structural links, child and item completeness, link types, relative links, and resolved target objects. Prohibits self links. Requires a `rel:"icon"` logo to have a renderable media type, title, and relative `href`. Keeps `child` and `item` links out of alternate-language trees. |
| `PTL-TTL` | 001–003 | Requires non-empty titles and descriptions, human-readable titles, and titles on child and item links. |
| `PTL-BBX` | 001 | Requires finite, sentinel-free WGS84 bounding boxes with south less than or equal to north. Supports 2D and 3D boxes. |
| `PTL-TMP` | 001–002 | Requires an item datetime or interval, RFC 3339 values, and a start before or equal to the end. |
| `PTL-PRV` | 001–003 | Requires at least one producer and exactly one host. The host must appear last and include a URL or email address. |
| `PTL-LIC` | 001–003 | Requires an SPDX identifier or `other`, a license link for `other`, and no `proprietary` value. |
| `PTL-FIL` | 001–005 | Requires `AGENTS.md` and `README.md` on disk. Requires Markdown links with `rel:"agents"` and `rel:"describedby"`. Requires a non-empty README with a title heading. Warns heuristically when a collection README omits license or provenance information. |
| `PTL-AST` | 001–006 | Checks asset `href`, media type, and roles. Requires the COG media type for primary raster data and HTTPS instead of S3 URLs. Requires `file:size` and multihash-encoded `file:checksum`; their absence produces a warning under the SHOULD requirement. Prohibits assets on catalogs. |
| `PTL-CNF` | 001–004 | Requires a versioned Portolan schema URI that agrees with the root catalog. Requires the STAC version extension for dataset versioning. Requires each registered extension to use the version in the profile's extension registry. |
| `PTL-PRO` | 001–004 | Checks mirror `via` and `canonical` links plus the `updated` synchronization time. Prohibits upstream links on official catalogs. |
| `PTL-VIZ` | 001–006 | Requires thumbnails on geospatial collections, style assets for visual derivatives, and `rel:"pmtiles"` registration for PMTiles. Reports large vectors without a visual derivative. Requires the MapLibre style media type in PMTiles collections. Requires the `default` role on one of several styles. |
| `PTL-STR` | 000–001 | Checks STAC 1.1.0 core structure offline with the included core schemas. Rule `000` warns when the pass cannot run. |
| `PTL-DAT` | 000–016 | Compares asset bytes with metadata and format MUST requirements. Checks `file:checksum`, `file:size`, format signatures, bounding boxes, and coordinate reference systems. Requires valid COGs with internal overviews, embedded band statistics, and valid percentages for nodata bands. Requires GeoParquet 1.1 or 2.x, spatial ordering, per-row-group statistics, and suitable row-group size. Statistics can use a bounding-box covering column or native 2.x `GeospatialStatistics`. Requires square internal tiles no larger than 512 pixels. Requires one Parquet schema across local partition files. Applies the tabular SHOULD requirements for `table:columns` and `extent.temporal` to plain-Parquet collection assets with temporal columns. Compares an item mirror's row count, IDs, geometry, datetime, and bounding box with the collection's items. Rule `000` warns when the geospatial stack cannot load. Source and alternate assets are exempt from format MUST requirements. Plain Parquet is exempt from GeoParquet rules. Those rules apply to an item mirror like any other spatial table. |
| `PTL-LIV` | 000–005 | Checks the hosting server's Data Storage MUST requirements for absolute HTTPS asset URLs. For each host, checks ranged GET support through `206` and `Accept-Ranges: bytes`, `Content-Length` on HEAD, CORS origins, exposed headers, and preflight support. Preflight must allow `GET`, `HEAD`, `Range`, and conditional-request headers. HEAD size must match `file:size`. Source and alternate assets are exempt. Rule `000` warns when no asset is available to probe or a host is unavailable. |
| `PTL-PRT` | 001 | Requires a partitioned collection to declare the partition extension and provide `partition:scheme`, `partition:keys`, and `partition:glob`. |
| `PTL-COL` | 001–004 | Requires a single-file collection to expose data through a collection asset instead of one item. Prohibits nested collections. Requires collection IDs to follow the naming convention and remain unique within their language tree. Requires raster scenes to appear on items instead of the collection. |
| `PTL-CAT` | 001 | Recommends subcatalogs when a catalog or collection has at least 20 ungrouped collections or items. Subcatalogs do not count as ungrouped children. |
| `PTL-MIR` | 001–002 | Recommends an `items.parquet` mirror for a raster collection with scene items. Requires a published mirror to be a collection asset with the `collection-mirror` role and `application/vnd.apache.parquet` media type, as the stac-geoparquet specification defines. |

`rashid.registry.CHECKS` provides the same list in machine-readable form. It maps each check ID to its description, highest possible severity, and specification requirements.

The per-rule summary and JSON `summary.by_rule` object read this registry. Call `rashid.describe(rule_id)` to get a rule's description.

The catalog tree loads from a local directory. `CatalogGraph` in `src/rashid/catalog.py` provides the only metadata I/O layer and loads the tree in one pass. This design allows a future HTTP catalog loader. The data pass already reads asset bytes through HTTPS.

## Network Cost

Only the data and live passes use the network by default. Only the data pass transfers asset bytes.

| Pass | Requests | Data transferred |
|---|---|---|
| Metadata | None | Catalog JSON from the local tree. |
| Structural | None | Nothing. The core schemas ship in the wheel. |
| Schema | Only with `--schema-allow-network` | One schema document when the package does not include the requested version. |
| Data | Zero or more requests for each remote asset | Asset bytes, as described below. |
| Live | Two requests for each host, plus one HEAD for each distinct asset URL | Response headers and one byte for the range check. |

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
from rashid import validate
from rashid.data.reader import LocalOnlyReader

report = validate("path/to/catalog", data_reader_factory=LocalOnlyReader)
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
