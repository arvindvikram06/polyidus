from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reviewer.agents.catalog import SpecialistSpec
from reviewer.agents.llm import LLMError
from reviewer.core import master
from reviewer.core.master import (
    SpecialistTask,
    _run_batch,
    _summarize_batch,
    _validate_tasks,
    build_dispatch_tool,
    run_master_loop,
)
from reviewer.models.diff_context import DiffContext
from reviewer.models.findings import Finding, Severity

CHANGED = ["app/auth.py", "infra/Dockerfile"]
DIFF = (
    "diff --git a/app/auth.py b/app/auth.py\n+token = request.args['t']\n"
    "diff --git a/infra/Dockerfile b/infra/Dockerfile\n+FROM python:latest\n"
)


def _spec(name: str) -> SpecialistSpec:
    return SpecialistSpec(
        name=name,
        description=f"{name} reviewer",
        system_prompt="prompt",
        prompt_file=Path(f"{name}.md"),
        when_to_use=f"when the diff touches {name}",
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
        verified_by="Read app/auth.py:12 - test fixture",
        diff_context=DIFF,
    )


# --- validation ------------------------------------------------------------


def test_unknown_specialist_is_rejected_not_executed():
    accepted, rejections = _validate_tasks(
        [SpecialistTask(agent="wizard", task="check the token check in login() against the session store it reads")], SPECIALISTS, CHANGED
    )
    assert accepted == []
    assert "unknown specialist 'wizard'" in rejections[0]


def test_paths_outside_the_diff_are_dropped():
    accepted, rejections = _validate_tasks(
        [SpecialistTask(agent="security", task="check the token check in login() against the session store it reads", files=["app/auth.py", "/etc/passwd"])],
        SPECIALISTS,
        CHANGED,
    )
    assert accepted[0].files == ["app/auth.py"]
    assert "/etc/passwd" in rejections[0]


def test_batch_is_truncated_at_the_cap(monkeypatch):
    monkeypatch.setattr(master, "MAX_TASKS_PER_BATCH", 2)
    tasks = [
        SpecialistTask(agent="security", task=f"check the token check in login() against the session store, case {i}")
        for i in range(5)
    ]
    accepted, rejections = _validate_tasks(tasks, SPECIALISTS, CHANGED)
    assert len(accepted) == 2
    assert "3 task(s) not run" in rejections[-1]


def test_same_specialist_may_appear_twice_with_different_scopes():
    accepted, _ = _validate_tasks(
        [
            SpecialistTask(agent="security", task="check the token check in login() against the session store it reads", files=["app/auth.py"]),
            SpecialistTask(agent="security", task="check the base image tag and whether the container runs as root", files=["infra/Dockerfile"]),
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
                SpecialistTask(agent="security", task="check the token check in login() against the session store it reads", files=["app/auth.py"]),
                SpecialistTask(agent="infra", task="check the base image tag and whether the container runs as root", files=["infra/Dockerfile"]),
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
    from reviewer.core.tracer import _current_run

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
                SpecialistTask(agent="security", task="check the token check in login() against the session store it reads"),
                SpecialistTask(agent="infra", task="check the base image tag and whether the container runs as root"),
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
            [SpecialistTask(agent="security", task="check the token check in login() against the session store it reads"), SpecialistTask(agent="infra", task="check the base image tag and whether the container runs as root")],
            SPECIALISTS,
            CONTEXT,
            [],
            1,
        )
    )

    assert runs[0].error == "upstream 503" and runs[0].findings == []
    assert runs[1].findings and runs[1].error is None


def test_summary_back_to_master_excludes_full_finding_bodies():
    from reviewer.models.findings import SpecialistRun

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
            "args": {"tasks": [{"agent": "security", "task": "check the token check in login() against the session store it reads", "files": ["app/auth.py"]}]},
            "id": "call-1",
            "name": "dispatch_specialists",
            "type": "tool_call",
            }
        )
    )

    assert "call-1" in collected
    assert collected["call-1"][0].findings[0].title == "security issue"
    assert "security" in str(result)


def test_a_non_llm_error_also_does_not_kill_the_batch(monkeypatch):
    """Only LLMError used to be caught; anything else cancelled the siblings."""

    async def fake_review(*, name, **kwargs):
        if name == "security":
            raise ValueError("bad schema")
        return [_finding(name)]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)

    runs = asyncio.run(
        _run_batch(
            [SpecialistTask(agent="security", task="check the token check in login() against the session store it reads"), SpecialistTask(agent="infra", task="check the base image tag and whether the container runs as root")],
            SPECIALISTS,
            CONTEXT,
            [],
            1,
        )
    )

    assert runs[0].error == "ValueError: bad schema"
    assert runs[1].findings and runs[1].error is None, "sibling work must survive"


