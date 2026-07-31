# Rules

Reference for every rule rashid applies. The README covers what each
validation pass does; this file lists the rules themselves.

Findings carry a stable rule id (`PTL-<GROUP>-<NNN>`), a severity, a message, and the offending file path — plus, where applicable, a `json_pointer` locating the defect inside the file, an imperative `fix_hint` naming the repair, and structured `expected`/`actual` values when the rule compares two concrete things (say, a declared checksum against the recomputed one). Every ERROR-severity metadata finding carries a hint and, where a JSON location exists, a pointer; a coverage test enforces this. The combination is designed for an agent iterating a fix loop: check, apply the hints, re-check. Severities follow the spec: MUST maps to `error`, SHOULD to `warning`, with three deliberate exceptions:

- `PTL-CNF-002` (schema URI differs from the root catalog's) is a **warning** — the spec's explicit exception; a mixed-version catalog remains valid.
- `PTL-TTL-002` (title looks machine-generated) is a **warning** by default because it is heuristic; promote it with a severity override.
- `PTL-PRO-002` (mirror without a `canonical` link) is **info**, since whether the upstream publishes STAC is unknowable from metadata.
- `PTL-VIZ-004` (large vector without a visual derivative) is **info**: the spec's render-path MUST hinges on whether render-from-source is viable, which metadata cannot prove; the size threshold (100 MB) is heuristic.
- `PTL-COL-001` fires only on the exact shape the spec rules out: one item, one data asset on it, and no data asset on the collection. Several items, several data files, or a partitioned collection are all legitimate, and a collection whose assets omit `roles` is undecidable, so `PTL-AST-001` reports it instead.
- `PTL-VIZ-001` skips collections whose geospatial-vs-tabular nature is undecidable from metadata (the spec identifies tabular by the Parquet's geometry column — a data-pass fact); positive signals are item geometries, a `table:columns` geometry column, or spatial media types.

| Group | Rules | Checks |
|-------|-------|--------|
| `PTL-GEN` | 000–001 | root catalog.json present, every object file parseable |
| `PTL-LNK` | 001–006 | required structural links, child/item completeness, link types, relative-only, no self link, links resolve to the correct object |
| `PTL-TTL` | 001–003 | non-empty title/description, human-readable titles, titled child/item links |
| `PTL-BBX` | 001 | finite, sentinel-free WGS84 bboxes with south ≤ north (2D and 3D) |
| `PTL-TMP` | 001–002 | item datetime or interval present; RFC 3339, start ≤ end |
| `PTL-PRV` | 001–003 | ≥1 producer, exactly one host listed last, host url-or-email |
| `PTL-LIC` | 001–003 | SPDX or `other`, license link for `other`, no `proprietary` |
| `PTL-FIL` | 001–005 | AGENTS.md and README.md on disk, `rel:"agents"` and `rel:"describedby"` markdown links; README.md non-empty with a title heading, and (heuristically, a warning) mentioning license and provenance on collections |
| `PTL-AST` | 001–005 | asset href/type/roles, https-not-s3, `file:size` and `file:checksum` present (a warning, per the SHOULD) with the checksum multihash-encoded; catalogs carry no assets |
| `PTL-CNF` | 001–003 | versioned Portolan schema URI declared, consistent with the root; dataset versioning declares the STAC version extension |
| `PTL-PRO` | 001–004 | mirror `via`/`canonical` links and `updated` sync time; officials carry no upstream links |
| `PTL-VIZ` | 001–005 | thumbnail on geospatial collections, style assets for visual derivatives, PMTiles `rel:"pmtiles"` registration, large-vector-without-visual nudge, MapLibre style media type in PMTiles collections |
| `PTL-STR` | 000–001 | STAC 1.1.0 core structural validity, offline against the vendored core schemas; `000` warns when the pass could not run |
| `PTL-DAT` | 000–016 | asset bytes vs metadata and the format MUSTs: `file:checksum`, `file:size`, format magic, bbox/CRS; valid COG with internal overviews, embedded band statistics, and valid percent (a MUST on nodata bands); GeoParquet version (1.1/2.x), spatial ordering, per-row-group statistics (bbox covering column or native 2.x `GeospatialStatistics`), and row-group size; square internal tiles within 512px; a single Parquet schema across local partition files; the tabular SHOULDs (`table:columns`, `extent.temporal` when the file carries a temporal column) on plain-Parquet collection assets; an item mirror whose row count, ids, geometry, datetime, or bbox diverge from the collection's items; `000` warns when the geospatial stack cannot import. Source/alternate assets are exempt from the format MUSTs; plain (non-geo) Parquet is exempt from the GeoParquet rules, which bind an item mirror like any other spatial table |
| `PTL-LIV` | 000–005 | the hosting server's Data Storage MUSTs, probed per host over absolute `https` asset hrefs: ranged GET honored (`206`, `Accept-Ranges: bytes`), HEAD `Content-Length` present and matching `file:size`, CORS origin allowed, required headers exposed, preflight accepting `GET`/`HEAD` with `Range`; source/alternate assets are exempt; `000` warns when nothing is probeable or a host is unreachable |
| `PTL-PRT` | 001 | a partitioned collection declares the partition extension and carries `partition:scheme`, `partition:keys`, and `partition:glob` |
| `PTL-COL` | 001–004 | a single-file collection exposes its data as a collection-level asset, not wrapped in a lone item; collections never nest; collection IDs follow the naming convention and are unique; raster scenes sit on items rather than on the collection |
| `PTL-MIR` | 001–002 | a raster collection with scene items publishes an `items.parquet` mirror (a warning, per the SHOULD), and a published mirror is registered as a collection-level asset with the `collection-mirror` role and type `application/vnd.apache.parquet`, per the stac-geoparquet spec |

`rashid.registry.CHECKS` is the same list in machine-readable form: every check id mapped to its description, the strongest severity it can emit, and the spec requirements it enforces. The per-rule summary and the JSON `summary.by_rule` block read from it, and `rashid.describe(rule_id)` returns one description.

The catalog tree is loaded from a local directory. `CatalogGraph` (`src/rashid/catalog.py`) is the single I/O layer for the metadata, loaded in one pass, so a remote (HTTP) catalog loader can slot in later; the data pass already reads asset bytes over `https`.

## Network cost

The data and live passes are the only ones that reach the network by default, and the data pass is the only one that transfers asset bytes.

| Pass | Requests | What it transfers |
|------|----------|-------------------|
| Metadata | none | the catalog's JSON, read from the local tree |
| Structural | none | nothing, the core schemas ship in the wheel |
| Schema | only under `--schema-allow-network` | one schema document, when the build does not bundle that version |
| Data | zero or more per remote asset | asset bytes, as below |
| Live | two per host, plus one HEAD per distinct asset URL | response headers, and one byte for the range probe |

Most data checks read only what they need at a known offset, a COG header, a Parquet footer, or one geometry column, over HTTP range requests or `/vsicurl/`. Two cannot. `PTL-DAT-001` recomputes the multihash and `PTL-DAT-002` counts the bytes, so an asset carrying a `file:checksum` rashid can compute, or an integer `file:size`, is read in full. An asset carrying neither is read only as far as the 16 bytes of format magic, and is not fetched at all when it declares no media type to confirm.

`PORTO-CORE-028` makes `file:size` and `file:checksum` a SHOULD, so a publisher who cannot compute them over bytes it does not host may omit them and stay conformant. Where they are present rashid verifies them, which is why a fully-populated catalog of large remote assets cannot be byte-verified without transferring all of it. That follows from what the metadata claims, not from how rashid reads.

### `--data-scope`

`--data-scope all`, the default, reads local files and remote `https` assets alike. `--data-scope local` runs every data rule against the assets that live inside the catalog tree and leaves remote assets untouched, reading none of their bytes.

Use `local` when the metadata is yours and the assets sit on a host you do not control, which is the shape of a mirror. It keeps the checks that bind the catalog's own files, such as GeoParquet ordering, row-group statistics, and the item-mirror comparison on a collection's `items.parquet`, all of which `--no-data` drops along with everything else. The catalog in [issue #86](https://github.com/portolan-sdi/rashid/issues/86) is the case that prompted it. Its 924 Items name 1848 remote assets on a host the operator does not run, declaring 1.86 TB between them, against three local files totalling 1.2 MB.

Pairing it with `--live` recovers most of what the narrowed scope gives up on remote assets. `PTL-LIV-002` compares each one's declared `file:size` against the `Content-Length` its host returns on HEAD, which settles every size without transferring a byte. Only `file:checksum` still needs the object itself.

The library takes a reader factory rather than a flag. `validate(path, data_reader_factory=LocalOnlyReader)` is the equivalent, using `LocalOnlyReader` from `rashid.data.reader`, and `validate_data(graph, reader_factory=LocalOnlyReader)` scopes the pass on its own.

Two settings drop the pass rather than narrowing it. `--no-data` skips it, and so does disabling every rule from `PTL-DAT-001` to `PTL-DAT-016` through `RulesConfig`. Disabling any subset only silences those findings, after the pass has run.

## Development

```bash
uv sync                       # install (the dev dependency group is included)
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push        # wire the quality gates
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org) — enforced by commitizen on the `commit-msg` hook.
