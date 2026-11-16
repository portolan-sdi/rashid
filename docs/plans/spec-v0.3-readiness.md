# rashid: ready for Portolan spec v0.3.0

## Context

Spec v0.3.0 (milestone `spec-v0.3`) adds about 35 validator-enforced requirements and makes breaking changes: absolute partition globs (FMT-064), native Parquet (FMT-004), s3 hrefs with an https alternate (CORE-087), range/CORS only for partial-read files, classification, mosaic overviews, and column descriptions. rashid main already has companion PRs for most rules (#290–#293, #243, #221/#297, #257, #174, #263). Five Opus audits found that rashid is still not release-ready:

- No rashid release since 0.3.0 (2026-10-12). All 0.3 rules exist only on main. The spec examples pin `rashid<0.2.0` and call a `validate()` signature that #272 removed. portolan-cli (`<0.4.0`) also breaks on that change.
- Two MUSTs pass without enforcement. `_check_cors` (`live.py:430`) accepts any `Access-Control-Allow-Origin` value. `data/partitions.py:39` reads no file for an absolute glob, so FMT-021 never runs on a conforming catalog, and nothing reports it (#294).
- s3 hrefs with an https alternate are skipped by the data and live passes, and nothing reports it.
- About 15 medium and low rule errors (FMT-008 false warning on native files, FMT-062 geography, CORE-057 sub-catalogs, spatial-order weight misalignment, and more).
- Version handling: `DEFAULT_SCHEMA_URI` and test constants are hardcoded to v0.2.0. The coverage gate ignores `PORTO-ZARR` and `status: incubating`. There is no spec-version signal in `--version` or JSON output. The next spec-sync (to `ceaf3f4`) goes red on PTL-VIZ-007.
- Zarr draft PR #295 matches the merged spec but conflicts with main and fails Vale and the PR-body lint.

Maintainer decisions: judge by the latest rules, with an info notice for other declared versions. Companion spec PRs are in scope. Ship #295. Spec #257 is in v0.3.0. The post-tag release is 0.5.0.

## PR grouping

The IDs below (R1, D2, …) are work items, not PRs. Ship them as 8 rashid PRs, each a branch from `origin/main`:

| PR | Work items | Title |
|---|---|---|
| 1 | R1 + the coverage-gate part of R2 | `chore: sync spec fixtures to ceaf3f4` |
| 2 | R2 (existing branch `feat/zarr-incubating-checks`, PR #295) | `feat(zarr): check a Zarr store against the incubating profile` |
| 3 | D2, D3, live part of D4 | `fix(live)!: enforce CORS origins, scope hosts, and probe https alternates` |
| 4 | data part of D4, D5, D6, D7 | `fix(data)!: read https alternates and fix native GeoParquet, ordering, and column checks` |
| 5 | D10 | `feat(data)!: list bucket partition globs and check sampled partition files` |
| 6 | D1, D8, D9, R7 | `fix(rules)!: correct provenance, asset, and partition rules for spec v0.3` |
| 7 | R3, R4, R5, R6, R8, R9, R10, R11 | `feat: report the spec version rashid applies` |
| 8 | R13, then R12 as its own `bump:` PR | `feat(data)!: scope embedded COG statistics to bands over 8 bits` |

Order: 1 → 2 → 7 in one chain, and 3 → 4 → 5 → 6 in a second. 8 comes last. The spec PRs (S1–S10) can be grouped the same way.

## Working method

- Use one checkout. Create each branch with `git switch -c <branch> origin/main`.
- Each PR: read the files, then implement with tests and docs. Run `uv run pre-commit run --all-files`, the pre-push stage, and `uv run pytest -n 4`. Then run an Opus code-review subagent on the diff (it gets the code, not your conclusions). Push and open a ready PR that follows the portolan-ops template and references an issue (`#N` in "Why"; CI requires it).
- In a Claude Code cloud session, `gh pr` and `gh issue` return 403. Open PRs and issues through `gh api repos/<owner>/<repo>/pulls` and `.../issues` (REST).
- Every behavior change updates `docs/rules.md`, plus `test_operation_counts.py` where counts move.
- Constraints: the import budget (obstore and the geo stack load lazily), import-linter (`rules` cannot import `_store` or `rashid.data`), the #272 single-owner guardrails, 90% diff coverage, and Vale rewording (suppression comments do not work in CI).

## Phase A — rashid PRs before the 0.4.0 release

### A1. Sync and incubating gate
- **R1** `chore: sync spec fixtures to ceaf3f4`. Manual branch, not `chore/spec-sync` (the bot force-pushes that branch). Run `scripts/vendor_spec_fixtures.py --spec <spec clone>`. In `tests/integration/test_reference_catalog.py`, allow PTL-VIZ-007 on the two `sample-scenes` paths only, with a link to the spec issue for S1. Remove the allowance in R14.
- **R2** Finish #295 (Zarr). Rebase onto R1 and drop its hand-vendored fixture commits. Resolve the conflicts in `docs/rules.md` and `registry.py`. In `test_spec_coverage.py`, the gate matches `PORTO-[A-Z]+-\d{3}`, honors `status: incubating` (floor WARNING), and asserts that every `- id:` line is parsed. Add a shared `https_alternate()` helper to `rules/_common.py` and use it in AST-002, `live._store_url`, and `check_zarr`. Leave Zarr store prefixes out of `_targets_by_host`. Add a test for a host that serves only Zarr stores (403 and 404). Reword the docs for Vale. Put `#<issue>` in the body.

### A2. Live pass (chain)
- **D2** `fix(live)!: require a permitting Access-Control-Allow-Origin on GET and preflight`. Add `_allows_origin()`: `*` or the exact `_PROBE_ORIGIN`. The preflight also fails on a status that is not 2xx. Move PTL-LIV-008's citation from CORE-023 to CORE-035/086/073.
- **D3** `fix(live)!: downgrade hosting findings when no publish base separates own hosts from upstream`. Add `_Target.owned`. With no base, LIV-001..005/008 become WARNING, plus one LIV-000 info that suggests a base URL.
- **D4** `feat(data,live): read and probe the https alternate of non-https hrefs; probe rel:pmtiles links`. Add `read_target(asset)` in `rules/_common.py` and use it in `data/checks._check_asset`, `storage.check_bytes(href=)`, `_reads_remote`, and `live._targets_by_host`. Move DAT-020 above the locate gate. Add one DAT-000 and one LIV-000 info that list the unreachable assets. `rel: pmtiles` links become partial-read targets. Add an autouse conftest guard that stops tests from reaching the network by accident. Metadata-only tests get `ValidationOptions(data=False)`.

### A3. Data pass (chain)
- **D5** `fix(data): weight footer ordering by boxed row groups only`. `_covering_bboxes` returns the kept indexes. Filter the row counts and sizes to match.
- **D6** `fix(data)!: native GeoParquet statistics and version rules (FMT-004/007/008)`. No covering-column warning without a `geo` key. Stats are required on every native geometry column. A stale `geo` version on a native file gives a WARNING. An rc version gives a WARNING. Flip the pin in `test_data_storage.py:865`.
- **D7** `fix(data): check a collection's own table:columns (FMT-077) and warn on partitioned vector data without table:columns (FMT-046)`.

### A4. Metadata rules (independent)
- **D8** `fix(rules)!: provenance and asset corrections`. CORE-057 applies to every all-mirror sub-catalog. CORE-053 compares the media-type essence (new `media_type_essence()`). PTL-PRO-004 becomes a WARNING. Remove CORE-025 on `item_assets`. FMT-062 and VIZ-001 recognize `geography`/`geog`.
- **R7** `fix(rules)!: align the asset rules with the v0.3 profile schema` (follow-ups to spec #255). PTL-AST-008 counts PMTiles `data` assets on items. PTL-AST-009 and PTL-AST-012 skip `source` assets. Remove the IPv6 exemption in AST-012.
- **D9** `fix(rules)!: reject file:// partition globs and pin the partition extension version`. Add `_href.bucket_glob()` as the single owner of "listable glob". PTL-PRT-001 errors on a partition URI that is not v1.x. CNF-004 skips Partition so it does not report the same problem twice.
- **D1** `docs: replace retired spec IDs and correct pass descriptions`. Fix the FMT-044/049/053/054 test comments, `checks.py:16`, and `docs/rules.md:107-108`.

### A5. Remote partitions (#294), last in the data chain
- **D10** `feat(data)!: list s3/gs/az partition globs anonymously and check sampled partition files`. Depends on D4, D6, D7, and D9.
  - `_store.list_objects()`: lazy obstore `S3Store`/`GCSStore`/`AzureStore` with `skip_signature=True`, explicit region and endpoint, and the shared retry and user agent. Add the `http.lists` counter and span. Add `bucket_region()` from `x-amz-bucket-region`. Add ruff bans on the obstore store classes outside `_store.py`.
  - `data/_files.py`: `glob_regex()` (`*`, `?`, `[]`, `**`), the literal listing prefix, and `remote_partition_files()`. Local globs switch to `recursive=True`.
  - `data/partitions.py`: read `storage:schemes` from the node and its ancestors. Supported types are `aws-s3`, `custom-s3` (expand the `platform` template), and `ms-azure`. Requester-pays, a missing account, or an unresolved template gives an info refusal.
  - Sampling: list up to 10,000 keys. Read footers of 64 evenly spaced files (always the first and last) for DAT-014/007/008/012/023. Read rows of 4 files for DAT-006 (new `rows=` flag on `check_geoparquet`). `reader.Locator.size` skips the HEAD.
  - Findings: listing denied, not found, or empty gives a WARNING. A cap gives an info with counts. A policy refusal gives an info.
  - Tests: a fake lister in unit tests. A `ListBucketResult` endpoint in `tests/_https.py` for integration. New exact-count tests (1 list request plus 1 ranged GET per sampled footer, no HEAD). Rewrite the "s3 is skipped" tests in `test_data_storage.py`.

### A6. Versioning and report
- **R3** `refactor(schema): derive the default profile URI from the newest bundled schema`. New leaf `src/rashid/_profile.py` with numeric version sort. `DEFAULT_SCHEMA_URI`, the `conformance.py:44` hint, and `conftest.PORTOLAN_URI` derive from it. Fix `test_cli_schema_map.py:176` and the README example.
- **R4** `test(schema): rehearse parity and the reference catalog against the draft profile schema`.
- **R5** `test(spec): validate the vendored profile examples`. Lay out the files by their links and expect no STR/EXT/SCH findings. Keep an allowlist that rejects stale entries.
- **R6** `feat(rules): report a declared profile version other than the newest bundled (PTL-CNF-005)`. Info at the root only. Add a "Which spec version rashid applies" section to `docs/rules.md`, and document that `--schema` still follows the declared version. Cite PORTO-CORE-089 after S6 merges.
- **R8** `feat(cli): show bundled profile versions and the spec commit in --version`. The vendor script writes `_schemas/spec-ref.json`. `rashid.api` exports the `_profile` helpers.
- **R9** `feat(report): publish a versioned JSON Schema for the --json report` (closes #207). Add `$schema`, `report_version`, and a `validator` block (version, spec commit, bundled versions, declared URI, passes). Put the schema at `_schemas/report/v1.0.0/schema.json`. Attach it to releases in `publish.yml`.
- **R10** `ci(spec-sync): title a sync that bundles a new profile version as a feature`. That sync gets no auto-merge.
- **R11** `docs(development): add the spec release runbook` (the text the release Plan agent drafted, adapted).

### A7. Spec #257 companion
- **R13** `feat(data)!: scope embedded COG statistics to bands over 8 bits`. Re-vendor after #257 merges. Cite FMT-026/083/084. Merge it with #257 (retargeted to main) before R12.

### A8. Release 0.4.0 ⚠ (needs your go-ahead: merging publishes to PyPI)
- **R12** `bump: version 0.3.0 → 0.4.0`. Run `cz bump --changelog` and add migration notes (`ValidationOptions`, #292, #174). Body says `Closes #283`. Smoke test: `uv run --with dist/*.whl python -c "from rashid import ValidationOptions, validate"`, then `rashid check --schema` on the spec reference catalog.

## Phase B — portolan-spec companion PRs

- **S1** `build(examples)!: validate the examples with rashid 0.4`. Pin `>=0.4.0,<0.5.0`. Port `validate.py` to `ValidationOptions` + `LocalOnlyReader`. `scenes.py` builds the overview COG. Every asset gets a title and a description. `check_catalogs.py` gets an allowlist for the PTL-DAT-020 warnings expected under FMT-078 that rejects stale entries.
- **S2/S3/S4** Rebase #254 onto S1, update #255's stale note, then `chore(stac): declare v0.3.0 on main`.
- **S5** Retarget #257 to main, with `Companion-PR` pointing at R13.
- **S6** `docs(core): state which version's requirements a validator applies` (PORTO-CORE-089, MAY, validator).
- **S7** `feat(stac): register the Storage and Datacube extensions`. Check the current Datacube release first.
- **S8** `docs(core): drop the v0.1 qualifier on versions.json` (with the PORTO-CORE-012 quote).
- **S9** `ci(schema): serve the rashid report schema` (after R9 is released).
- **S10** `chore(release): v0.3.0`. Its changelog names rashid 0.4.0 (rules) and 0.5.0 (schema).

## Phase C — tag and post-tag ⚠ (each step needs your go-ahead)

1. Tag spec `v0.3.0` and publish. Check that `schemas.portolan-sdi.org/portolan/v0.3.0/schema.json` serves the tagged bytes.
2. `gh workflow run spec-sync.yml -R portolan-sdi/rashid` → **R14** `feat(schema): bundle the Portolan v0.3.0 profile schema`. It needs no hand edits, thanks to R3. Remove the R1 allowance.
3. **R15** `bump: version 0.4.0 → 0.5.0`.
4. **S11**: the spec pins rashid 0.5 and declares v0.4.0 on main.
5. portolan-cli `build(deps)!: require rashid 0.5`. Port `runner.py` to `ValidationOptions`, use the `fetch_schema` monkeypatch target, and take the newest version from `rashid.api` (not a string `max`). portolan-registry `==0.5.0`.
6. Housekeeping: close #95, #158, and #283. Delete the merged branches. Turn on `delete_branch_on_merge`.

## Verification

- Per PR: pre-commit (both stages), `uv run pytest -n 4`, diff coverage ≥90%, then an Opus review subagent. Fix its confirmed findings before opening the PR.
- End-to-end before R12: build the wheel and run it on spec main's reference catalog with `--schema --all` and the #255 draft schema mapped. Expect 0 errors. Every remaining warning must be on the S1 allowlist.
- Remote partitions: integration test against the local HTTPS `ListBucketResult` server. One manual run against a public s3 partitioned registry collection, read-only and anonymous.
- CORS and alternates: `FakeProber` and `https_server` tests for each new branch.
- Version policy: CNF-005 tests for older, newer, and equal versions. The R4 rehearsal passes against the draft v0.3.0 schema.
- A final independent Opus audit of rashid main against spec main repeats the coverage audit. It must find no unenforced validator MUST and no silent skip.
