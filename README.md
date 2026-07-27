# reis

[![CI](https://github.com/portolan-sdi/reis/actions/workflows/ci.yml/badge.svg)](https://github.com/portolan-sdi/reis/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Validator and linter for [Portolan](https://www.portolan-sdi.org/) catalogs. Named after [Piri Reis](https://www.unesco.org/en/memory-world/piri-reis-world-map-1513).

reis reads a catalog directory and reports every rule it breaks. Portolan conformance is not a claim you make. It is passing this validator.

## Install

```bash
uv tool install reis
```

The data pass reads asset bytes, which needs a geospatial stack the core validator does without. Install the extra only if you plan to run it.

```bash
uv tool install "reis[data]"
```

## Check a catalog

```bash
reis check path/to/catalog
```

The exit code carries the result. 0 means the catalog broke no MUST, and 1 means it broke at least one.

Three passes stay off until you ask for them.

```bash
reis check path/to/catalog --schema --data --live
```

Each opt-in pass reaches the network, and `--data` also needs `reis[data]`. Add `--json` to get the report as JSON rather than text.

## What it checks

Validation runs as five separable passes.

**Metadata.** Every requirement in the [Portolan spec](https://github.com/portolan-sdi/portolan-spec) checkable from the catalog's JSON alone, without reading asset bytes.

**Structural.** STAC 1.1.0 core validity, delegated to [stac-validator](https://github.com/stac-utils/stac-validator). Each object is checked against the core schema only. Declared extensions belong to the metadata pass, so an unpublished extension schema never breaks this.

**Schema.** The published [Portolan profile schema](https://schemas.portolan-sdi.org/portolan/) applied to every object. reis implements those requirements by hand for precise messages and fix hints, so this pass overlaps them deliberately and catches drift between the two. A defect both find is reported twice. Opt-in with `--schema`.

**Data.** Asset bytes, local files and remote https URLs alike, checked against the declared metadata and the format MUSTs. It recomputes checksum and size, confirms media type by magic number, and enforces the cloud-native storage rules a metadata reader cannot see. Opt-in with `--data`, and stricter than what current tooling emits, so a real catalog can fail here on rules its metadata satisfies.

**Live hosting.** The servers behind remote https assets, probed for HTTP range support and CORS. These are properties of the server rather than of any file. Range and CORS are probed once per host, and only a cheap HEAD runs per asset. Opt-in with `--live`.

A pass that cannot run reports a warning rather than passing quietly. A dropped network or a missing dependency lands in the report instead of turning the run green.

## Use it as a library

```python
from reis import validate

report = validate("path/to/catalog")
for finding in report.errors:
    print(finding.message)
```

`validate()` runs the metadata pass alone. The keyword arguments `structural`, `schema`, `data`, and `live` turn on the rest. Note that `structural` is on by default in the CLI and off here, because it reaches the network.

`RulesConfig` skips rules or changes their severity.

```python
from reis import RulesConfig, Severity, validate

config = RulesConfig(
    disabled=frozenset({"PTL-VIZ-004"}),
    severity_overrides={"PTL-TTL-002": Severity.ERROR},
)
report = validate("path/to/catalog", config=config)
```

`Report` also carries `passed`, `warnings`, `infos`, and `to_dict()` for machine output.

## Rules

Every finding carries a stable id shaped `PTL-GROUP-NNN`, a severity, a message, and the offending file path. A MUST in the spec becomes an error, and a SHOULD becomes a warning.

[docs/rules.md](docs/rules.md) lists every rule group, the deliberate severity exceptions, and the codes reis emits when a pass degrades.

## Contributing

[docs/contributing.md](docs/contributing.md) covers hook setup, the two-stage gate, and how to run the tests.

`SPEC_REF` records the portolan-spec commit the vendored fixtures were built from. A nightly workflow re-vendors them and opens a pull request when the spec moves.

## License

Apache-2.0.
