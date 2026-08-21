"""Thin Click CLI over the rashid validation library.

Exit codes: 0 when validation passed, 1 when errors were found,
2 on usage errors (Click's default).
"""

from __future__ import annotations

import json as json_module
from pathlib import Path

import click

from rashid.data.reader import LocalOnlyReader
from rashid.model import Report, Severity
from rashid.runner import validate

_SEVERITY_TAGS = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "info",
}

#: Findings listed one per line before the per-rule summary takes over. Set to
#: roughly a screenful: past this, a listing costs the reader the top of its
#: own output.
_SUMMARY_THRESHOLD = 50

#: Findings quoted verbatim under each rule in the summary. Enough to show
#: whether a rule fires on one file or across the catalog.
_EXAMPLES_PER_RULE = 3


@click.group()
@click.version_option(package_name="rashid")
def main() -> None:
    """rashid — validator and linter for Portolan catalogs."""


@main.command()
@click.argument(
    "catalog_path",
    type=click.Path(exists=True, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option(
    "--structural/--no-structural",
    default=True,
    help="Run the STAC 1.1.0 structural pass against the bundled schemas (offline).",
)
@click.option(
    "--extensions/--no-extensions",
    "extensions",
    default=True,
    help="Validate every object against the schemas of the STAC extensions it "
    "declares, from the bundled registry-pinned copies (offline).",
)
@click.option(
    "--schema/--no-schema",
    "schema",
    default=False,
    help="Also validate against the bundled Portolan profile schema (offline).",
)
@click.option(
    "--schema-allow-network",
    is_flag=True,
    help="Fetch a schema this build does not bundle over https: an unbundled "
    "profile version, or an extension the registry does not pin.",
)
@click.option(
    "--data/--no-data",
    "data",
    default=True,
    help="Verify asset bytes: checksum, size, format, extent (reads every asset; "
    "network for remote hrefs). On by default; --no-data skips.",
)
@click.option(
    "--data-scope",
    type=click.Choice(["all", "local"]),
    default="all",
    help="Which assets the byte checks may read: all (default), or local to check only "
    "the assets inside the catalog tree, leaving assets hosted elsewhere unread.",
)
@click.option(
    "--live/--no-live",
    "live",
    default=False,
    help="Also probe the servers behind https assets: HTTP range and CORS (needs network).",
)
@click.option(
    "--live-base-url",
    metavar="URL",
    default=None,
    help="Publish base URL; makes relative asset hrefs probeable with --live.",
)
@click.option(
    "--all",
    "list_all",
    is_flag=True,
    help=f"List every finding, however many. Default up to {_SUMMARY_THRESHOLD}.",
)
@click.option(
    "--summary",
    "force_summary",
    is_flag=True,
    help="Count findings per rule instead of listing them.",
)
def check(
    catalog_path: Path,
    as_json: bool,
    structural: bool,
    extensions: bool,
    schema: bool,
    schema_allow_network: bool,
    data: bool,
    data_scope: str,
    live: bool,
    live_base_url: str | None,
    list_all: bool,
    force_summary: bool,
) -> None:
    """Validate CATALOG_PATH: the Portolan metadata pass, the STAC 1.1.0
    structural pass (unless --no-structural), and — with --schema / --data /
    --live — the bundled Portolan profile schema, the asset bytes, and the
    hosting servers.

    CATALOG_PATH is the catalog directory, or the catalog.json inside it."""
    if list_all and force_summary:
        raise click.UsageError("--all and --summary ask for opposite things.")
    if data_scope != "all" and not data:
        raise click.UsageError("--data-scope and --no-data ask for opposite things.")
    report = validate(
        catalog_path,
        structural=structural,
        extensions=extensions,
        schema=schema,
        schema_allow_network=schema_allow_network,
        data=data,
        data_reader_factory=LocalOnlyReader if data_scope == "local" else None,
        live=live,
        live_base_url=live_base_url,
    )
    if as_json:
        click.echo(json_module.dumps(report.to_dict(), indent=2))
    else:
        _print_human(report, list_all=list_all, force_summary=force_summary)
    raise SystemExit(0 if report.passed else 1)


def _print_human(report: Report, *, list_all: bool = False, force_summary: bool = False) -> None:
    """Render ``report`` for a person, listing or summarizing its findings.

    A rule broken once per asset produces one finding per asset, so a large
    catalog can push tens of thousands of lines past the scrollback. Above
    ``_SUMMARY_THRESHOLD`` findings the per-rule summary replaces the listing
    unless ``--all`` asked for every line; ``--summary`` requests it at any
    size.
    """
    if not report.findings:
        click.echo(f"OK: {report.files_checked} files checked, no findings.")
        return
    collapsed = not list_all and (force_summary or len(report.findings) > _SUMMARY_THRESHOLD)
    if collapsed:
        _print_summary(report, note=not force_summary)
    else:
        _print_listing(report)


def _print_listing(report: Report) -> None:
    """Every finding, grouped under the file it was found in."""
    current_path: str | None = None
    for finding in report.findings:
        if finding.path != current_path:
            current_path = finding.path
            click.echo(finding.path)
        tag = _SEVERITY_TAGS[finding.severity]
        click.echo(f"  {tag:<7} {finding.rule_id}  {finding.message}")
        if finding.fix_hint:
            click.echo(f"          hint: {finding.fix_hint}")
    _print_counts(report)


def _print_summary(report: Report, *, note: bool) -> None:
    """One block per rule: how often it fired, and a few examples.

    ``note`` explains the collapse when it was rashid's decision rather than
    the caller's.
    """
    _print_counts(report)
    if note:
        click.echo("Too many to list; showing a summary (--all to list every finding).")
    summaries = report.by_rule()
    tag_width = max(len(_SEVERITY_TAGS[s.severity]) for s in summaries)
    id_width = max(len(s.rule_id) for s in summaries)
    count_width = max(len(f"{s.count}x") for s in summaries)
    for summary in summaries:
        click.echo("")
        tag = _SEVERITY_TAGS[summary.severity]
        count = f"{summary.count}x"
        click.echo(
            f"{tag:<{tag_width}}  {summary.rule_id:<{id_width}}"
            f"  {count:>{count_width}}  {summary.description}".rstrip()
        )
        examples = [
            finding
            for finding in report.findings
            if finding.rule_id == summary.rule_id and finding.severity == summary.severity
        ][:_EXAMPLES_PER_RULE]
        for finding in examples:
            click.echo(f"    {finding.path}: {finding.message}")
        remaining = summary.count - len(examples)
        if remaining:
            click.echo(f"    ... {remaining} more in {summary.file_count} files")


def _print_counts(report: Report) -> None:
    click.echo(
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s),"
        f" {len(report.infos)} info(s) across {report.files_checked} files."
    )
