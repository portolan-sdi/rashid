# rashid

[![PyPI](https://img.shields.io/pypi/v/rashid.svg)](https://pypi.org/project/rashid/)
[![Python versions](https://img.shields.io/pypi/pyversions/rashid.svg)](https://pypi.org/project/rashid/)
[![CI](https://github.com/portolan-sdi/rashid/actions/workflows/ci.yml/badge.svg)](https://github.com/portolan-sdi/rashid/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

rashid checks whether a [Portolan](https://www.portolan-sdi.org/) catalog follows the standard.

Give it a catalog directory or its root `catalog.json`. rashid checks the metadata, structure, and asset bytes. It reports each problem with a stable rule ID.

The name comes from the Arabic root ر ش د. Its participles رَاشِدٌ and رَشِيدٌ mean ["taking, or following, a right way or course or direction"](https://arabiclexicon.hawramani.com/?p=5472#205af1).

## Install

```bash
uv tool install rashid
```

rashid requires Python 3.10 or later.

## Check a Catalog

```bash
rashid check path/to/catalog
```

Exit code `0` means the catalog breaks no MUST requirement. Exit code `1` means it breaks at least one.

By default, rashid checks:

- Portolan metadata requirements.
- STAC 1.1.0 core structure with schemas included in the package.
- Local and remote asset bytes against their metadata and format rules.

Use `--no-data` when you only need a metadata and structure result. Use `--data-scope local` to keep byte checks inside the catalog directory.

```bash
rashid check path/to/catalog --data-scope local
```

Use `--live` to check HTTP range support and CORS on published assets. Relative asset links also require the catalog's public base URL.

```bash
rashid check path/to/catalog \
  --live \
  --live-base-url https://data.example.org/my-catalog/
```

Use `--json` for a machine-readable report.

## Read the Report

Each finding contains a rule ID, severity, message, and file path. MUST requirements produce errors, and SHOULD requirements produce warnings.

A catalog can produce thousands of similar findings. After 50 findings, rashid groups results by rule and shows representative examples.

Use `--all` to list every finding. Use `--summary` to group any report by rule. JSON output always contains the complete `findings` array and a `summary.by_rule` object.

## Choose Additional Checks

rashid provides six separate validation passes:

- **Metadata** checks Portolan requirements in catalog JSON.
- **Structural** checks STAC 1.1.0 core structure offline.
- **Extension** checks each object against the schemas of the STAC extensions it declares, offline.
- **Schema** checks each object against the packaged Portolan profile schema.
- **Data** reads asset bytes and verifies checksums, sizes, formats, and extents.
- **Live hosting** checks HTTP range support and CORS on remote servers.

Metadata, structural, extension, and data checks run by default. Use `--schema` as a cross-check against the published profile schema. Use `--live` for checks that require a published catalog and network access.

The extension pass uses the versions that the profile's extension registry pins. rashid packages those schemas, so the pass needs no network. rashid reports an extension outside the registry as an info finding rather than validating it. Use `--schema-allow-network` to fetch and check such a schema.

If a pass cannot run, rashid reports a warning. It does not treat the skipped pass as successful.

For transfer details and every rule ID, see [the rule reference](docs/rules.md).

## Use rashid from Python

```python
from rashid import validate

report = validate("path/to/catalog")

for finding in report.errors:
    print(finding.message)
```

`validate()` uses the same default passes as the command-line interface. Set `schema=True` or `live=True` to add those passes. Set `extensions=False` to skip the extension pass.

Use `RulesConfig` to disable rules or change their severity.

```python
from rashid import RulesConfig, Severity, validate

config = RulesConfig(
    disabled=frozenset({"PTL-VIZ-004"}),
    severity_overrides={"PTL-TTL-002": Severity.ERROR},
)

report = validate("path/to/catalog", config=config)
```

The public `rashid.api` module also provides helpers for tools that repair catalogs. Names from this module remain available across 0.x releases. Other internal modules can change in a patch release.

## Documentation

- [Rule reference](docs/rules.md) describes every validation pass, rule group, and degraded-pass code.
- [Contributing guide](docs/contributing.md) covers development setup, tests, and repository checks.
- [Portolan specification](https://github.com/portolan-sdi/portolan-spec) defines the standard that rashid implements.

## License

rashid is available under the [Apache License 2.0](LICENSE).
