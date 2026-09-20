from __future__ import annotations

import asyncio
from typing import Annotated, Any

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from pydantic import BaseModel, Field

from reviewer.agents.catalog import SpecialistSpec, format_catalog_summary, load_specialists
from reviewer.agents.llm import LLMError, get_chat_model
from reviewer.agents.subagents.base import run_subagent_review
from reviewer.config import (
    DEFAULT_MASTER_MODEL,
    MASTER_DISPATCH_CAP,
    MAX_FANOUT,
    MAX_TASKS_PER_BATCH,
)
from reviewer.core.tracer import tracer
from reviewer.models.diff_context import DiffContext, slice_diff
from reviewer.models.findings import Finding, SpecialistRun

MASTER_SYSTEM_PROMPT = (
    """You are the lead reviewer coordinating a code review of a developer's staged git changes. You do not review code yourself. Your job is to split this diff into review tasks and assign each one to a specialist.

AVAILABLE SPECIALISTS:
{catalog_summary}

INPUT
You receive the list of changed files and the full staged diff. There is no pre-analysis step — you are the first and only judgment call on how this diff should be reviewed.

HOW TO DISPATCH
You have one tool: dispatch_specialists(tasks). It takes a LIST of tasks and runs them CONCURRENTLY, then returns one result line per task.

Put every task you can plan up front into a SINGLE dispatch call. Tasks that do not depend on each other's results must go in the same call — issuing them one at a time wastes time and budget for no benefit.

Each task has three fields:
  agent  — a specialist name from the catalog above.
  task   — what this specialist should look for. Be specific. 'Check whether the new query builder escapes user input before it reaches execute()' produces a far better review than 'look for security bugs'.

Ground every task in what YOU see in the diff. Bad: 'Review docker-compose.yml for security issues and configuration best practices.' Good: 'The MongoDB root password is hardcoded as "password" in docker-compose.yml — check for credential exposure, and verify whether the port mapping exposes 27017 beyond localhost.'

  files  — the subset of changed files this task covers. Omit or leave empty only when the task genuinely needs the whole diff. Scoping keeps each specialist's context small, which makes its findings sharper.

SPLITTING WORK
The same specialist may appear MULTIPLE times in one batch with different files and different tasks. Split when a diff touches several unrelated areas — one security task for the auth changes and another for the file upload handler beats one task that has to hold both in its head.
Do not split a single coherent change across tasks just to create parallelism; a reviewer that cannot see the whole change it is judging will produce false positives.

WHICH SPECIALISTS
Use the conditions listed under each specialist in the catalog. Skip specialists whose domain this change cannot touch.

BUDGET
At most {max_tasks} tasks per batch, and at most {max_batches} batches for the whole review. Spend the first batch on breadth: cover every relevant area. Use a second batch only if the first batch's results point somewhere genuinely new — never to re-confirm what a specialist already told you.

A second-batch task MUST cite what the first batch revealed that warrants further investigation. Retrying a failed task, or re-dispatching the same specialist on the same files without a new angle discovered from batch 1 results, is never valid.

OUTPUT
When every relevant area has been covered, stop calling tools and respond with a routing report and nothing else. Do not summarize, restate, or judge the findings; they are aggregated separately and shown to the developer directly.

Format the report as one line per SKIPPED specialist:
  <specialist>: skipped (<why this change cannot touch its area>)
Do not report specialists that you ran; the system already traces them.
"""
)

class SpecialistTask(BaseModel):
    """One unit of review work assigned to one specialist."""

    agent: str = Field(description="Specialist name from the catalog.")
    task: str = Field(description="Specific instruction for this run: what to look for and where.")
    files: list[str] = Field(
        default_factory=list,
        description="Subset of the changed files this task covers. Empty means the whole diff.",
    )


class MasterTraceEntry(BaseModel):
    subagent: str
    task: str = ""
    files: list[str] = Field(default_factory=list)
    finding_count: int = 0
    is_recall: bool = False
    error: str | None = None


class MasterResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    trace: list[MasterTraceEntry] = Field(default_factory=list)
    cap_reached: bool = False
    routing_summary: str = ""
    # Set when the master loop died with completed runs already in hand. The
    # findings above are real and worth showing; the review is just unfinished.
    aborted: str | None = None


def _summarize_findings(findings: list[Finding]) -> str:
    if not findings:
        return "no findings"
    parts = [f"{f.severity.value}: {f.title} ({f.file_path})" for f in findings]
    return f"{len(findings)} finding(s): " + "; ".join(parts)


def _scope_label(files: list[str]) -> str:
    return ", ".join(files) if files else "full diff"


