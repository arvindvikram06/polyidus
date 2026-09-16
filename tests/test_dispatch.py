from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reviewer import master
from reviewer.catalog import SpecialistSpec
from reviewer.diff_context import DiffContext
from reviewer.findings import Finding, Severity
from reviewer.llm import LLMError
from reviewer.master import (
    SpecialistTask,
    _run_batch,
    _summarize_batch,
    _validate_tasks,
    build_dispatch_tool,
)

CHANGED = ["app/auth.py", "infra/Dockerfile"]
DIFF = (
    "diff --git a/app/auth.py b/app/auth.py\n+token = request.args['t']\n"
    "diff --git a/infra/Dockerfile b/infra/Dockerfile\n+FROM python:latest\n"
)


def _spec(name: str) -> SpecialistSpec:
    return SpecialistSpec(
        name=name, description=f"{name} reviewer", system_prompt="prompt", prompt_file=Path(f"{name}.md")
    )


SPECIALISTS = {"security": _spec("security"), "infra": _spec("infra")}
CONTEXT = DiffContext(diff_text=DIFF, changed_files=CHANGED)


def _finding(agent: str) -> Finding:
    return Finding(
        subagent=agent,
        file_path="app/auth.py",
        severity=Severity.HIGH,
        title=f"{agent} issue",
        message="m",
        diff_context=DIFF,
    )


# --- validation ------------------------------------------------------------


def test_unknown_specialist_is_rejected_not_executed():
    accepted, rejections = _validate_tasks(
        [SpecialistTask(agent="wizard", task="t")], SPECIALISTS, CHANGED
    )
    assert accepted == []
    assert "unknown specialist 'wizard'" in rejections[0]


def test_paths_outside_the_diff_are_dropped():
    accepted, rejections = _validate_tasks(
        [SpecialistTask(agent="security", task="t", files=["app/auth.py", "/etc/passwd"])],
        SPECIALISTS,
        CHANGED,
    )
    assert accepted[0].files == ["app/auth.py"]
    assert "/etc/passwd" in rejections[0]


def test_batch_is_truncated_at_the_cap(monkeypatch):
    monkeypatch.setattr(master, "MAX_TASKS_PER_BATCH", 2)
    tasks = [SpecialistTask(agent="security", task=f"t{i}") for i in range(5)]
    accepted, rejections = _validate_tasks(tasks, SPECIALISTS, CHANGED)
    assert len(accepted) == 2
    assert "3 task(s) not run" in rejections[-1]


def test_same_specialist_may_appear_twice_with_different_scopes():
    accepted, _ = _validate_tasks(
        [
            SpecialistTask(agent="security", task="auth", files=["app/auth.py"]),
            SpecialistTask(agent="security", task="docker", files=["infra/Dockerfile"]),
        ],
        SPECIALISTS,
        CHANGED,
    )
    assert [t.files for t in accepted] == [["app/auth.py"], ["infra/Dockerfile"]]


# --- fan-out ---------------------------------------------------------------


def test_batch_runs_tasks_concurrently_and_scopes_each_diff(monkeypatch):
    seen: list[tuple[str, str]] = []
    barrier = asyncio.Barrier(2)

    async def fake_review(*, name, diff_text, task, **kwargs):
        seen.append((name, diff_text))
        # Deadlocks (and times out) unless both tasks are genuinely in flight.
        async with asyncio.timeout(5):
            await barrier.wait()
        return [_finding(name)]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)

    runs = asyncio.run(
        _run_batch(
            [
                SpecialistTask(agent="security", task="auth", files=["app/auth.py"]),
                SpecialistTask(agent="infra", task="image", files=["infra/Dockerfile"]),
            ],
            SPECIALISTS,
            CONTEXT,
            [],
            1,
        )
    )

    assert [r.agent for r in runs] == ["security", "infra"], "result order must match request order"
    scoped = dict(seen)
    assert "Dockerfile" not in scoped["security"]
    assert "auth.py" not in scoped["infra"]


def test_concurrent_runs_do_not_share_tracer_state(monkeypatch):
    """Contextvars must isolate per-run state across asyncio tasks, as threads did."""
    from reviewer.tracer import _current_run

    seen: dict[str, str | None] = {}

    async def fake_review(*, name, **kwargs):
        await asyncio.sleep(0.01)
        run = _current_run.get()
        seen[name] = run.agent if run else None
        return [_finding(name)]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)
    asyncio.run(
        _run_batch(
            [
                SpecialistTask(agent="security", task="a"),
                SpecialistTask(agent="infra", task="b"),
            ],
            SPECIALISTS,
            CONTEXT,
            [],
            1,
        )
    )
    assert seen == {"security": "security", "infra": "infra"}


def test_a_failing_specialist_does_not_kill_the_batch(monkeypatch):
    async def fake_review(*, name, **kwargs):
        if name == "security":
            raise LLMError("upstream 503")
        return [_finding(name)]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)

    runs = asyncio.run(
        _run_batch(
            [SpecialistTask(agent="security", task="a"), SpecialistTask(agent="infra", task="b")],
            SPECIALISTS,
            CONTEXT,
            [],
            1,
        )
    )

    assert runs[0].error == "upstream 503" and runs[0].findings == []
    assert runs[1].findings and runs[1].error is None


def test_summary_back_to_master_excludes_full_finding_bodies():
    from reviewer.findings import SpecialistRun

    summary = _summarize_batch(
        [
            SpecialistRun(agent="security", task="t", files=["app/auth.py"], findings=[_finding("security")]),
            SpecialistRun(agent="infra", task="t", files=[], error="boom"),
        ],
        rejections=["dropped x"],
    )
    assert "high: security issue" in summary
    assert "FAILED — boom" in summary
    assert "rejected: dropped x" in summary
    assert DIFF not in summary, "the router must not receive full diffs back"


# --- the tool itself -------------------------------------------------------


def test_dispatch_tool_collects_runs_out_of_band(monkeypatch):
    async def fake_review(**kw):
        return [_finding(kw["name"])]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)
    collected: dict = {}
    dispatch = build_dispatch_tool(CONTEXT, [], collected, SPECIALISTS)

    result = asyncio.run(
        dispatch.ainvoke(
        {
            "args": {"tasks": [{"agent": "security", "task": "check auth", "files": ["app/auth.py"]}]},
            "id": "call-1",
            "name": "dispatch_specialists",
            "type": "tool_call",
            }
        )
    )

    assert "call-1" in collected
    assert collected["call-1"][0].findings[0].title == "security issue"
    assert "security" in str(result)


def test_dispatch_tool_rejects_an_empty_batch():
    dispatch = build_dispatch_tool(CONTEXT, [], {}, SPECIALISTS)
    result = asyncio.run(
        dispatch.ainvoke(
            {"args": {"tasks": []}, "id": "c", "name": "dispatch_specialists", "type": "tool_call"}
        )
    )
    assert "error" in str(result).lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
