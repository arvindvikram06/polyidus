"""Repository tools backed by a checkout, handed to specialists.

Implementations live in ``reviewer.sandbox.files``. Called in-process rather
than across a server: the bot already holds the checkout, and a second process
would add a lifetime to manage for no isolation benefit.

A specialist gets three functions that read files under one directory. No
network, no write, no way to reach another repository — the boundary is
`_resolve_within` rejecting a path that escapes the root.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from reviewer.core.tracer import tracer
from reviewer.sandbox import files as fs


def _call(name: str, fn: Callable[..., str], **kwargs: Any) -> str:
    """Run one filesystem function, trace it, and return errors as text.

    Errors come back as text, not exceptions: a specialist that asked for a
    missing file should get something it can act on, not an ended run.
    """
    args = {key: value for key, value in kwargs.items() if value is not None}
    try:
        result = fn(**args)
    except fs.ToolError as exc:
        result = f"error: {exc}"
    except OSError as exc:
        result = f"error: {type(exc).__name__}: {exc}"
    tracer.tool_call(name, args, result)
    return result


def repo_tools(repo_root: Path) -> list[BaseTool]:
    """The three tools a specialist gets, bound to one checkout."""
    root = Path(repo_root)

    @tool(parse_docstring=True)
    async def read_file(
        path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        """Read a file from the repository at the exact commit under review.

        Use this whenever a finding depends on something the diff does not show —
        the definition of a type, what a base class does, whether a field exists.
        A claim about code you have not opened is unverified, and unverified
        findings must be reported at `info` severity.

        Args:
            path: Repository-relative path to the file, e.g.
                `src/OrderApi.Domain/Entities/Product.cs`. Required.
            start_line: First line to return. Omit to start at the top.
            end_line: Last line to return. Omit to read to the end.
        """
        return _call(
            "read_file", fs.read_file, repo_root=root,
            path=path, start_line=start_line, end_line=end_line,
        )

    @tool(parse_docstring=True)
    async def search_code(pattern: str, path_glob: str = "**/*") -> str:
        """Search the repository for a regular expression.

        Returns `file:line: text` for each match. This searches the actual code
        under review, including everything the pull request adds — use it to find
        where a symbol is defined when you do not know the path, then read that
        file.

        Args:
            pattern: Regular expression to search for, e.g. `class Product\\b`
                or `SupplierCode`. Required.
            path_glob: Restrict the search, e.g. `src/**/*.cs`. Narrow this when
                you can — searching everything is slower and returns more noise.
        """
        return _call(
            "search_code", fs.grep, repo_root=root,
            pattern=pattern, path_glob=path_glob,
        )

    @tool(parse_docstring=True)
    async def list_directory(path: str = ".") -> str:
        """List one directory of the repository. Directories end with `/`.

        Use it to find your way when you do not know a path: start at `.`, then
        call again with a subdirectory to go deeper. Cheaper than guessing a path
        and getting an error.

        Args:
            path: Repository-relative directory, e.g. `src` or `.` for the top
                level.
        """
        return _call("list_directory", fs.list_dir, repo_root=root, path=path)

    return [read_file, search_code, list_directory]
