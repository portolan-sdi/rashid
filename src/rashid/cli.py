"""Thin Click CLI over the rashid validation library.

Exit codes: 0 when validation passed, 1 when errors were found,
2 on usage errors (Click's default).
"""

from __future__ import annotations

import json as json_module
from pathlib import Path

import click

from rashid.model import Report, Severity
from rashid.runner import validate

_SEVERITY_TAGS = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFO: "info",
}


@click.group()
@click.version_option(package_name="rashid")
def main() -> None:
    """rashid — validator and linter for Portolan catalogs."""


@main.command()
@click.argument(
    "catalog_path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option(
    "--structural/--no-structural",
    default=True,
    help="Run the STAC 1.1.0 structural pass via stac-validator (needs network).",
)
@click.option(
    "--schema/--no-schema",
    "schema",
    default=False,
    help="Also validate against the published Portolan profile schema (needs network).",
)
@click.option(
    "--data/--no-data",
    "data",
    default=False,
    help="Also verify asset bytes: checksum, size, format, extent (needs rashid[data], network).",
)
@click.option(
    "--live/--no-live",
    "live",
    default=False,
    help="Also probe the servers behind https assets: HTTP range and CORS (needs network).",
)
def check(
    catalog_path: Path, as_json: bool, structural: bool, schema: bool, data: bool, live: bool
) -> None:
    """Validate CATALOG_PATH: the Portolan metadata pass, the STAC 1.1.0
    structural pass (unless --no-structural), and — with --schema / --data /
    --live — the published Portolan profile schema, the asset bytes, and the
    hosting servers."""
    report = validate(catalog_path, structural=structural, schema=schema, data=data, live=live)
    if as_json:
        click.echo(json_module.dumps(report.to_dict(), indent=2))
    else:
        _print_human(report)
    raise SystemExit(0 if report.passed else 1)


def _print_human(report: Report) -> None:
    if not report.findings:
        click.echo(f"OK: {report.files_checked} files checked, no findings.")
        return
    current_path: str | None = None
    for finding in report.findings:
        if finding.path != current_path:
            current_path = finding.path
            click.echo(finding.path)
        tag = _SEVERITY_TAGS[finding.severity]
        click.echo(f"  {tag:<7} {finding.rule_id}  {finding.message}")
        if finding.fix_hint:
            click.echo(f"          hint: {finding.fix_hint}")
    click.echo(
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s),"
        f" {len(report.infos)} info(s) across {report.files_checked} files."
    )
