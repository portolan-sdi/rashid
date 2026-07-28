# Rules

Reference for every rule rashid applies. The README covers what each
validation pass does; this file lists the rules themselves.

Findings carry a stable rule id (`PTL-<GROUP>-<NNN>`), a severity, a message, and the offending file path. Severities follow the spec: MUST maps to `error`, SHOULD to `warning`, with three deliberate exceptions:

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
| `PTL-AST` | 001–005 | asset href/type/roles, https-not-s3, `file:size`, multihash `file:checksum`; catalogs carry no assets |
| `PTL-CNF` | 001–003 | versioned Portolan schema URI declared, consistent with the root; dataset versioning declares the STAC version extension |
| `PTL-PRO` | 001–004 | mirror `via`/`canonical` links and `updated` sync time; officials carry no upstream links |
| `PTL-VIZ` | 001–005 | thumbnail on geospatial collections, style assets for visual derivatives, PMTiles `rel:"pmtiles"` registration, large-vector-without-visual nudge, MapLibre style media type in PMTiles collections |
| `PTL-STR` | 000–001 | STAC 1.1.0 core structural validity, offline against the vendored core schemas; `000` warns when the pass could not run |
| `PTL-DAT` | 000–016 | asset bytes vs metadata and the format MUSTs: `file:checksum`, `file:size`, format magic, bbox/CRS; valid COG with internal overviews, embedded band statistics, and valid percent (a MUST on nodata bands); GeoParquet version (1.1/2.x), spatial ordering, per-row-group statistics (bbox covering column or native 2.x `GeospatialStatistics`), and row-group size; square internal tiles within 512px; a single Parquet schema across local partition files; the tabular SHOULDs (`table:columns`, `extent.temporal` when the file carries a temporal column) on plain-Parquet collection assets; an item mirror whose ids diverge from the collection's items; `000` warns when the `rashid[data]` extra is absent. Source/alternate assets are exempt from the format MUSTs; plain (non-geo) Parquet is exempt from the GeoParquet rules, which bind an item mirror like any other spatial table |
| `PTL-LIV` | 000–005 | the hosting server's Data Storage MUSTs, probed per host over absolute `https` asset hrefs: ranged GET honored (`206`, `Accept-Ranges: bytes`), HEAD `Content-Length` present and matching `file:size`, CORS origin allowed, required headers exposed, preflight accepting `GET`/`HEAD` with `Range`; source/alternate assets are exempt; `000` warns when nothing is probeable or a host is unreachable |
| `PTL-PRT` | 001 | a partitioned collection declares the partition extension and carries `partition:scheme`, `partition:keys`, and `partition:glob` |
| `PTL-COL` | 001–004 | a single-file collection exposes its data as a collection-level asset, not wrapped in a lone item; collections never nest; collection IDs follow the naming convention and are unique; raster scenes sit on items rather than on the collection |
| `PTL-MIR` | 001–002 | a raster collection with scene items publishes an `items.parquet` mirror (a warning, per the SHOULD), and a published mirror is registered as a collection-level asset with the `collection-mirror` role and type `application/vnd.apache.parquet`, per the stac-geoparquet spec |

The catalog tree is loaded from a local directory. `CatalogGraph` (`src/rashid/catalog.py`) is the single I/O layer for the metadata, loaded in one pass, so a remote (HTTP) catalog loader can slot in later; the data pass already reads asset bytes over `https`.

## Development

```bash
uv sync                       # install (the dev dependency group is included)
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push        # wire the quality gates
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org) — enforced by commitizen on the `commit-msg` hook.
