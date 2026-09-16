from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from langchain_core.runnables.config import ContextThreadPoolExecutor

from reviewer.tracer import NORMAL, Tracer


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_tool_calls_from_langgraphs_own_executor_stay_attached(tmp_path):
    """LangGraph runs a specialist's tools on a *different* thread than the
    specialist, via ContextThreadPoolExecutor. A thread-local loses them."""
    trace = tmp_path / "t.jsonl"
    tracer = Tracer(level=NORMAL, trace_file=trace)

    def specialist(agent, scope):
        with tracer.run(agent, scope):
            # exactly how ToolNode dispatches tool calls
            with ContextThreadPoolExecutor(max_workers=2) as inner:
                list(inner.map(lambda n: tracer.tool_call(n, {}, "a\nb"), ["grep", "read_file"]))
            tracer.finding("high", f"issue in {scope}", scope)

    with ThreadPoolExecutor(max_workers=3) as outer:
        list(outer.map(lambda i: specialist("security", f"f{i}.py"), range(3)))

    ev = _events(trace)
    tool_calls = [e for e in ev if e["kind"] == "tool_call"]
    assert len(tool_calls) == 6
    assert all(e["run"] is not None for e in tool_calls), "tool calls lost their run"
    assert all(e["agent"] == "security" for e in tool_calls)

    # every run must report the 2 tool calls it actually made
    for end in [e for e in ev if e["kind"] == "run_end"]:
        assert end["tool_calls"] == 2, end
        assert end["findings"] == 1


def test_events_are_partitioned_by_run(tmp_path):
    trace = tmp_path / "t.jsonl"
    tracer = Tracer(level=NORMAL, trace_file=trace)

    def specialist(scope):
        with tracer.run("security", scope):
            with ContextThreadPoolExecutor(max_workers=1) as inner:
                list(inner.map(lambda _: tracer.tool_call("grep", {"p": scope}, "x"), [0]))

    with ThreadPoolExecutor(max_workers=3) as outer:
        list(outer.map(specialist, ["a.py", "b.py", "c.py"]))

    ev = _events(trace)
    by_run = {}
    for e in ev:
        if e.get("run"):
            by_run.setdefault(e["run"], []).append(e)
    assert len(by_run) == 3
    for events in by_run.values():
        scopes = {e.get("scope") or e["args"]["p"] for e in events if e["kind"] != "run_end"}
        assert len(scopes) == 1, f"run mixed scopes: {scopes}"