def _validate_tasks(
    tasks: list[SpecialistTask],
    specialists: dict[str, SpecialistSpec],
    changed_files: list[str],
) -> tuple[list[SpecialistTask], list[str]]:
    """Drop tasks the master cannot legally ask for; repair the ones we can.

    Returns the accepted tasks and human-readable rejection notes, which are fed
    back to the master so it can correct itself rather than silently losing work.
    """
    accepted: list[SpecialistTask] = []
    rejections: list[str] = []
    known_files = set(changed_files)

    for index, task in enumerate(tasks):
        if task.agent not in specialists:
            rejections.append(
                f"unknown specialist '{task.agent}' (available: {', '.join(sorted(specialists))})"
            )
            continue

        unknown = [path for path in task.files if path not in known_files]
        scoped = [path for path in task.files if path in known_files]
        if unknown:
            rejections.append(
                f"{task.agent}: dropped path(s) not in this diff: {', '.join(unknown)}"
            )
        accepted.append(SpecialistTask(agent=task.agent, task=task.task, files=scoped))

        if len(accepted) == MAX_TASKS_PER_BATCH:
            dropped = len(tasks) - index - 1
            if dropped > 0:
                rejections.append(
                    f"batch truncated at {MAX_TASKS_PER_BATCH} tasks; {dropped} task(s) not run"
                )
            break

    return accepted, rejections


async def _execute_task(
    task: SpecialistTask,
    spec: SpecialistSpec,
    diff_context: DiffContext,
    tools: list[BaseTool],
    repo_context: str = "",
) -> SpecialistRun:
    """Run one task. A failed specialist must not kill the batch.

    Errors are caught *inside* ``tracer.run`` so ``agent_error`` lands on this
    run rather than as a floating event — which is why this catches here at all
    instead of leaving it to the backstop in ``_run_batch``.
    """
    scope = task.files or diff_context.changed_files
    with tracer.run(task.agent, _scope_label(task.files)):
        try:
            findings = await run_subagent_review(
                name=spec.name,
                system_prompt=spec.system_prompt,
                diff_text=slice_diff(diff_context.diff_text, task.files),
                changed_files=scope,
                tools=tools,
                task=task.task,
                repo_context=repo_context,
            )
        except LLMError as exc:
            tracer.agent_error(task.agent, str(exc))
            return SpecialistRun(agent=task.agent, task=task.task, files=task.files, error=str(exc))
        except Exception as exc:
            # `run_subagent_review` funnels its own failures into LLMError, so
            # anything here came from the machinery around it — a bad slice, a
            # tool wrapper, a schema error. Still not worth the batch.
            # CancelledError is a BaseException and deliberately passes through.
            reason = f"{type(exc).__name__}: {exc}"
            tracer.agent_error(task.agent, reason)
            return SpecialistRun(agent=task.agent, task=task.task, files=task.files, error=reason)

    return SpecialistRun(agent=task.agent, task=task.task, files=task.files, findings=findings)


async def _run_batch(
    tasks: list[SpecialistTask],
    specialists: dict[str, SpecialistSpec],
    diff_context: DiffContext,
    tools: list[BaseTool],
    batch: int,
    repo_context: str = "",
) -> list[SpecialistRun]:
    """Fan the batch out as concurrent tasks, at most MAX_FANOUT at a time.

    ``asyncio.gather`` preserves argument order, so results come back in the
    order the master asked for them. Each coroutine runs in its own context, so
    the tracer's per-run state stays isolated between concurrent specialists.

    ``return_exceptions=True`` is what makes the isolation structural. Without
    it a single escaping exception cancels every sibling coroutine mid-flight,
    destroying finished work to report one failure — and the guarantee would
    rest on ``_execute_task`` happening to catch everything, including from
    ``tracer.run`` itself, which writes to the trace file and can fail.
    """
    workers = max(1, min(MAX_FANOUT, len(tasks)))
    tracer.dispatch([(t.agent, _scope_label(t.files), t.task) for t in tasks], workers, batch)

    limit = asyncio.Semaphore(workers)

    async def run_one(task: SpecialistTask) -> SpecialistRun:
        async with limit:
            return await _execute_task(
                task, specialists[task.agent], diff_context, tools, repo_context
            )

    results = await asyncio.gather(
        *(run_one(task) for task in tasks), return_exceptions=True
    )

    runs: list[SpecialistRun] = []
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, SpecialistRun):
            runs.append(result)
            continue
        if isinstance(result, asyncio.CancelledError):
            # Shutdown, not a specialist failure. Swallowing it here would
            # report a cancelled review as a completed one.
            raise result
        reason = f"{type(result).__name__}: {result}"
        tracer.agent_error(task.agent, reason)
        runs.append(
            SpecialistRun(agent=task.agent, task=task.task, files=task.files, error=reason)
        )
    return runs


def _summarize_batch(runs: list[SpecialistRun], rejections: list[str]) -> str:
    lines = [f"Ran {len(runs)} specialist task(s)."]
    for run in runs:
        scope = _scope_label(run.files)
        if run.error:
            lines.append(f"- {run.agent} [{scope}]: FAILED — {run.error}")
        else:
            lines.append(f"- {run.agent} [{scope}]: {_summarize_findings(run.findings)}")
    for note in rejections:
        lines.append(f"- rejected: {note}")
    return "\n".join(lines)


