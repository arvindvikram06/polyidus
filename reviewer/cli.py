from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from reviewer.findings import Severity
from reviewer.git_utils import GitError
from reviewer.orchestrator import run_review
from reviewer.report import render_report
from reviewer.tracer import NORMAL, QUIET, VERBOSE, configure

app = typer.Typer()

_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


@app.callback(invoke_without_command=False)
def main() -> None:
    """Git staged file reviewer."""


@app.command()
def review(
    path: Path = typer.Option(Path("."), "--path", help="Path to the git repository to review."),
    fail_on: str = typer.Option(
        "none",
        "--fail-on",
        help="Exit non-zero when a finding at this severity or above is reported "
        "(none|info|low|medium|high|critical).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Trace agent reasoning and the dispatch plan."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the live trace; print the report only."),
    trace_file: Path | None = typer.Option(
        None, "--trace-file", help="Write a JSONL transcript of every tool call and finding."
    ),
) -> None:
    repo_root = path.resolve()
    configure(level=QUIET if quiet else VERBOSE if verbose else NORMAL, trace_file=trace_file)

    try:
        report = asyncio.run(run_review(repo_root))
    except GitError as exc:
        typer.secho(f"git error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(render_report(report))

    if fail_on.lower() == "none":
        return
    try:
        threshold = _SEVERITY_RANK[Severity(fail_on.lower())]
    except ValueError as exc:
        typer.secho(f"invalid --fail-on value: {fail_on}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if any(_SEVERITY_RANK[f.severity] >= threshold for f in report.findings):
        raise typer.Exit(code=1)


if __name__ == "__main__":  # `python -m reviewer.cli ...` without installing
    app()
