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

`--no-data` skips byte verification when the assets are huge or remote and only the metadata verdict is needed. Add `--json` for a machine-readable report.

## What it checks

Validation runs as five separable passes.

**Metadata.** Every spec requirement rashid can check from the catalog's JSON, without reading asset bytes.

**Structural.** STAC 1.1.0 core validity, checked offline against the schemas vendored in the wheel. Only the core schema applies. Declared extensions belong to the metadata pass, which keeps an unpublished extension schema from failing the tree.

**Schema.** The published [Portolan profile schema](https://schemas.portolan-sdi.org/portolan/), applied to every object. rashid also implements those requirements in code, which yields precise messages and fix hints. Running both catches drift between them, at the cost of reporting some defects twice. Opt in with `--schema`.

**Data.** Asset bytes, local files and remote https URLs alike, checked against the declared metadata and the format rules. rashid recomputes checksum and size, confirms media type by magic number, and enforces storage rules a metadata reader cannot see. Expect a real catalog to fail here on rules its metadata satisfies, because this pass is stricter than what current tooling emits. On by default; `--no-data` skips it.

**Live hosting.** The servers behind remote https assets, probed for HTTP range support and CORS. Those are properties of the server rather than of any file. Range and CORS cost one probe per host, and each asset costs one HEAD. Opt in with `--live`.

A pass that cannot run reports a warning rather than passing quietly.

## Use it as a library

```python
from rashid import validate

report = validate("path/to/catalog")
for finding in report.errors:
    print(finding.message)
```

`validate()` runs the metadata, structural, and data passes by default, matching the CLI; `schema` and `live` opt the remaining two in, and `structural=False` / `data=False` opt the defaults out.

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

## Rules

Every finding carries a stable id shaped `PTL-GROUP-NNN`, a severity, a message, and the file path. MUST requirements become errors, and SHOULD requirements become warnings.

[docs/rules.md](docs/rules.md) lists the rule groups, the severity exceptions, and the codes rashid emits when a pass degrades.

## Contributing

[docs/contributing.md](docs/contributing.md) covers hook setup, the two-stage gate, and how to run the tests.

`SPEC_REF` records the portolan-spec commit the vendored fixtures were built from. A nightly workflow re-vendors them and opens a pull request when the spec moves.

## License

Apache-2.0.