def build_dispatch_tool(
    diff_context: DiffContext,
    tools: list[BaseTool],
    collected: dict[str, list[SpecialistRun]],
    specialists: dict[str, SpecialistSpec],
    repo_context: str = "",
) -> BaseTool:
    """Build the master's single tool: a parallel, scoped specialist dispatcher."""
    catalog_summary = format_catalog_summary(specialists)
    state = {"batch": 0}

    @tool(
        "dispatch_specialists",
        description=(
            "Run one or more specialist reviewers CONCURRENTLY over the staged diff. "
            "Pass every independent task in a single call.\n"
            f"Available specialists:\n{catalog_summary}"
        ),
    )
    async def dispatch_specialists(
        tasks: list[SpecialistTask],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        state["batch"] += 1
        if not tasks:
            return "error: 'tasks' was empty — pass at least one task."

        accepted, rejections = _validate_tasks(tasks, specialists, diff_context.changed_files)
        for note in rejections:
            tracer.rejected(note)
        if not accepted:
            return "error: no valid tasks in this call.\n" + "\n".join(rejections)

        runs = await _run_batch(
            accepted, specialists, diff_context, tools, state["batch"], repo_context
        )
        # One write, from the calling thread, after every worker has joined — so
        # the shared dict needs no lock.
        collected[tool_call_id] = runs
        return _summarize_batch(runs, rejections)

    return dispatch_specialists


def _absorb(
    runs: list[SpecialistRun],
    trace: list[MasterTraceEntry],
    findings: list[Finding],
    seen_agents: set[str],
) -> None:
    """Fold one batch's runs into the accumulating trace and finding list."""
    for run in runs:
        trace.append(
            MasterTraceEntry(
                subagent=run.agent,
                task=run.task,
                files=run.files,
                finding_count=len(run.findings),
                is_recall=run.agent in seen_agents,
                error=run.error,
            )
        )
        seen_agents.add(run.agent)
        findings.extend(run.findings)


def _salvage_result(collected: dict[str, list[SpecialistRun]], reason: str) -> MasterResult:
    """Rebuild a result from completed runs after the master loop died.

    The message history is gone — so routing_summary and cap_reached are
    unavailable — but ``collected`` holds every run that finished, in dispatch
    order, and those findings were paid for. Throwing them away because the
    master's own next call 429'd is the expensive failure this avoids.
    """
    trace: list[MasterTraceEntry] = []
    findings: list[Finding] = []
    seen_agents: set[str] = set()
    for runs in collected.values():
        _absorb(runs, trace, findings, seen_agents)
    return MasterResult(findings=findings, trace=trace, aborted=reason)


def _extract_result(
    messages: list[Any], collected: dict[str, list[SpecialistRun]]
) -> MasterResult:
    trace: list[MasterTraceEntry] = []
    all_findings: list[Finding] = []
    seen_agents: set[str] = set()
    cap_reached = False
    routing_summary = ""

    for msg in messages:
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                for call in msg.tool_calls:
                    runs = collected.get(call["id"])
                    if runs is None:
                        continue  # blocked by the dispatch limit, never executed
                    _absorb(runs, trace, all_findings, seen_agents)
            elif msg.content:
                routing_summary = str(msg.content)
                tracer.routing_report(routing_summary)
        elif (
            isinstance(msg, ToolMessage)
            and getattr(msg, "status", None) == "error"
            and "limit exceeded" in str(msg.content).lower()
        ):
            cap_reached = True

    return MasterResult(
        findings=all_findings,
        trace=trace,
        cap_reached=cap_reached,
        routing_summary=routing_summary,
    )


async def run_master_loop(
    diff_context: DiffContext,
    tools: list[BaseTool],
    model: str = DEFAULT_MASTER_MODEL,
    specialists: dict[str, SpecialistSpec] | None = None,
    repo_context: str = "",
) -> MasterResult:
    tracer.review_start(len(diff_context.changed_files))
    collected: dict[str, list[SpecialistRun]] = {}

    if specialists is None:
        specialists = load_specialists()
    if not specialists:
        raise LLMError("no specialists found in the prompt catalog")

    dispatch_tool = build_dispatch_tool(
        diff_context, tools, collected, specialists, repo_context
    )

    user_content = (
        f"Changed files:\n{chr(10).join('- ' + f for f in diff_context.changed_files)}\n\n"
        f"Diff:\n{diff_context.diff_text}"
    )

    try:
        agent = create_agent(
            model=get_chat_model(model),
            tools=[dispatch_tool],
            system_prompt=MASTER_SYSTEM_PROMPT.format(
                catalog_summary=format_catalog_summary(specialists),
                max_tasks=MAX_TASKS_PER_BATCH,
                max_batches=MASTER_DISPATCH_CAP,
            ),
            middleware=[
                ToolCallLimitMiddleware(run_limit=MASTER_DISPATCH_CAP, exit_behavior="end")
            ],
        )
        result = await agent.ainvoke({"messages": [HumanMessage(content=user_content)]})
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        # Specialists that already finished are in `collected`, and their work
        # is not cheap. The master's own call is what usually dies here — a 429
        # or a context overflow on batch two — so the completed batches are
        # still a usable review. Only a failure with nothing in hand is fatal.
        if collected:
            tracer.agent_error("master", reason)
            return _salvage_result(collected, reason)
        raise LLMError(reason) from exc

    return _extract_result(result.get("messages", []), collected)
