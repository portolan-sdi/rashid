"""Thin Click CLI over the rashid validation library.

Exit codes: 0 when validation passed, 1 when errors were found,
2 on usage errors (Click's default).
"""

from __future__ import annotations

import json as json_module
import os
from pathlib import Path

import click

from rashid import _trace
from rashid.data.reader import LocalOnlyReader
from rashid.model import Report, Severity
from rashid.remote import is_catalog_url
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


class CatalogLocation(click.ParamType[Path | str]):
    """An existing local path, or the https URL a catalog is published under.

    A path is checked for existence here so a typo fails as a usage error, the
    way ``click.Path(exists=True)`` failed it before URLs were accepted. A URL
    is passed through as a string; whether it answers is the runner's to
    report (``PTL-GEN-000``), since that takes the network.
    """

    name = "catalog"

    def convert(
        self, value: object, param: click.Parameter | None, ctx: click.Context | None
    ) -> Path | str:
        if isinstance(value, (Path, str)) and is_catalog_url(str(value)):
            return str(value)
        if str(value).lower().startswith("http://"):
            self.fail(f"{str(value)!r} is not https; only https catalog URLs are read.", param, ctx)
        path = Path(str(value))
        if not path.exists():
            self.fail(f"Path {str(value)!r} does not exist.", param, ctx)
        return path


@click.group()
@click.version_option(package_name="rashid")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Write log records to stderr: -v for INFO, -vv for DEBUG. This sets the log "
    "level only; it does not change which findings print. Same as RASHID_LOG.",
)
@click.option("-q", "--quiet", is_flag=True, help="Write only ERROR log records to stderr.")
@click.option(
    "--timings",
    is_flag=True,
    help="Time each stage. Prints a table to stderr, or adds a meta block to --json "
    "output. Same as RASHID_TIMINGS=1.",
)
@click.option(
    "--trace",
    "trace_path",
    metavar="PATH",
    default=None,
    help="Write a Chrome trace of every stage to PATH at exit; open it in "
    "https://ui.perfetto.dev. Same as RASHID_TRACE.",
)
@click.pass_context
def main(
    ctx: click.Context, verbose: int, quiet: bool, timings: bool, trace_path: str | None
) -> None:
    """rashid — validator and linter for Portolan catalogs."""
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet ask for opposite things.")
    if verbose or quiet:
        level = "error" if quiet else ("info" if verbose == 1 else "debug")
        log_file = os.environ.get(_trace.ENV_LOG_FILE) or None
        try:
            _trace.configure_logging(level, log_file)
        except OSError as exc:
            # The same message the import prints, so the user reads it once.
            _trace.warn_once(f"ignoring {_trace.ENV_LOG}/{_trace.ENV_LOG_FILE}: {exc}")
            _trace.configure_logging(level)
    timings_mode = "on" if timings else _trace.env_timings()
    if timings_mode is not None and not _trace.enabled():
        _trace.enable(timings_mode)
    if trace_path is not None:
        try:
            _trace.trace_to(trace_path)
        except OSError as exc:
            raise click.BadParameter(str(exc), param_hint="--trace") from exc
    # --trace turns spans on too, but only timings ask for the table or meta block.
    ctx.obj = {"timings": timings_mode is not None}


@main.command()
@click.argument("catalog", type=CatalogLocation(), metavar="CATALOG")
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
    "declares. Pinned versions use bundled schemas; an older version of a "
    "registered extension is fetched over https.",
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
    default=None,
    help="Probe the servers behind the assets: HTTP range and CORS (needs network). "
    "When the publish base is known, also check that every linked document exists "
    "there. On by default for a catalog URL, off for a directory.",
)
@click.option(
    "--live-base-url",
    metavar="URL",
    default=None,
    help="Publish base URL for a directory. Not needed for a catalog URL, nor when the "
    "root catalog carries an absolute self link.",
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
    catalog: Path | str,
    as_json: bool,
    structural: bool,
    extensions: bool,
    schema: bool,
    schema_allow_network: bool,
    data: bool,
    data_scope: str,
    live: bool | None,
    live_base_url: str | None,
    list_all: bool,
    force_summary: bool,
) -> None:
    """Validate CATALOG: the Portolan metadata pass, the STAC 1.1.0
    structural pass (unless --no-structural), and — with --schema / --data /
    --live — the bundled Portolan profile schema, the asset bytes, and the
    hosting servers.

    CATALOG is the catalog directory, or the catalog.json inside it, or the
    https URL the catalog is published under. For a URL rashid follows the
    links from the root catalog and runs the hosting checks by default."""
    if list_all and force_summary:
        raise click.UsageError("--all and --summary ask for opposite things.")
    if data_scope != "all" and not data:
        raise click.UsageError("--data-scope and --no-data ask for opposite things.")
    if data_scope != "all" and isinstance(catalog, str):
        raise click.UsageError("--data-scope local reads the tree on disk; a catalog URL has none.")
    if live_base_url is not None and isinstance(catalog, str):
        raise click.UsageError(
            "--live-base-url is not needed with a catalog URL; the URL is the base."
        )
    with _trace.span("check"):
        report = validate(
            catalog,
            structural=structural,
            extensions=extensions,
            schema=schema,
            schema_allow_network=schema_allow_network,
            data=data,
            data_reader_factory=LocalOnlyReader if data_scope == "local" else None,
            live=live,
            live_base_url=live_base_url,
        )
    root = click.get_current_context().find_root().obj or {}
    show_timings = root.get("timings", False)
    if as_json:
        output = report.to_dict()
        if show_timings:
            from importlib.metadata import version

            output["meta"] = _trace.meta(version("rashid"))
        click.echo(json_module.dumps(output, indent=2))
    else:
        _print_human(report, list_all=list_all, force_summary=force_summary)
        if show_timings:
            click.echo(_trace.format_timings(_trace.timings()), err=True)
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
