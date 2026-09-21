"""Repository tools backed by a checkout, handed to specialists.

The implementations are ``reviewer.sandbox.files`` — the same path
containment, deny-list and output caps the local CLI already uses, and the
reason that module was written with no framework imports. They are called
in-process here rather than through the MCP server, because the bot already
holds the checkout and a second process would add a lifetime to manage for no
isolation benefit: these functions only read, and they refuse to read outside
the repository root.

The capability boundary is unchanged, and arguably tighter than before. A
specialist gets three functions that read files under one directory. There is
no network, no write, and no way to reach another repository — where the MCP
version depended on GitHub honouring a `/readonly` URL, this depends on
`_resolve_within` rejecting a path that escapes the root.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from reviewer.core.tracer import tracer
from reviewer.sandbox import files as fs


class _ReadArgs(BaseModel):
    path: str = Field(
        description=(
            "Repository-relative path to the file, e.g. "
            "`src/OrderApi.Domain/Entities/Product.cs`. Required."
        )
    )
    start_line: int | None = Field(
        default=None, description="First line to return. Omit to start at the top."
    )
    end_line: int | None = Field(
        default=None, description="Last line to return. Omit to read to the end."
    )


class _GrepArgs(BaseModel):
    pattern: str = Field(
        description=(
            "Regular expression to search for, e.g. `class Product\\b` or "
            "`SupplierCode`. Required."
        )
    )
    path_glob: str = Field(
        default="**/*",
        description=(
            "Restrict the search, e.g. `src/**/*.cs`. Narrow this when you can — "
            "searching everything is slower and returns more noise."
        ),
    )


class _ListArgs(BaseModel):
    path: str = Field(
        default=".",
        description="Repository-relative directory, e.g. `src` or `.` for the top level.",
    )


_READ_DESCRIPTION = (
    "Read a file from the repository at the exact commit under review.\n"
    "\n"
    "Use this whenever a finding depends on something the diff does not show — "
    "the definition of a type, what a base class does, whether a field exists. "
    "A claim about code you have not opened is unverified, and unverified "
    "findings must be reported at `info` severity.\n"
    "\n"
    "Pass `start_line` and `end_line` to read part of a large file."
)

_GREP_DESCRIPTION = (
    "Search the repository for a regular expression. Returns `file:line: text` "
    "for each match.\n"
    "\n"
    "This searches the actual code under review, including everything the pull "
    "request adds — use it to find where a symbol is defined when you do not "
    "know the path, then read that file.\n"
    "\n"
    "Narrow with `path_glob` when you can."
)

_LIST_DESCRIPTION = (
    "List one directory of the repository. Directories end with `/`.\n"
    "\n"
    "Use it to find your way when you do not know a path: start at `.`, then "
    "call again with a subdirectory to go deeper. Cheaper than guessing a path "
    "and getting an error."
)


def _wrap(name: str, description: str, args_schema: type[BaseModel], fn) -> BaseTool:
    """Trace a filesystem function and present it as a tool.

    Errors are returned as text rather than raised. A specialist that asked for
    a file which does not exist should get an answer it can act on — try a
    different path — not an exception that ends its run.
    """

    async def call(**kwargs) -> str:
        args = {key: value for key, value in kwargs.items() if value is not None}
        try:
            result = fn(**args)
        except fs.ToolError as exc:
            result = f"error: {exc}"
        except OSError as exc:
            result = f"error: {type(exc).__name__}: {exc}"
        tracer.tool_call(name, args, result)
        return result

    return StructuredTool(
        name=name, description=description, args_schema=args_schema, coroutine=call
    )


def repo_tools(repo_root: Path) -> list[BaseTool]:
    """The three tools a specialist gets, bound to one checkout."""
    root = Path(repo_root)
    return [
        _wrap(
            "read_file",
            _READ_DESCRIPTION,
            _ReadArgs,
            lambda **kw: fs.read_file(root, **kw),
        ),
        _wrap(
            "search_code",
            _GREP_DESCRIPTION,
            _GrepArgs,
            lambda **kw: fs.grep(root, **kw),
        ),
        _wrap(
            "list_directory",
            _LIST_DESCRIPTION,
            _ListArgs,
            lambda **kw: fs.list_dir(root, **kw),
        ),
    ]
