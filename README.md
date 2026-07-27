# reis

A validator and linter for [Portolan](https://www.portolan-sdi.org/) catalogs. Named after [Piri Reis](https://www.unesco.org/en/memory-world/piri-reis-world-map-1513).

Portolan conformance is defined by passing this validator. reis implements four of the spec's separable passes:

- the **metadata pass** — every requirement in the [Portolan spec](https://github.com/portolan-sdi/portolan-spec) checkable from the catalog's JSON metadata alone, without reading asset bytes;
- the **structural pass** — STAC 1.1.0 core validity, delegated to [`stac-validator`](https://github.com/stac-utils/stac-validator) (the maintained `stac-valid` distribution). This validates each object against the STAC 1.1.0 *core* schema only; the extensions an object declares — including the Portolan profile — are the metadata pass's domain, so a not-yet-published extension schema never breaks it.
- the **schema pass** — the published [Portolan profile schema](https://schema.portolan-sdi.org/v0.1.0/schema.json) applied directly to every object, which the spec calls the machine-checkable core of the metadata pass. reis implements those requirements by hand (for precise messages and fix hints), so this pass overlaps them by design: it is an authoritative cross-check that catches drift or gaps between the hand rules and the canonical schema. It is therefore **opt-in** (`--schema`), and a defect both catch is reported twice — once by a metadata rule, once by the schema.
- the **data pass** — reads each asset's bytes, local files and remote `https` URLs alike, and checks them against the declared metadata and the format MUSTs: `file:checksum` and `file:size` recomputed from the bytes, the media type confirmed by magic number, and the data's own bbox/CRS checked against the object's. It also enforces the cloud-native storage MUSTs a metadata reader cannot see — a raster is a valid COG carrying internal overviews (when larger than its own internal tile) and embedded band statistics (with valid percent where the band has a nodata value), and a GeoParquet is spatially ordered with per-row-group spatial statistics (a 1.1 `bbox` covering column, or native 2.x `GeospatialStatistics`) and row groups under 150,000 rows. It is **opt-in** (`--data`) because it reaches the network and needs the geospatial stack in the `reis[data]` extra; without that extra it emits one `PTL-DAT-000` warning and moves on. Because these storage MUSTs are stricter than what current tooling emits, a real catalog can fail the data pass on rules its metadata satisfies.
- the **live-hosting pass** — probes the servers behind remote `https` assets for the spec's Data Storage MUSTs, which are properties of the server, not of any file: HTTP range support (`206 Partial Content`, `Accept-Ranges: bytes`, an accurate `Content-Length` on HEAD) and CORS for browser readers (a read-permitting `Access-Control-Allow-Origin`, the required response headers exposed, a preflight accepting `GET`/`HEAD` with `Range`). Range and CORS semantics are probed once per distinct host; only a cheap HEAD runs per asset. It is **opt-in** (`--live`), reaches the network, and is stdlib-only (no extra needed).

## Install

```bash
uv tool install reis
# with the data pass (pyarrow, rasterio, rio-cogeo, pyproj):
uv tool install "reis[data]"
```

## CLI

```bash
reis check path/to/catalog
reis check --json path/to/catalog
reis check --no-structural path/to/catalog
reis check --schema path/to/catalog
reis check --data path/to/catalog
reis check --live path/to/catalog
```

Exit code 0 when the catalog passes (no errors; warnings and infos allowed), 1 when errors were found.

`reis check` runs the metadata and structural passes by default. The structural pass fetches the STAC core schemas from `schemas.stacspec.org` (cached in-process); when it cannot reach them it emits a single `PTL-STR-000` warning rather than failing, so the offline metadata findings still surface. Pass `--no-structural` to skip it and run the metadata pass alone.

`--schema` additionally runs the schema pass, fetching the Portolan profile schema from the URI the root catalog declares (falling back to `schema.portolan-sdi.org`) and validating every object against it. Like the structural pass it degrades to a single `PTL-SCH-000` warning when the schema is unreachable. It is off by default because it overlaps the metadata pass; turn it on to cross-check reis's hand rules against the canonical schema.

`--data` additionally runs the data pass, reading each asset's bytes to verify the declared `file:checksum`, `file:size`, media type, and bbox/CRS, plus the cloud-native storage MUSTs: a valid COG with embedded band statistics, and a spatially ordered GeoParquet with per-row-group spatial statistics and bounded row groups. Relative hrefs resolve against the catalog tree; absolute `https` hrefs are fetched (checksum over a whole-object read, headers over range requests). It needs the `reis[data]` extra and degrades to a single `PTL-DAT-000` warning when the extra is absent; assets it cannot reach (`s3`, a missing local file) are skipped rather than failed.

`--live` additionally runs the live-hosting pass, probing the servers behind absolute `https` asset hrefs for the Data Storage MUSTs: a ranged `GET` (sent with an `Origin` header, since servers omit every `Access-Control-*` header without one) checks `206`/`Accept-Ranges` and the simple-response CORS headers, an `OPTIONS` preflight checks the allowed methods and request headers, and a `HEAD` per asset checks `Content-Length` (against the declared `file:size` when present). Range and CORS are server properties, so those probes run once per distinct host. Source/alternate assets are exempt, as in the data pass: a retained original lives on a server the publisher does not control, and the hosting MUSTs bind the servers hosting the cloud-native primaries. Relative hrefs are not probeable from a local tree (a `--base-url` mapping is a planned follow-up); when nothing is probeable, or a host is unreachable, the pass degrades to `PTL-LIV-000` warnings.

## Library

```python
from reis import validate

report = validate("path/to/catalog")
report.passed  # no ERROR findings
for finding in report.findings:
    print(finding.rule_id, finding.severity.value, finding.path, finding.message)
```

`validate` runs the metadata pass only. Add `structural=True` to also run the STAC structural pass, `schema=True` for the Portolan profile schema pass, `data=True` for the data pass, or `live=True` for the live-hosting pass (all reach the network; `data=True` also needs the `reis[data]` extra). The CLI turns `structural` on by default:

```python
report = validate("path/to/catalog", structural=True, schema=True, data=True, live=True)
```

Rules can be disabled or re-severitied:

```python
from reis import RulesConfig, Severity, validate

config = RulesConfig(
    disabled=frozenset({"PTL-PRO-002"}),
    severity_overrides={"PTL-TTL-002": Severity.ERROR},
)
report = validate("path/to/catalog", config=config)
```

## Rules

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
| `PTL-STR` | 000–001 | STAC 1.1.0 core structural validity (`stac-validator`); `000` warns when the pass could not run |
| `PTL-DAT` | 000–016 | asset bytes vs metadata and the format MUSTs: `file:checksum`, `file:size`, format magic, bbox/CRS; valid COG with internal overviews, embedded band statistics, and valid percent (a MUST on nodata bands); GeoParquet version (1.1/2.x), spatial ordering, per-row-group statistics (bbox covering column or native 2.x `GeospatialStatistics`), and row-group size; square internal tiles within 512px; a single Parquet schema across local partition files; the tabular SHOULDs (`table:columns`, `extent.temporal` when the file carries a temporal column) on plain-Parquet collection assets; an item mirror whose ids diverge from the collection's items; `000` warns when the `reis[data]` extra is absent. Source/alternate assets are exempt from the format MUSTs; plain (non-geo) Parquet is exempt from the GeoParquet rules, which bind an item mirror like any other spatial table |
| `PTL-LIV` | 000–005 | the hosting server's Data Storage MUSTs, probed per host over absolute `https` asset hrefs: ranged GET honored (`206`, `Accept-Ranges: bytes`), HEAD `Content-Length` present and matching `file:size`, CORS origin allowed, required headers exposed, preflight accepting `GET`/`HEAD` with `Range`; source/alternate assets are exempt; `000` warns when nothing is probeable or a host is unreachable |
| `PTL-PRT` | 001 | a partitioned collection declares the partition extension and carries `partition:scheme`, `partition:keys`, and `partition:glob` |
| `PTL-COL` | 001–004 | a single-file collection exposes its data as a collection-level asset, not wrapped in a lone item; collections never nest; collection IDs follow the naming convention and are unique; raster scenes sit on items rather than on the collection |
| `PTL-MIR` | 001–002 | a raster collection with scene items publishes an `items.parquet` mirror (a warning, per the SHOULD), and a published mirror is registered as a collection-level asset with the `collection-mirror` role and type `application/vnd.apache.parquet`, per the stac-geoparquet spec |

The catalog tree is loaded from a local directory. `CatalogGraph` (`src/reis/catalog.py`) is the single I/O layer for the metadata, loaded in one pass, so a remote (HTTP) catalog loader can slot in later; the data pass already reads asset bytes over `https`.

## Development

```bash
uv sync                       # install (the dev dependency group is included)
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push        # wire the quality gates
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org) — enforced by commitizen on the `commit-msg` hook.

The gates run in two stages, both reproduced by CI and runnable locally:

```bash
# fast gate (every commit): ruff, ruff-format, codespell,
# actionlint, zizmor, file hygiene
uv run pre-commit run --all-files

# full gate (every push): deptry, mypy (strict), vulture,
# xenon (complexity), import-linter
uv run pre-commit run --all-files --hook-stage pre-push
```

Tests carry a 90% coverage floor:

```bash
uv run pytest                 # full suite
uv run pytest -n auto         # parallelised, as CI runs it
uv run pytest -m unit         # fast, isolated tests only
```

Markers are `unit`, `integration`, and `network`. The `network` tests drive the real `stac-validator` against `schemas.stacspec.org` and self-skip when offline. The pre-push hook also runs the fast tests when `ENABLE_PRE_PUSH_TESTS=1` is set.

Mutation testing (run nightly in CI):

```bash
uv run mutmut run
```
