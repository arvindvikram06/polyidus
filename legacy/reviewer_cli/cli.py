from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from dotenv import find_dotenv, load_dotenv

# Loaded at the entry point, before config is imported: `reviewer/config.py`
# reads os.environ at import time, so anything loaded later is already too late.
# Two locations, because the reviewer is normally run from *inside the repository
# under review*, not from its own checkout: the working directory first, then
# the reviewer's own install. Real environment variables win over both, so CI
# can override without editing a file.
load_dotenv(find_dotenv(usecwd=True), override=False)
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from legacy.reviewer_cli.github.publish import StaleReviewError, post_pending_review
from legacy.reviewer_cli.mcp.github_client import GitHubMcpError
from legacy.reviewer_cli.orchestrator import run_review
from legacy.reviewer_cli.report import render_report
from legacy.reviewer_cli.session import load_session
from legacy.reviewer_cli.sources.base import ReviewTarget
from legacy.reviewer_cli.sources.github_pr import GitHubPRSource, list_open_pulls, parse_remote
from legacy.reviewer_cli.sources.staged import StagedGitSource
from legacy.reviewer_cli.utils.git_utils import GitError, get_remote_url
from reviewer.core.tracer import NORMAL, QUIET, VERBOSE, configure
from reviewer.models.findings import Severity

app = typer.Typer()

_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


def _identity(repo_root: Path, remote: str) -> tuple[str, str]:
    try:
        return parse_remote(get_remote_url(repo_root, remote))
    except (GitError, GitHubMcpError) as exc:
        _fail(f"cannot determine the GitHub repository: {exc}")
        raise  # unreachable; keeps type checkers happy


@app.callback(invoke_without_command=False)
def main() -> None:
    """Multi-agent reviewer for git changes and GitHub pull requests."""


@app.command()
def review(
    path: Path = typer.Option(Path("."), "--path", help="Path to the git repository."),
    pr: int | None = typer.Option(
        None, "--pr", help="Review this pull request instead of staged changes."
    ),
    remote: str = typer.Option("origin", "--remote", help="Remote to resolve the PR against."),
    fail_on: str = typer.Option(
        "none",
        "--fail-on",
        help="Exit non-zero when a finding at this severity or above is reported "
        "(none|info|low|medium|high|critical).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Trace agent reasoning."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print the report only."),
    trace_file: Path | None = typer.Option(
        None, "--trace-file", help="Write a JSONL transcript of every tool call and finding."
    ),
) -> None:
    """Review staged changes, or a pull request with --pr."""
    repo_root = path.resolve()
    configure(level=QUIET if quiet else VERBOSE if verbose else NORMAL, trace_file=trace_file)

    if pr is not None:
        owner, repo = _identity(repo_root, remote)
        source = GitHubPRSource(owner, repo, pr)
    else:
        source = StagedGitSource(repo_root)

    try:
        report = asyncio.run(run_review(repo_root, source))
    except GitError as exc:
        _fail(f"git error: {exc}")
    except GitHubMcpError as exc:
        _fail(f"github error: {exc}")

    typer.echo(render_report(report))

    if pr is not None and report.findings:
        typer.echo(
            f"\nReview saved. Post it as a pending review with:\n"
            f"  reviewer post --pr {pr}"
        )

    if fail_on.lower() == "none":
        return
    try:
        threshold = _SEVERITY_RANK[Severity(fail_on.lower())]
    except ValueError:
        _fail(f"invalid --fail-on value: {fail_on}")
        return

    if any(_SEVERITY_RANK[f.severity] >= threshold for f in report.findings):
        raise typer.Exit(code=1)


@app.command("pr")
def list_prs(
    path: Path = typer.Option(Path("."), "--path", help="Path to the git repository."),
    remote: str = typer.Option("origin", "--remote", help="Remote to resolve against."),
) -> None:
    """List the open pull requests on this repository's GitHub remote."""
    repo_root = path.resolve()
    owner, repo = _identity(repo_root, remote)

    try:
        pulls = asyncio.run(list_open_pulls(owner, repo))
    except GitHubMcpError as exc:
        _fail(f"github error: {exc}")
        return

    if not pulls:
        typer.echo(f"No open pull requests on {owner}/{repo}.")
        return

    typer.echo(f"Open pull requests on {owner}/{repo}:\n")
    for pull in pulls:
        flag = " [draft]" if pull.get("draft") else ""
        author = (pull.get("user") or {}).get("login", "unknown")
        head = (pull.get("head") or {}).get("ref", "?")
        base = (pull.get("base") or {}).get("ref", "?")
        typer.echo(f"  #{pull.get('number'):<5} {pull.get('title', '')}{flag}")
        typer.echo(f"         by {author} · {head} -> {base}")
    typer.echo("\nReview one with:  reviewer review --pr <number>")


@app.command()
def post(
    pr: int = typer.Option(..., "--pr", help="Pull request to post the saved review to."),
    path: Path = typer.Option(Path("."), "--path", help="Path to the git repository."),
    remote: str = typer.Option("origin", "--remote", help="Remote to resolve against."),
    min_severity: str = typer.Option(
        "low", "--min-severity", help="Do not post findings below this severity."
    ),
    allow_stale: bool = typer.Option(
        False, "--allow-stale", help="Post even though the PR moved since the review ran."
    ),
) -> None:
    """Post a saved review to its pull request as a PENDING review.

    Pending means only you can see it. Open the PR, delete anything wrong, then
    click Submit — the author is not notified until you do.
    """
    repo_root = path.resolve()
    owner, repo = _identity(repo_root, remote)

    target = ReviewTarget(kind="pull_request", owner=owner, repo=repo, number=pr)
    report = load_session(repo_root, target)
    if report is None:
        _fail(f"no saved review for {owner}/{repo}#{pr}. Run: reviewer review --pr {pr}")
        return
    if not report.findings:
        typer.echo("That review produced no findings; nothing to post.")
        return

    try:
        severity = Severity(min_severity.lower())
    except ValueError:
        _fail(f"invalid --min-severity value: {min_severity}")
        return

    try:
        result = asyncio.run(
            post_pending_review(report, min_severity=severity, allow_stale=allow_stale)
        )
    except StaleReviewError as exc:
        _fail(f"{exc}\nPass --allow-stale to post anyway (line numbers may be wrong).")
        return
    except GitHubMcpError as exc:
        _fail(f"github error: {exc}")
        return

    typer.secho(
        f"Pending review created with {result['comments_added']} comment(s) — "
        "only you can see it.",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  {result.get('url') or ''}")
    for title, error in result.get("rejections", []):
        typer.secho(f"  ! GitHub rejected '{title}': {error}", fg=typer.colors.YELLOW)
    typer.echo("Review the comments there, delete any that are wrong, then click Submit.")


if __name__ == "__main__":  # `python -m legacy.reviewer_cli.cli ...` without installing
    app()
