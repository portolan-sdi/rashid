<!-- ops-sync:begin — synced from portolan-sdi/portolan-ops. Edit there, not here. -->

<!-- Title in conventional-commit form: it becomes the squash commit message. -->

## What this changes

<!-- Two or three sentences. What behaves differently once this merges. -->

## Why

<!-- One or two sentences. Link the issue rather than restating it. -->

## Verification

<!-- Paste the command you ran and the output you got. Name the data it ran
     against: a URL or a catalog path. Green tests are not verification. -->

- [ ] This change does not alter behavior (docs, chore, or CI only).

## Related issues

<!--
Budget: 200 words outside code blocks, no section longer than six lines. A
reviewer should finish this in under a minute. Prose follows the org style
guide: https://github.com/portolan-sdi/portolan-ops/blob/main/STYLE.md

CI checks the budget and the verification evidence on every push and edit.
-->

<!-- ops-sync:end -->

## Checklist

- [ ] Tests cover the change (each new rule has valid + violating cases)
- [ ] `uv run pre-commit run --all-files` and `--hook-stage pre-push` pass
- [ ] README updated if rules or CLI behavior changed
- [ ] Commit messages follow conventional commits (enforced by commitizen)
