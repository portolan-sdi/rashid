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
