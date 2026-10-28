# Development

Use this guide to set up rashid, run checks, measure performance, and inspect logs. To propose a change, see the [Portolan contributing guide](https://github.com/portolan-sdi/portolan-ops/blob/main/policies/CONTRIBUTING.md).

## Setup

```bash
uv sync
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push
```

`uv sync` includes development dependencies. Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org), which Commitizen checks through the `commit-msg` hook.

## Local checks

Hooks run checks at commit and push time. CI runs both sets.

```bash
# On commit: ruff, ruff-format, codespell,
# actionlint, zizmor, and file hygiene checks
uv run pre-commit run --all-files

# On push: deptry, strict mypy, vulture,
# xenon complexity checks, and import-linter
uv run pre-commit run --all-files --hook-stage pre-push
```

CI uses [prek](https://github.com/j178/prek) to run the same `.pre-commit-config.yaml`, so these checks apply even if you haven't installed the local hooks.

## Tests

Tests require at least 90% coverage overall and on lines changed by a pull request.

```bash
uv run pytest                 # Full suite
uv run pytest -n auto         # In parallel, as in CI
uv run pytest -m unit         # Fast, isolated tests
```

Tests use the `unit`, `integration`, and `network` markers. Network tests check live servers for the live pass and remote data validation, and skip when offline. Structural and schema validation use the schema closures vendored in `src/rashid/_schemas/` and require no network access.

Set `ENABLE_PRE_PUSH_TESTS=1` to include fast tests in the pre-push hook.

## Mutation testing

Mutation testing checks whether tests catch changes to the code, beyond measuring which lines they run.

```bash
uv run mutmut run
```

CI runs mutation tests nightly and checks the score against `.mutation-baseline`, currently 72%. Raise the minimum as tests improve. If you lower it, explain why in the pull request.

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
rules.checks                        1,821
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

`tests/unit/test_import_budget.py` checks that `import rashid` and `rashid --help` do not load the geospatial stack, jsonschema, the network stack, or a profiler. Keep heavy imports inside the functions that need them.

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

Replace `X.Y.Z` with the release version. `cz bump` updates `pyproject.toml` and `CHANGELOG.md`, and a pre-bump hook refreshes `uv.lock`. It then creates a `bump:` commit and tag.

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

`spec-sync.yml` checks for spec changes nightly and opens a pull request when it finds them. Review the test results before merging. If `test_schema_uri_invariant` fails, rashid needs a code change as well as a fixture refresh.