def test_a_failure_in_the_tracer_itself_does_not_kill_the_batch(monkeypatch):
    """`tracer.run` sits outside _execute_task's try; the gather site is the backstop.

    It writes to the trace file, so a disk or permission error raises there —
    which the per-task handler structurally cannot catch.
    """
    real_run = master.tracer.run
    calls = {"n": 0}

    def flaky_run(agent: str, scope: str):
        calls["n"] += 1
        if agent == "security":
            raise OSError("trace file unwritable")
        return real_run(agent, scope)

    async def fake_review(*, name, **kwargs):
        await asyncio.sleep(0.01)
        return [_finding(name)]

    monkeypatch.setattr(master.tracer, "run", flaky_run)
    monkeypatch.setattr(master, "run_subagent_review", fake_review)

    runs = asyncio.run(
        _run_batch(
            [SpecialistTask(agent="security", task="check the token check in login() against the session store it reads"), SpecialistTask(agent="infra", task="check the base image tag and whether the container runs as root")],
            SPECIALISTS,
            CONTEXT,
            [],
            1,
        )
    )

    assert [r.agent for r in runs] == ["security", "infra"], "order must still match the request"
    assert runs[0].error == "OSError: trace file unwritable"
    assert runs[1].findings and runs[1].error is None


def test_cancellation_is_not_swallowed_as_a_specialist_error(monkeypatch):
    """A cancelled review must not report itself as a completed one."""

    async def fake_review(*, name, **kwargs):
        if name == "security":
            raise asyncio.CancelledError()
        return [_finding(name)]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _run_batch(
                [
                    SpecialistTask(agent="security", task="check the token check in login() against the session store it reads"),
                    SpecialistTask(agent="infra", task="check the base image tag and whether the container runs as root"),
                ],
                SPECIALISTS,
                CONTEXT,
                [],
                1,
            )
        )


# --- partial-result salvage ------------------------------------------------


def test_master_failure_after_a_batch_keeps_the_completed_runs(monkeypatch):
    """A dead master must not bin specialist work that already finished.

    The expensive failure is the master's own second call 429ing while batch
    one's findings sit in `collected`.
    """

    async def fake_review(**kw):
        return [_finding(kw["name"])]

    monkeypatch.setattr(master, "run_subagent_review", fake_review)

    class _Agent:
        async def ainvoke(self, _state):
            # Stand in for the master: dispatch one batch, then die.
            await dispatch_holder["tool"].ainvoke(
                {
                    "args": {"tasks": [{"agent": "security", "task": "check the token check in login() against the session store it reads"}]},
                    "id": "call-1",
                    "name": "dispatch_specialists",
                    "type": "tool_call",
                }
            )
            raise RuntimeError("rate limited")

    dispatch_holder: dict = {}
    real_build = master.build_dispatch_tool

    def capture(*args, **kwargs):
        dispatch_holder["tool"] = real_build(*args, **kwargs)
        return dispatch_holder["tool"]

    monkeypatch.setattr(master, "build_dispatch_tool", capture)
    monkeypatch.setattr(master, "create_agent", lambda **kw: _Agent())
    monkeypatch.setattr(master, "get_chat_model", lambda m: None)

    result = asyncio.run(run_master_loop(CONTEXT, [], specialists=SPECIALISTS))

    assert result.aborted is not None and "rate limited" in result.aborted
    assert [f.title for f in result.findings] == ["security issue"], "batch 1 must survive"
    assert [e.subagent for e in result.trace] == ["security"]


def test_master_failure_with_nothing_collected_still_raises(monkeypatch):
    """No partial work to save means the old fatal behaviour is correct."""

    class _Agent:
        async def ainvoke(self, _state):
            raise RuntimeError("model unreachable")

    monkeypatch.setattr(master, "create_agent", lambda **kw: _Agent())
    monkeypatch.setattr(master, "get_chat_model", lambda m: None)

    with pytest.raises(LLMError, match="model unreachable"):
        asyncio.run(run_master_loop(CONTEXT, [], specialists=SPECIALISTS))


def test_dispatch_tool_rejects_an_empty_batch():
    dispatch = build_dispatch_tool(CONTEXT, [], {}, SPECIALISTS)
    result = asyncio.run(
        dispatch.ainvoke(
            {"args": {"tasks": []}, "id": "c", "name": "dispatch_specialists", "type": "tool_call"}
        )
    )
    assert "error" in str(result).lower()



