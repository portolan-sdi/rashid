# Contributing

## Setup

```bash
uv sync
uv run pre-commit install \
  --hook-type pre-commit \
  --hook-type commit-msg \
  --hook-type pre-push
```

The dev dependency group installs with the project. Commits follow
[Conventional Commits](https://www.conventionalcommits.org), enforced by
commitizen on the `commit-msg` hook.

## The two-stage gate

Hooks run in two stages, and CI reproduces both.

```bash
# fast gate, every commit: ruff, ruff-format, codespell,
# actionlint, zizmor, file hygiene
uv run pre-commit run --all-files

# full gate, every push: deptry, mypy (strict), vulture,
# xenon (complexity), import-linter
uv run pre-commit run --all-files --hook-stage pre-push
```

CI runs these with [prek](https://github.com/j178/prek) rather than
pre-commit. Both read the same `.pre-commit-config.yaml`. A clone without
hooks installed therefore faces the same checks.

## Tests

Tests carry a 90% coverage floor, and pull requests also need 90% on
changed lines.

```bash
uv run pytest                 # full suite
uv run pytest -n auto         # parallelised, as CI runs it
uv run pytest -m unit         # fast, isolated tests only
```

Markers are `unit`, `integration`, and `network`. The `network` tests
drive the real stac-validator against `schemas.stacspec.org` and skip
themselves when offline. Setting `ENABLE_PRE_PUSH_TESTS=1` adds the fast
tests to the pre-push hook.

## Mutation testing

Coverage measures which lines run. Mutation testing measures whether the
tests notice when those lines change.

```bash
uv run mutmut run
```

CI runs this nightly against the floor in `.mutation-baseline`, currently
72%. Ratchet that number up as the suite improves. Lowering it needs a
justification in the pull request that does so.

## CI

CI logic lives in
[portolan-ops](https://github.com/portolan-sdi/portolan-ops) as a
reusable workflow, and `.github/workflows/ci.yml` here is a thin caller.
Change the shared floor there, not here. `norms/ci.md` in that repo
covers how a change reaches this one.

`security-audit.yml` stays local. It opens a tracking issue when
pip-audit finds a vulnerability and closes it when the audit goes clean.

## Spec fixtures

`SPEC_REF` records the portolan-spec commit the vendored fixtures came
from. Re-vendor against a checkout of the spec:

```bash
uv run python scripts/vendor_spec_fixtures.py --spec ../portolan-spec
```

`spec-sync.yml` runs this nightly and opens a pull request when the spec
moves. Read that pull request's test run before merging it. A red
`test_schema_uri_invariant` means reis needs a code change rather than a
fixture refresh.
