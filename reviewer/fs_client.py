"""Supplies specialists with repository tools from the MCP server.

The orchestrator's ``dispatch_specialists`` stays native — it is the reviewer's
own machinery and has no business crossing a process boundary. The leaf tools a
specialist uses to read the repository come from the MCP server, which owns both
the implementation and the sandbox.

Tracing stays on this side of the wire. ``tracer.tool_call`` fires in the wrapper
below, inside the calling specialist's own context, so every call is attributed
to its run. The server has no idea which specialist is asking, and does not need
to.
"""

from __future__ import annotations

from typing import Any

from langchain_core._api import suppress_langchain_beta_warning
from langchain_core.tools import BaseTool, StructuredTool

with suppress_langchain_beta_warning():
    from langchain.mcp import MCPAdapter

from reviewer.config import FS_MCP_URL
from reviewer.tracer import tracer


class FsBackendError(Exception):
    """The repository MCP server could not be reached."""


def _as_text(result: Any) -> str:
    """Flatten a tool result into a line the tracer can summarise."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(block.get("text", "") for block in result if isinstance(block, dict))
    return str(result)


def _traced(tool: BaseTool) -> BaseTool:
    """Wrap an MCP tool so the tracer records the call against the current run."""

    async def call(**kwargs: Any) -> Any:
        # Unset optionals are dropped so the server applies its own defaults.
        args = {key: value for key, value in kwargs.items() if value is not None}
        try:
            result = await tool.ainvoke(args)
        except Exception as exc:
            # Transport failures are not something an agent can act on, so they
            # propagate — but the trace should still show the attempt.
            tracer.tool_call(tool.name, args, f"error: {type(exc).__name__}: {exc}")
            raise
        tracer.tool_call(tool.name, args, _as_text(result))
        return result

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=call,
    )


async def repo_tools() -> list[BaseTool]:
    """Discover the repository tools for one review.

    FastMCP clients are reentrant, so each tool opens its own connection when
    called and there is no session to keep alive. Listing the tools up front
    doubles as the reachability check, turning a dead server into one clear
    message instead of an identical failure inside every specialist.
    """
    try:
        tools = await MCPAdapter(FS_MCP_URL).list_tools()
    except Exception as exc:  
        raise FsBackendError(
            f"cannot reach the repository MCP server at {FS_MCP_URL}: "
            f"{type(exc).__name__}: {exc}. Start it with `reviewer-fs-server <repo>`."
        ) from exc

    if not tools:
        raise FsBackendError(f"MCP server at {FS_MCP_URL} exposes no tools")
    return [_traced(tool) for tool in tools]
