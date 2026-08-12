# rashid

[![PyPI](https://img.shields.io/pypi/v/rashid.svg)](https://pypi.org/project/rashid/)
[![Python versions](https://img.shields.io/pypi/pyversions/rashid.svg)](https://pypi.org/project/rashid/)
[![CI](https://github.com/portolan-sdi/rashid/actions/workflows/ci.yml/badge.svg)](https://github.com/portolan-sdi/rashid/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Validator and linter for [Portolan](https://www.portolan-sdi.org/) catalogs. The name comes from the Arabic root ر ش د, whose participles رَاشِدٌ and رَشِيدٌ mean ["taking, or following, a right way or course or direction"](https://arabiclexicon.hawramani.com/?p=5472#205af1).

rashid reads a catalog directory and reports every rule it breaks.

## Install

```bash
uv tool install rashid
```

## Check a catalog

```bash
rashid check path/to/catalog
```

An exit code of 0 means the catalog broke no MUST. An exit code of 1 means it broke at least one.

The default run covers the metadata, structural, and data passes, all offline for local assets. Two passes stay opt-in: `--schema` (redundant with the hand rules by construction; useful as a cross-check on catalogs from other tooling) and `--live` (probes the hosting servers, so it needs the network and a published catalog).

```bash
rashid check path/to/catalog --live --live-base-url https://data.example.org/my-catalog/
```

`--no-data` skips byte verification when only the metadata verdict is needed. `--data-scope local` keeps every data rule and reads only the assets inside the catalog tree, which bounds the transfer when the remote assets are large. Add `--json` for a machine-readable report.

### Reading a large report

A rule broken once per asset produces one finding per asset, so a catalog of a few hundred files can break one rule ten thousand times. Past 50 findings rashid counts each rule instead of listing it, and quotes a few examples:

```
1 error(s), 60 warning(s), 0 info(s) across 62 files.
Too many to list; showing a summary (--all to list every finding).

error    PTL-LIC-003   1x  the deprecated license value 'proprietary' must not be used
    parks/collection.json: license 'proprietary' is deprecated and must not be used

warning  PTL-TTL-002  60x  titles must be human-readable, not raw slugs or ns:LayerName identifiers
    roads_0/collection.json: title 'road_centerlines_0' looks like a raw slug, not a human-readable title
    roads_1/collection.json: title 'road_centerlines_1' looks like a raw slug, not a human-readable title
    roads_10/collection.json: title 'road_centerlines_10' looks like a raw slug, not a human-readable title
    ... 57 more in 60 files
```

`--all` lists every finding however many there are, and `--summary` counts them however few. `--json` reports both: a `summary.by_rule` block alongside the complete `findings` array.

## What it checks

Validation runs as five separable passes.

**Metadata.** Every spec requirement rashid can check from the catalog's JSON, without reading asset bytes.

**Structural.** STAC 1.1.0 core validity, checked offline against the schemas vendored in the wheel. Only the core schema applies. Declared extensions belong to the metadata pass, which keeps an unpublished extension schema from failing the tree.

**Schema.** The published [Portolan profile schema](https://schemas.portolan-sdi.org/portolan/), applied to every object. rashid also implements those requirements in code, which yields precise messages and fix hints. Running both catches drift between them, at the cost of reporting some defects twice. Opt in with `--schema`.

**Data.** Asset bytes, local files and remote https URLs alike, checked against the declared metadata and the format rules. rashid recomputes checksum and size, confirms media type by magic number, and enforces storage rules a metadata reader cannot see. Expect a real catalog to fail here on rules its metadata satisfies, because this pass is stricter than what current tooling emits. On by default. `--no-data` skips it, and `--data-scope local` narrows it to the catalog tree. [docs/rules.md](docs/rules.md) records what each pass transfers.

**Live hosting.** The servers behind remote https assets, probed for HTTP range support and CORS. Those are properties of the server rather than of any file. Range and CORS cost one probe per host, and each asset costs one HEAD. Opt in with `--live`.

A pass that cannot run reports a warning rather than passing quietly.

## Use it as a library

```python
from rashid import validate

report = validate("path/to/catalog")
for finding in report.errors:
    print(finding.message)
```

`validate()` runs the metadata, structural, and data passes by default, matching the CLI; `schema` and `live` opt the remaining two in, and `structural=False` / `data=False` opt the defaults out. `data_reader_factory=LocalOnlyReader` narrows the data pass to the catalog tree, as `--data-scope local` does.

`RulesConfig` skips rules or changes their severity.

```python
from rashid import RulesConfig, Severity, validate

config = RulesConfig(
    disabled=frozenset({"PTL-VIZ-004"}),
    severity_overrides={"PTL-TTL-002": Severity.ERROR},
)
report = validate("path/to/catalog", config=config)
```

`Report` also carries `passed`, `warnings`, `infos`, and `to_dict()`.

### Helpers for tools that fix what rashid reports

A tool that rewrites catalog metadata has to read that metadata the way rashid reads it. Reimplementing a check produces code that passes its own tests and still disagrees with the rule it was written against. `rashid.api` publishes the helpers rashid's rules use.

```python
from rashid.api import has_cog, is_cog_media_type, links_of, roles_of

if has_cog(item):  # true only for the required COG media type
    ...
```

The module also exports `STRUCTURAL_RELS` and `parse_rfc3339`, along with the multihash codec behind `file:checksum`.

```python
import hashlib
from rashid.api import SHA2_256, encode_multihash

checksum = encode_multihash(SHA2_256, hashlib.sha256(data).digest())
```

`decode_multihash` and `is_well_formed_multihash` read those values back.

`SPDX_LICENSE_IDS` is the license list PTL-LIC-001 validates against, and `canonical_spdx_id` is the case-insensitive lookup behind its hint. Gate on these rather than on a shorter hand-written list, which rejects real identifiers the rule accepts.

```python
from rashid.api import canonical_spdx_id

canonical_spdx_id("apache-2.0")  # "Apache-2.0"
canonical_spdx_id("Apache 2.0")  # None
```

A name imported from `rashid.api` stays available across 0.x releases. The modules behind it are private, and importing from them directly can break in any patch release.

## Rules

Every finding carries a stable id shaped `PTL-GROUP-NNN`, a severity, a message, and the file path. MUST requirements become errors, and SHOULD requirements become warnings.

[docs/rules.md](docs/rules.md) lists the rule groups, the severity exceptions, and the codes rashid emits when a pass degrades.

## Contributing

[docs/contributing.md](docs/contributing.md) covers hook setup, the two-stage gate, and how to run the tests.

`SPEC_REF` records the portolan-spec commit the vendored fixtures were built from. A nightly workflow re-vendors them and opens a pull request when the spec moves.

## License

Apache-2.0.
