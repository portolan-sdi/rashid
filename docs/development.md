# Development

Use this guide to set up rashid, run checks, measure performance, and inspect logs. To propose a change, see the [Portolan contributing guide](https://github.com/portolan-sdi/portolan-ops/blob/main/policies/CONTRIBUTING.md).

## Setup

```bash
uv sync --all-packages --all-extras
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push
```

This installs both workspace distributions, development dependencies, and the geospatial stack.
All Python code lives in `src/rashid_core/`; `meta/rashid/` holds the code-free metapackage and its console entry point.
Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org), which Commitizen checks through the `commit-msg` hook.

## Local checks

Hooks run checks at commit and push time. CI runs both sets.

```bash
# On commit: ruff, ruff-format, codespell,
# actionlint, zizmor, jscpd, and file hygiene checks
uv run pre-commit run --all-files

# On push: bandit, deptry, strict mypy, vulture, and import-linter
uv run pre-commit run --all-files --hook-stage pre-push
```

CI uses [prek](https://github.com/j178/prek) to run the same `.pre-commit-config.yaml`, so these checks apply even if you haven't installed the local hooks.

## Tests

Tests require at least 90% coverage overall and on lines changed by a pull request.

```bash
uv run pytest -n 8             # Full suite, as in CI
uv run pytest -n 8 -m unit     # Fast, isolated tests
```

Tests use the `unit`, `integration`, and `network` markers. Network tests check live servers for the live pass and remote data validation, and skip when offline. Structural and schema validation use the schema closures vendored in `src/rashid_core/_schemas/` and require no network access.

Set `ENABLE_PRE_PUSH_TESTS=1` to include fast tests in the pre-push hook.

## Consolidation guardrails

These gates keep each consolidated concept at its owner ([#266](https://github.com/portolan-sdi/rashid/issues/266)).

| Gate | Enforced boundary |
|---|---|
| Ruff `TID251` | `_href.py` owns URL interpretation; `_store.py` owns requests and object stores. `_json.py` owns JSON loading; `_package.py` owns schema resources and installed versions. `data/formats/_files.py` owns readers and glob enumeration. |
| import-linter | Exhaustive layers start with `__main__` and order every package module. Metadata rules stay independent; only the registry imports them. Only optional formats import geospatial libraries; only the CLI imports click. Only the data entry point imports optional formats, and metadata rules cannot import transport. |
| jscpd 5.2.1 | New Python clones in `src/rashid_core` and `tests` fail the commit hook. Fixtures and virtual environments are excluded. An empty scan fails. |
| Package split | `test_packaging.py` checks version agreement and the core dependency closure. `wheel-smoke` checks isolated wheels and upgrades from rashid 0.4.0. |
| API snapshot | `test_api.py` pins `rashid_core.api` and `rashid_core.__all__`, their defining objects, and callable signatures. Python defines the enum constructor signature. |
| Registry checks | Duplicate IDs fail registry construction. An AST scan compares emitted IDs with `CHECKS` and checks that pass declarations reuse the ID constants. |
| Pointer construction | An AST scan rejects hand-built pointers outside `model.py`: f-strings, addition, percent formatting, `.format`, and slash joins. Generic slash prefixes are checked in values assigned or passed as `pointer` or `json_pointer`. |
| Exemption ledger | `guardrail_exemptions.json` lists every Ruff per-file ignore code (including the #264 signature exemptions), import ignore, allowed importer, baseline, and strict xfail issue, with reasons. Tests pin their counts and reject stale entries. |
| Strict xfails | Tests for unresolved defects must reference an open issue that the PR does not close and use `strict=True`. After a fix, the test fails with XPASS until its xfail marker is removed. Ruff bans imperative `pytest.xfail`, including imported aliases, in tests too. |

`data/formats` owns geospatial readers; storage and Zarr checks remain in `data`.
`_href.py` also owns wire encoding. The import contracts have no ignored imports.
`remote.py` has no banned API use, so it needs no Ruff ignore. Production-only API bans live in `src/ruff.toml`; tests and developer scripts can construct raw fixtures without a blanket TID251 exemption.

The committed jscpd baseline accepts existing clones. Its fingerprint and clone counts may only decrease; `test_quality_config.py` pins the counts without running external tools. The archive self-test checks live fingerprints and rejects stale entries.
Because `origin/main` has no baseline yet, the fingerprint membership comparison becomes active after this PR merges.
After deleting duplication, regenerate it and lower the pinned counts in the same commit:

```bash
uvx jscpd@5.2.1 src/rashid_core tests --baseline .jscpd-baseline.json --update-baseline
```

The commit hook and CI run the same command with `--fail-on-new-clones 0` instead of `--update-baseline`.
CI also runs `scripts/gate_selftest.py` in a Linux job after installing the locked dependencies.
The pinned reusable workflow runs both pre-commit stages; `test_quality_config.py` pins that caller and the jscpd hook.

After committing, prove the gates against injected faults:

```bash
uv run python scripts/gate_selftest.py
uv run python scripts/gate_selftest.py --fault duplicate-block
```

The script extracts `HEAD` with `git archive`, runs each gate on a clean copy, then injects its fault.
It checks banned APIs, import boundaries, duplication, API exports and signatures, duplicate and missing IDs, pointer variants, baseline growth, loose and bare xfails, CI wiring, and exemption growth.
The self-test pins its fault count so dropping a case fails. Each fault must produce its specific diagnostic; syntax errors and clean-copy failures fail the self-test, which runs without modifying the working tree.

## Measuring performance

Use `scripts/profile.py` for repeatable performance measurements. The seeded scenarios in `tests/scenarios.py` generate the same catalogs on every run. Compare small and large scenarios to see how costs grow with catalog size.

```bash
uv run python scripts/profile.py metadata --mode counts
uv run python scripts/profile.py remote --size large --mode time
uv run python scripts/profile.py data --mode mem
uv run python scripts/profile.py remote-data --mode counts
uv run python scripts/profile.py metadata --mode import
uv run python scripts/profile.py metadata --size large --mode instr
```

Choose a scenario:

- `metadata` validates collections and items without reading asset bytes.
- `data` reads real GeoParquet and Cloud Optimized GeoTIFF files.
- `remote` serves the `metadata` catalog over local HTTPS and validates it by URL, with the live pass enabled.
- `remote-data` serves the `data` catalog over local HTTPS from a separate process and validates it by URL, with the data pass enabled. Each asset request waits 20 ms before the server answers, as a remote host would.

Add `--warm` to run the scenario once before measuring, excluding the initial schema load.

Choose a measurement mode:

| Mode | Output | Requirements |
|---|---|---|
| `counts` | Operation counts and stage timings | No extra packages |
| `time` | pyinstrument profile | Loaded automatically with `uv run --with` |
| `mem` | memray profile | Loaded automatically with `uv run --with`; unavailable on Windows |
| `import` | Slowest imports from `python -X importtime` | No extra packages |
| `instr` | Instruction counts from cachegrind | Install `valgrind` first |

The script pins the pyinstrument and memray versions; neither is a rashid dependency. Instruction counts vary less between runs than wall time. Profiler output goes to `.cache/profiles`.

The `counts` output includes every operation counter and the time spent in each stage:

```text
# metadata/small seed=0
json.parses                           147
rules.checks                        1,925
schema.rust_compiles                    3
schema.rust_passes                    103

span              calls      wall ms       cpu ms  peak rss MB
load                  1         14.3         14.3         38.9
rules                 1         23.0         23.0         38.9
structural            1         51.1         51.0         48.8
extensions            1          2.3          2.3         49.1
```

Use this script for performance comparisons. Custom timing loops make results harder to compare.

## Operation counts

Counters are always enabled. `tests/integration/test_operation_counts.py` checks:

- Exact counts for every small scenario and the large `metadata` and `data` scenarios.
- The exact requests each asset in `remote-data` costs, and that the data pass sends them in parallel.
- A memory ceiling for the large `metadata` scenario.

If your change reduces the work performed, update the expected counts in the same pull request.

`tests/unit/test_import_budget.py` checks that `import rashid_core`, `rashid --help`, and `python -m rashid_core --help` do not load the geospatial stack, jsonschema, the network stack, or a profiler. Keep heavy imports inside the functions that need them.

Build both wheels and test isolated installs with:

```bash
uv run python scripts/wheel_smoke.py
```

The smoke check verifies the base dependency allowlist, metadata validation, and upgrades from rashid 0.4.0.
Without the geospatial stack, it requires the missing-stack warning alongside checksum, size, and Zarr findings.

## Logs and timings

Place logging and profiling flags before `check`:

```bash
uv run rashid --timings check --json path/to/catalog
uv run rashid -vv check path/to/catalog
uv run rashid --trace trace.json check path/to/catalog
```

- `--timings` adds a `meta` block to JSON reports. Without `--json`, it prints the stage table to stderr.
- `-v` enables `INFO` logs; `-vv` enables `DEBUG` logs. Logs go to stderr, leaving stdout for the report.
- `--trace` writes a Chrome trace that you can open in [Perfetto](https://ui.perfetto.dev).

Logger names follow the import package, for example:

```text
+0.055s DEBUG rashid_core.runner: rules: start
```

When calling `validate()` from a library, use these environment variables:

| Variable | Effect |
|---|---|
| `RASHID_LOG` | Sets the stderr log level: `debug`, `info`, `warning`, or `error`. |
| `RASHID_LOG_FILE` | Writes every log record to a file as one JSON object per line. |
| `RASHID_TIMINGS` | Set to `1` to record stage timings, or `mem` to also record the tracemalloc peak. |
| `RASHID_TRACE` | Writes a Chrome trace to the specified path at exit. |

## Performance pull requests

For a `perf:` pull request, include before-and-after measurements under `## Verification`. State the scenario name and size, and include at least one of:

- Operation counts from `--mode counts`, with updated expectations in `tests/integration/test_operation_counts.py`.
- A timing block from `--mode counts` or `--mode time`.
- An instruction count from `--mode instr`.

## CI

The shared CI workflow is defined in [portolan-ops](https://github.com/portolan-sdi/portolan-ops). This repo's `.github/workflows/ci.yml` calls it. Change shared CI requirements in portolan-ops; its `norms/ci.md` explains how changes sync to this repo.

`security-audit.yml` stays in this repo. It opens a tracking issue when pip-audit finds a vulnerability and closes the issue when the audit passes.

## Releasing

Commitizen determines the next version from commits since the last tag.

```bash
git checkout main && git pull
git switch -c release/vX.Y.Z
uv run cz bump --changelog
git tag -d "vX.Y.Z"
git push -u origin release/vX.Y.Z
```

Replace `X.Y.Z` with the release version.
`cz bump` updates both project versions, the metapackage's exact core pin, and `CHANGELOG.md`.
The pre-bump hook refreshes `uv.lock`. Commitizen then creates one `bump:` commit and one `vX.Y.Z` tag for both distributions.

Build both wheels and source distributions locally with:

```bash
uv build --package rashid-core --out-dir .cache/dist/core
uv build --package rashid --out-dir .cache/dist/meta
```

`rashid-core` contains all Python code. The code-free `rashid` metapackage provides the console command and depends on `rashid-core[data]` at the same version.
The publish workflow uses the `pypi` environment and publishes `rashid-core` first, then `rashid`.
Both upload steps skip existing files so interrupted releases can be retried.

Delete the local tag. `publish.yml` creates the tag on the merged commit; the local tag points to the release branch commit.

To override the calculated version, pass `--increment PATCH`, as past releases have done. With `major_version_zero` enabled, breaking changes do not trigger a major release, but `feat` commits still trigger a minor release.

Open a release pull request and **squash-merge it with a title that starts with `bump:`**. The publish workflow checks the head commit message for that prefix. A regular merge commit starts with `Merge pull request #N from ...`, so publishing silently skips and nothing reaches PyPI.

Squash-merging puts the `bump:` title on `main`, triggering the workflow to tag, build, publish to PyPI, and create a GitHub release.

If you merge the release incorrectly, run:

```bash
gh workflow run publish.yml --ref main
```

This manual run reads the version from `pyproject.toml` instead of checking the commit message.

Use a pull request for releases. Branch protection blocks direct pushes to `main` unless you have bypass rights.

## Spec fixtures

`SPEC_REF` records the portolan-spec commit used for the vendored fixtures. To refresh them from a local spec checkout, run:

```bash
uv run python scripts/vendor_spec_fixtures.py --spec ../portolan-spec
```

Released schemas come from final `vX.Y.Z` tags. The vendor stores the test-only draft in `tests/fixtures/draft-profile-schema.json`. The vendor reads the single schema on spec main, or the newest version folder in the old layout. The reference-catalog test maps an unreleased URI to this fixture. The wheel contains only tagged profile schemas.

`spec-sync.yml` checks for spec changes nightly and opens a pull request when it finds them. Review the test results before merging. If `test_schema_uri_invariant` fails, rashid needs a code change as well as a fixture refresh.

## Spec releases

rashid applies the requirements of the newest profile version it bundles. A spec release needs two rashid releases: one with the new rules before the tag, and one that bundles the tagged schema after it.

1. Before the spec tag, rashid main must enforce every validator requirement of the new version. `tests/unit/test_spec_coverage.py` checks the citations. `tests/unit/test_schema_parity.py` and `test_reference_catalog_passes_against_the_draft_profile` check rashid against the draft schema on spec main. Release rashid as described in [Releasing](#releasing). The spec examples pin this release.
2. The spec tags `vX.Y.Z`. Confirm that the profile URI of that version serves the tagged schema.
3. Run the sync: `gh workflow run spec-sync.yml -R portolan-sdi/rashid`. The new schema directory makes the pull request a feature, `feat(schema): bundle the Portolan vX.Y.Z profile schema`, with auto-merge off. `DEFAULT_SCHEMA_URI`, `rashid --version`, the JSON report, and `PTL-CNF-005` follow the newest bundled version. The pull request needs no code change. Review it and merge it.
4. Release rashid again, so that the wheel on PyPI applies `vX.Y.Z`.
5. Update the downstream pins. Tools read the newest version from `rashid_core.api.newest_profile_version()`.