# --- a failed specialist is not a finding -----------------------------------


def test_a_failed_specialist_is_marked_so_publishers_can_exclude_it():
    """`_unparseable_finding` fabricates a Finding when a specialist dies.

    The CLI shows it on purpose: someone reading a terminal report should know
    a specialist produced nothing, rather than silently seeing fewer findings.
    A bot must do the opposite — `file_path` on that record is just the first
    changed file, so publishing it puts a comment on a file with nothing wrong
    with it. That happened on the first real run against test-proj#1.

    The flag is what lets the two consumers disagree.
    """
    from reviewer.agents.subagents.base import _unparseable_finding

    failure = _unparseable_finding("architecture", "diff text", ["a.cs"], [])

    assert failure.is_failure is True
    assert failure.severity is Severity.INFO
    assert "not verified" in failure.verified_by


def test_a_real_finding_is_not_marked_as_a_failure():
    """The flag must default off, or every finding would be filtered away."""
    finding = Finding(
        subagent="security",
        file_path="src/Repo.cs",
        severity=Severity.CRITICAL,
        title="SQL injection",
        message="supplierCode is concatenated into raw SQL",
        verified_by="Read ProductRepository.cs:57",
        diff_context="",
    )
    assert finding.is_failure is False


# --- the shared severity rubric ---------------------------------------------


def test_every_specialist_receives_the_shared_severity_rubric():
    """Nothing defined what `critical` meant, so each specialist invented one.

    On the first real run the same problem — credentials hardcoded and logged —
    came back `critical` from `security` and `high` from `coding_standards`.
    The author cannot tell which to believe.

    The rubric is composed in rather than copied into each prompt file, because
    four copies become four rubrics.
    """
    from reviewer.agents.catalog import load_specialists
    from reviewer.agents.subagents.rules import compose_system_prompt

    specialists = load_specialists()
    assert specialists, "no specialists loaded"

    for name, spec in specialists.items():
        composed = compose_system_prompt(spec.system_prompt)
        for level in ("critical", "high", "medium", "low", "info"):
            assert level in composed, f"{name} did not receive the {level!r} definition"
        # The specialist's own instructions must survive composition.
        assert spec.system_prompt.strip()[:40] in composed, f"{name} lost its own prompt"


def test_missing_shared_rules_fails_loudly_rather_than_silently():
    """A specialist with no rubric produces plausible, inconsistent severities.

    That is invisible until someone compares two findings on a pull request, so
    the absence has to raise here instead of degrading to a default.
    """
    from reviewer.agents.subagents import rules

    rules.shared_rules.cache_clear()
    original = rules._RULES_PATH
    try:
        rules._RULES_PATH = original.parent / "definitely-not-here.md"
        with pytest.raises(rules.RulesMissingError, match="not found"):
            rules.shared_rules()
    finally:
        rules._RULES_PATH = original
        rules.shared_rules.cache_clear()

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- task quality ------------------------------------------------------------


def test_a_vague_task_is_rejected_rather_than_dispatched():
    """The strongest measured lever on what a review finds.

    Same specialist, same file: 7 findings for a task naming the file and what
    to examine, 2 for "review these files for coding_standards problems". A
    specialist spends its whole tool budget on whatever question it is given,
    so a bad question costs the same as a good one and returns a third as much.

    Rejected rather than accepted-and-logged because the master is fed its own
    rejections — told the task was thin, it writes a better one and
    re-dispatches.
    """
    accepted, rejections = _validate_tasks(
        [SpecialistTask(agent="security", task="review this")], SPECIALISTS, CHANGED
    )

    assert accepted == []
    assert "too vague" in rejections[0]
    assert "security" in rejections[0], "the master needs to know which task to redo"


def test_a_task_that_only_restates_the_specialist_name_is_rejected():
    """"Review these files for security problems" tells the security specialist
    nothing it did not already know from being the security specialist."""
    accepted, rejections = _validate_tasks(
        [SpecialistTask(agent="security", task="Review these files for security problems.")],
        SPECIALISTS,
        CHANGED,
    )

    assert accepted == []
    assert "restates the specialist" in rejections[0]


def test_a_task_naming_what_to_examine_is_accepted():
    accepted, rejections = _validate_tasks(
        [
            SpecialistTask(
                agent="security",
                task=(
                    "ProcessReturnAsync restocks products and computes a refund. "
                    "Check the refund against what OrderItem records."
                ),
            )
        ],
        SPECIALISTS,
        CHANGED,
    )

    assert len(accepted) == 1
    assert rejections == []
