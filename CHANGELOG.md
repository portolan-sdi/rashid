## v0.5.0 (2026-11-25)

### BREAKING CHANGE

- The import name is rashid_core. Replace "from rashid import ..." with "from rashid_core import ...", and import public helpers from rashid_core.api. Depend on rashid-core for metadata validation, or rashid-core[data] for the data checks. Logger names are now rashid_core.*.

### Feat

- split rashid into rashid-core and a rashid metapackage (#341)

### Fix

- **data**: accept GDAL overview levels that round down (PTL-DAT-011) (#351)
- **rules**: read Shape, wkb_* and binary columns as possible geometry (#335)
- **rules**: clarify missing collection and forbidden member messages (#350)
- **data**: name a passing row order in the PTL-DAT-006 fix hint (#344)
- **rules**: accept RFC 3987 IRI references in link and asset hrefs (#342)

## v0.4.0 (2026-11-17)

### BREAKING CHANGE

- file:// partition globs and partition extensions other
than v1.x are now errors; PTL-PRO-004 is now a warning.
- the data pass now sends requests for a bucket partition:glob.
Use --data-scope local to keep a run offline.
- PTL-DAT-012 gives warnings instead of errors for
pre-release versions and for stale geo keys on native files, and
PTL-DAT-020 and PTL-DAT-017 report on more collections.
- catalogs checked without a publish base now get hosting
warnings instead of errors, and servers that echo a different origin now
fail PTL-LIV-003 and PTL-LIV-005.
- files under 5 MB now get an ordering verdict, and files
with three to seven row groups are judged by their rows, not their boxes.
- a catalog with a relative partition:glob now fails
PTL-PRT-002. Replace the glob with its absolute bucket URL, such as
s3://bucket/data/*.parquet.
- Replace validate(path, data=False, config=config) with validate(path, options=ValidationOptions(data=False, config=config)). Import ValidationOptions from rashid or rashid.api. The unused extensions_validator parameter is removed; use validate_extensions for its lower-level validator adapter. Version changes remain part of the Commitizen release process.

### Feat

- **schema**: bundle the Portolan v0.3.0 profile schema (#322)
- report the spec version rashid applies (#315)
- **data**: list s3/gs/az partition globs anonymously and check sampled partition files (#316)
- **zarr**: check a Zarr store against the incubating profile (#295)
- **data**: judge spatial order by pruning efficiency and report it as a warning (#174)
- **rules**: accept s3 asset hrefs that have an https alternate (#293)
- **rules**: validate classification classes on raster bands (#243)
- **schema**: map draft profile URIs and vendor release tags (#298)
- check that every table:columns entry carries a description (#221)
- **rules**: require partition globs to be listable absolute URLs (#292)
- **validation**: recommend an overview COG for collections of rasters (#291)
- **live**: require range support only for partial-read files (#290)
- **validation**: enforce the spec batch requirements (#263)
- add counters, stage timings, logging, and a profile entry point (#223)

### Fix

- **rules**: correct provenance, asset, and partition rules for spec v0.3 (#318)
- **data**: read https alternates and fix native GeoParquet, ordering, and column checks (#314)
- **live**: enforce CORS origins, scope hosts, and probe https alternates (#311)
- **rules**: exempt COG visual derivatives from PTL-VIZ-002 (#252)
- **data**: check native-geo Parquet instead of skipping it (#257)
- **data**: check column descriptions on assetless partitioned collections (#297)
- stop urllib streams at TLS EOF and compare POSIX finding paths (#274)
- open the spec-sync pull request as the org sync app (#245)
- **ci**: let the nightly mutmut sweep run the full suite (#241)
- **live**: report a redirect off https as PTL-LIV-008 and never follow it (#228)

### Refactor

- give each repeated concept one owner and guard against new copies (#272)

### Perf

- **data**: open each asset once and read assets in parallel (#230)
- load obstore, ssl, and urllib.request on the first HTTPS request (#225) (#229)
- parse JSON with orjson and fetch remote objects with obstore (#218)

## v0.3.0 (2026-10-12)

### Feat

- **check**: accept the https URL of a published catalog; rashid follows the links from the root, runs every pass over the fetched tree, and turns the live pass on with the URL as the publish base; `PTL-GEN-002` names what a crawl cannot see (#160)
- **live**: read the publish base from the root catalog's absolute `self` link when `--live-base-url` is absent, and report a `self` link that disagrees with the base as `PTL-LIV-007` (#160)
- **rules**: require every collection to refer to its primary data (`PTL-AST-007`, PORTO-CORE-082) and every item to carry a `data` asset (`PTL-AST-008`, PORTO-CORE-083)

### Perf

- **check**: index the catalog graph and screen objects with jsonschema-rs; an object that jsonschema-rs rejects still goes through jsonschema, so each finding keeps its message (#214)

## v0.2.0 (2026-10-12)

### Feat

- **validation**: validate objects against declared STAC extension schemas (#159)
- **live**: probe every link target under `--live-base-url` and report a document the publish host does not serve as `PTL-LIV-006` (#189)

### Fix

- **deps**: lock urllib3 2.8.0 and virtualenv 21.14.5 (#211)

## v0.1.8 (2026-08-28)

### Feat

- **schema**: bundle the portolan-spec v0.2.0 profile schema
- **rules**: drop PTL-LNK-004 and resolve absolute links through the root self base
- **rules**: drop PTL-LNK-005, the self-link rule

### Fix

- **data**: read PTL-DAT-006 from GeoParquet footers (#167)
- **rules**: replace an assert with a guard for the bandit gate

## v0.1.7 (2026-08-20)

### Feat

- catch up to portolan-spec v0.1.2
- **rules**: flag a collection whose scenes or mirror imply items it never published
- accept a catalog.json path where a catalog directory is expected
- accept a catalog that carries alternate-language trees
- report the fan-out on collections as well as catalogs
- warn when a catalog leaves twenty or more children ungrouped

### Fix

- **data**: run GeoParquet byte checks over partition files
- **ci**: restore the moving tag on repo-checks

## v0.1.6 (2026-08-14)

### Feat

- catch up to portolan-spec v0.1.1

## v0.1.5 (2026-08-13)

### Feat

- **rules**: report undecidable geospatial nature instead of skipping
- flag extensions pinned behind the profile registry
- enforce the catalog logo convention

### Fix

- report invalid COG types without hiding raster rules

## v0.1.4 (2026-08-12)

### Feat

- **api**: publish the SPDX identifier list PTL-LIC-001 validates against
- **data**: apply the ordering criteria only at five or more row groups
- **live**: probe the full CORS header set browsers need
- **rules**: downgrade PTL-AST-003 to a warning
- **rules**: accept image/webp thumbnails in PTL-VIZ-001
- **rules**: add PTL-VIZ-006 for the default-style key (PORTO-CORE-070)
- **data**: add --data-scope so a metadata-only mirror can be validated

### Fix

- scope the source carve-out to formats, and drop the alternate role
- **data**: raise the locality limit to 30% and sort fixtures by Hilbert
- **data**: judge row ordering at every row-group count
- skip the wheel test when it would build a partial tree
- point the fixture vendor at the renamed spec catalog directory
- **ci**: run the checks on every pull request, not only those onto main

### Refactor

- **rules**: PTL-VIZ-006 reads the `default` role, not the asset key

## v0.1.3 (2026-07-29)

### Feat

- summarize findings by rule when a run produces too many

### Fix

- raise instead of assert when building the check registry
- **data**: measure the skip rate the locality criterion names
- **data**: judge spatial ordering on files written as one row group
- **rules**: resolve a collection's items across organizing catalogs
- **schema**: send the named User-Agent when fetching a remote schema
- **data**: compare a mirror's rows to the items, not only their ids
- **rules**: accept an item collection link across an organizing catalog
- **data**: point PTL-DAT-005 at the proj:epsg a collection declares
- **data**: send the same named User-Agent from the byte reader
- **live**: send a named User-Agent on every probe
- **rules**: accept extra describedby and agents links

### Refactor

- **data**: compare mirror geometries through shapely

## v0.1.2 (2026-07-28)

### Feat

- **api**: publish the helpers downstream tools were importing privately

## v0.1.1 (2026-07-28)

### Feat

- **data**: run the data pass by default
- **rules**: give every finding a fix hint and structured values
- **live**: probe relative hrefs against a publish base URL
- carry structured expected/actual values on findings
- validate STAC structure offline from vendored schemas, drop stac-valid
- bundle the profile schema and validate offline by default
- ship the py.typed marker in the wheel

### Fix

- lower the pyarrow floor to 21 so consumers capped below 25 can install

## v0.1.0 (2026-07-27)

### Feat

- **mirror**: follow the stac-geoparquet asset convention, keep the storage rules
- **rollup**: enforce the items.parquet rollup with PTL-ROL and PTL-DAT-016
- **rules**: enforce raster scene-as-item modeling with PTL-COL-004
- **spec**: map every check to the requirements manifest and gate coverage
- **scripts**: vendor the spec's requirements manifest as a fixture
- **data**: align the data pass with the spec's format MUSTs
- **rules**: align the metadata pass with the spec's structure MUSTs
- **rules**: add PTL-COL-001 for single-file collections
- require internal overviews on rasters larger than one tile
- read native GeospatialStatistics; enforce raster valid percent
- probe hosting servers with an opt-in live pass
- enforce cloud-native storage MUSTs in the data pass
- verify asset bytes with an opt-in data pass
- vendor spec reference catalog with spec-drift guards
- add Portolan profile schema validation pass
- add STAC structural pass and sync rules with spec v0.1 profile

### Fix

- **data**: accept asset-level table:columns in the tabular check
- **data**: mark the partition-skip continue for bandit
- **data**: treat a raster grid as a container, not a tight envelope
- **data**: bound reprojected bboxes instead of sampling corners
- skip networked tests when the transport drops
- exempt source assets from live probes; check every declared size
- scope data-pass format MUSTs to primary GeoParquet and rasters
- satisfy bandit in the asset reader
- track spec migration to schemas.portolan-sdi.org/portolan schema URI
- point schema URI at canonical schema.portolan-sdi.org host

### Refactor

- rename the package from reis to rashid
