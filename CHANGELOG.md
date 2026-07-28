## v0.2.0 (2026-07-28)

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
