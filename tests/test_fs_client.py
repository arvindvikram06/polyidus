from __future__ import annotations

import json

import pytest
from langchain_core.tools import StructuredTool

from reviewer import fs_client
from reviewer.fs_client import FsBackendError, _traced, repo_tools
from reviewer.tracer import NORMAL, Tracer

SCHEMA = {
    "type": "object",
    "properties": {"pattern": {"type": "string"}, "repo": {"type": "string"}},
    "required": ["pattern"],
}


def _fake_mcp_tool(result=None, raises=None) -> StructuredTool:
    """Stands in for a tool returned by MCPAdapter.

    The protocol itself is covered in test_fs_server.py; what matters here is
    the wrapper this module puts around it.
    """

    async def call(**kwargs):
        if raises:
            raise raises
        return result

    return StructuredTool(name="grep_tool", description="d", args_schema=SCHEMA, coroutine=call)


@pytest.mark.anyio
async def test_wrapper_records_the_call_against_the_current_run(tmp_path, monkeypatch):
    trace = tmp_path / "t.jsonl"
    tracer = Tracer(level=NORMAL, trace_file=trace)
    monkeypatch.setattr(fs_client, "tracer", tracer)

    tool = _traced(_fake_mcp_tool(result=[{"type": "text", "text": "a.py:1: hit"}]))
    with tracer.run("security", "a.py"):
        assert await tool.ainvoke({"pattern": "hit"})

    events = [json.loads(line) for line in trace.read_text().splitlines()]
    call = next(e for e in events if e["kind"] == "tool_call")
    assert call["agent"] == "security", "tool call lost its run crossing MCP"
    assert call["tool"] == "grep_tool"
    assert next(e for e in events if e["kind"] == "run_end")["tool_calls"] == 1


@pytest.mark.anyio
async def test_unset_optionals_are_dropped_so_server_defaults_apply():
    seen: dict = {}

    async def call(**kwargs):
        seen.update(kwargs)
        return []

    inner = StructuredTool(name="grep_tool", description="d", args_schema=SCHEMA, coroutine=call)
    await _traced(inner).ainvoke({"pattern": "x", "repo": None})
    assert seen == {"pattern": "x"}


@pytest.mark.anyio
async def test_a_failing_tool_is_traced_then_propagates(tmp_path, monkeypatch):
    """Transport failures are not something an agent can act on, so they raise —
    but the trace must still show the attempt that ended the run."""
    trace = tmp_path / "t.jsonl"
    tracer = Tracer(level=NORMAL, trace_file=trace)
    monkeypatch.setattr(fs_client, "tracer", tracer)

    tool = _traced(_fake_mcp_tool(raises=ConnectionError("server went away")))
    with tracer.run("security", "a.py"):
        with pytest.raises(ConnectionError):
            await tool.ainvoke({"pattern": "x"})

    events = [json.loads(line) for line in trace.read_text().splitlines()]
    call = next(e for e in events if e["kind"] == "tool_call")
    assert "ConnectionError" in call["result_excerpt"]


@pytest.mark.anyio
async def test_unreachable_server_fails_with_an_actionable_message(monkeypatch):
    monkeypatch.setattr(fs_client, "FS_MCP_URL", "http://127.0.0.1:59999/mcp")
    with pytest.raises(FsBackendError, match="cannot reach"):
        await repo_tools()
