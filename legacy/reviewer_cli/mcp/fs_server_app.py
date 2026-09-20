"""An MCP server exposing the reviewer's repository tools over HTTP.

The sandbox is NOT reimplemented here. ``tools.py`` next door owns path containment,
the deny-list, symlink checks and output caps, and is covered by its own tests;
this module is a protocol wrapper over those same functions. Anything that
tightens the sandbox there tightens it here for free.

Tool names match what the reviewer's specialists already call in-process, so
pointing an agent at this server is a change of transport, not of behaviour.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from legacy.reviewer_cli.mcp.roots import RootError, RootRegistry
from reviewer.sandbox.files import ToolError, grep, list_dir, read_file

SERVER_INSTRUCTIONS = """\
Read-only access to source repositories for code review.

Every path is confined to the configured repository roots: absolute paths
outside them, `..` traversal, and symlinks pointing out are refused. Version
control internals and credential files (.git, .env, .ssh, .aws, .reviewer,
node_modules) are refused even from inside a repository.

Nothing here can modify the filesystem.\
"""

# Descriptions are written out rather than taken from docstrings so that what the
# model receives is exactly this text, without source indentation leaking in.
_REPO_ARG = (
    "`repo` selects the repository when the server exposes more than one; omit it "
    "when it exposes only one."
)

GREP_DESCRIPTION = f"""\
Search repository files for a Python regular expression.

Returns one `path:line: text` per match, relative to the repository root, or the
literal string `no matches`.

Results stop at 50 matches, so prefer a narrow `path_glob` (e.g. `**/*.py`,
`src/**/*.java`) over scanning everything. Files under excluded directories are
skipped silently and never appear in results.

{_REPO_ARG}\
"""

READ_FILE_DESCRIPTION = f"""\
Read a file from the repository, optionally a 1-based inclusive line range.

`path` is relative to the repository root. Output is truncated at 2000 lines
with a marker; request a narrower range for anything larger.

{_REPO_ARG}\
"""

LIST_DIR_DESCRIPTION = f"""\
List the entries of a directory in the repository.

Directories are suffixed with `/`. Excluded directories are omitted, so the
listing may be shorter than what is on disk.

{_REPO_ARG}\
"""


logger = logging.getLogger("reviewer.fs_server")


def _fmt_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if v is not None)


def _serve(name: str, args: dict[str, Any], call: Callable[[], str]) -> str:
    """Run one tool call, logging what was asked and how it was answered.

    This is the server's own record, and it is deliberately separate from the
    reviewer's tracer: the tracer knows *which specialist* called, this knows
    *what this server was asked to do*. A refusal is logged at WARNING — the
    sandbox is the point of this server, so exercising it must be visible even
    though the protocol reports it as a successful call.
    """
    started = time.monotonic()
    try:
        result = call()
        refused = False
    except (ToolError, RootError) as exc:
        result, refused = f"error: {exc}", True

    elapsed_ms = (time.monotonic() - started) * 1000
    if refused:
        logger.warning("%s(%s) REFUSED %s [%.0fms]", name, _fmt_args(args), result, elapsed_ms)
    else:
        logger.info(
            "%s(%s) -> %d line(s) [%.0fms]",
            name, _fmt_args(args), len(result.splitlines()), elapsed_ms,
        )
    return result


def build_server(registry: RootRegistry) -> MCPServer:
    server = MCPServer(
        name="reviewer-fs",
        title="Reviewer filesystem",
        instructions=SERVER_INSTRUCTIONS,
    )

    def _repo(repo: str | None) -> Path:
        """Resolve and authorise a client-supplied repository path."""
        return registry.resolve(repo)

    @server.tool(title="Search files", description=GREP_DESCRIPTION)
    def grep_tool(pattern: str, path_glob: str = "**/*", repo: str | None = None) -> str:
        args = {"pattern": pattern, "path_glob": path_glob, "repo": repo}
        return _serve("grep_tool", args, lambda: grep(_repo(repo), pattern, path_glob))

    @server.tool(title="Read a file", description=READ_FILE_DESCRIPTION)
    def read_file_tool(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        repo: str | None = None,
    ) -> str:
        args = {"path": path, "start_line": start_line, "end_line": end_line, "repo": repo}
        return _serve(
            "read_file_tool", args, lambda: read_file(_repo(repo), path, start_line, end_line)
        )

    @server.tool(title="List a directory", description=LIST_DIR_DESCRIPTION)
    def list_dir_tool(path: str = ".", repo: str | None = None) -> str:
        args = {"path": path, "repo": repo}
        return _serve("list_dir_tool", args, lambda: list_dir(_repo(repo), path))

    return server
