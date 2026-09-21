"""Tracing for the review pipeline.

Nothing about a worker is printed while it runs. Specialists run concurrently,
so live output interleaves into noise and needs locks and per-run buffers to
untangle. Instead each run simply *collects* what it did, and everything is
rendered once, in order, when the review ends.

The only live output is the dispatch plan, which is worth seeing immediately
because it tells you what the review is about to do.
"""

from __future__ import annotations

import contextvars
import itertools
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.tree import Tree

QUIET, NORMAL, VERBOSE = 0, 1, 2

_LEVELS = {"quiet": QUIET, "normal": NORMAL, "verbose": VERBOSE}

_SEVERITY_STYLE = {
    "critical": ("✖", "bold red"),
    "high": ("▲", "red"),
    "medium": ("▲", "yellow"),
    "low": ("▾", "cyan"),
    "info": ("·", "dim"),
}

_run_counter = itertools.count(1)

# Context-local, not thread-local: a specialist's tools do not run on its own
# thread or task, and contextvars follow both.
_current_run: contextvars.ContextVar[_Run | None] = contextvars.ContextVar(
    "reviewer_current_run", default=None
)


@dataclass
class _ToolCall:
    name: str
    args: dict[str, Any]
    summary: str


@dataclass
class _Finding:
    severity: str
    title: str
    location: str


@dataclass
class _Run:
    agent: str
    scope: str
    id: str = field(default_factory=lambda: f"run-{next(_run_counter)}")
    started: float = field(default_factory=time.monotonic)
    elapsed: float = 0.0
    tool_calls: list[_ToolCall] = field(default_factory=list)
    findings: list[_Finding] = field(default_factory=list)
    error: str | None = None


class Tracer:
    def __init__(
        self,
        level: int = NORMAL,
        trace_file: str | os.PathLike[str] | None = None,
        console: Console | None = None,
    ) -> None:
        self.level = level
        self._console = console or Console(stderr=True, highlight=False)
        self._started = time.monotonic()
        self._runs: list[_Run] = []
        self._rejections: list[str] = []
        self._routing = ""
        self._trace_path: Path | None = None
        if trace_file:
            self.set_trace_file(trace_file)

    # -- configuration ---------------------------------------------------

    def set_trace_file(self, trace_file: str | os.PathLike[str]) -> None:
        self._trace_path = Path(trace_file)
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._trace_path.write_text("")

    def reset_clock(self) -> None:
        self._started = time.monotonic()
        self._runs.clear()
        self._rejections.clear()
        self._routing = ""

    def _print(self, *args: Any, **kwargs: Any) -> None:
        if self.level > QUIET:
            self._console.print(*args, **kwargs)

    def _emit(self, kind: str, **data: Any) -> None:
        """Append one structured event to the JSONL transcript, if configured."""
        if not self._trace_path:
            return
        record = {"ts": round(time.monotonic() - self._started, 4), "kind": kind, **data}
        with self._trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    # -- collection ------------------------------------------------------

    @property
    def _run(self) -> _Run | None:
        return _current_run.get()

    @contextmanager
    def run(self, agent: str, scope: str) -> Iterator[_Run]:
        """Scope every tool call and finding on this task to one specialist run."""
        run = _Run(agent=agent, scope=scope)
        token = _current_run.set(run)
        self._emit("run_start", run=run.id, agent=agent, scope=scope)
        try:
            yield run
        finally:
            _current_run.reset(token)
            run.elapsed = time.monotonic() - run.started
            self._runs.append(run)
            self._emit(
                "run_end", run=run.id, agent=run.agent, scope=run.scope,
                findings=len(run.findings), tool_calls=len(run.tool_calls),
                error=run.error, seconds=round(run.elapsed, 3),
            )

    def tool_call(self, name: str, args: dict[str, Any], result: str) -> None:
        run = self._run
        self._emit(
            "tool_call", run=run.id if run else None, agent=run.agent if run else None,
            tool=name, args=args, result_lines=len(result.splitlines()),
            result_excerpt=result[:200],
        )
        if run is not None:
            run.tool_calls.append(_ToolCall(name, args, _preview(result)))

    def finding(self, severity: str, title: str, location: str) -> None:
        run = self._run
        self._emit(
            "finding", run=run.id if run else None, agent=run.agent if run else None,
            severity=severity, title=title, location=location,
        )
        if run is not None:
            run.findings.append(_Finding(severity, title, location))

    def agent_error(self, agent: str, error: str) -> None:
        run = self._run
        if run is not None:
            run.error = error
        else:
            self._emit("agent_error", agent=agent, error=error)

    def note(self, text: str) -> None:
        self._emit("note", text=text)

    def rejected(self, reason: str) -> None:
        self._emit("task_rejected", reason=reason)
        self._rejections.append(reason)

    def routing_report(self, text: str) -> None:
        self._emit("routing_report", text=text)
        self._routing = text

    # -- live output (the plan only) -------------------------------------

    def review_start(self, file_count: int) -> None:
        self._emit("review_start", files=file_count)
        noun = "file" if file_count == 1 else "files"
        self._print(f"\n[bold cyan]◆[/] [bold]Code review[/] · {file_count} {noun} staged")

    def dispatch(self, tasks: list[tuple[str, str, str]], workers: int, batch: int = 1) -> None:
        """tasks: (agent, scope, task_text). Printed live — it is the plan."""
        self._emit(
            "dispatch", workers=workers, batch=batch,
            tasks=[{"agent": a, "scope": s, "task": t} for a, s, t in tasks],
        )
        noun = "task" if len(tasks) == 1 else "tasks"
        self._print(
            f"\n  [cyan]▸[/] [bold]Dispatch (Batch {batch})[/] · {len(tasks)} {noun} · {workers} at a time"
        )
        for agent, scope, task in tasks:
            self._print(f"      [bold]{agent}[/] [dim]{_short_scope(scope)}[/]")
            if self.level >= VERBOSE:
                self._print(f"        [dim]{task}[/]")

    # -- the summary, rendered once at the end ---------------------------

    def review_end(self, findings: int, runs: int) -> None:
        elapsed = time.monotonic() - self._started
        self._emit("review_end", findings=findings, runs=runs, seconds=round(elapsed, 3))
        if self.level == QUIET:
            return

        if self._runs:
            tree = Tree("[bold]Workers[/]")
            for run in self._runs:
                tree.add(self._render_run(run))
            self._print()
            self._print(tree)

        for reason in self._rejections:
            self._print(f"  [yellow]⚠[/] rejected: {reason}")

        if self._routing and self.level >= VERBOSE:
            self._print("\n  [cyan]▸[/] [bold]Routing report[/]")
            for line in self._routing.splitlines():
                self._print(f"      [dim]{line}[/]")

        noun = "finding" if findings == 1 else "findings"
        self._print(
            f"\n[bold cyan]◆[/] [bold]Done[/] · {findings} {noun} · "
            f"{runs} runs · {elapsed:.1f}s"
        )
        if self._trace_path:
            self._print(f"  [dim]trace: {self._trace_path}[/]")

    def _render_run(self, run: _Run) -> Tree:
        if run.error:
            head = (
                f"[red]✗[/] [bold]{run.agent}[/] [dim]{_short_scope(run.scope)}[/] "
                f"[red]failed: {run.error}[/] [dim]{run.elapsed:.1f}s[/]"
            )
        else:
            noun = "finding" if len(run.findings) == 1 else "findings"
            head = (
                f"[green]✓[/] [bold]{run.agent}[/] [dim]{_short_scope(run.scope)}[/] "
                f"{len(run.findings)} {noun} [dim]{run.elapsed:.1f}s[/]"
            )
        node = Tree(head)
        for call in run.tool_calls:
            node.add(f"[dim]🔍 {call.name}({_fmt_args(call.args)}) → {call.summary}[/]")
        for finding in run.findings:
            mark, style = _SEVERITY_STYLE.get(finding.severity.lower(), ("·", "dim"))
            node.add(
                f"[{style}]{mark} {finding.severity.upper():<8}[/] {finding.title}  "
                f"[dim]{finding.location}[/]"
            )
        return node


def _preview(result: str) -> str:
    """Describe a tool result: pass short literals through, count long ones."""
    if not result:
        return "empty"
    single = result.strip()
    if result.startswith("error:") or "\n" not in single:
        return single if len(single) <= 60 else single[:57] + "..."
    return f"{len(result.splitlines())} line(s)"


def _fmt_args(args: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items() if v is not None)
    return shown if len(shown) <= 70 else shown[:67] + "..."


def _short_scope(scope: str, limit: int = 60) -> str:
    """Long file lists make the plan unreadable; keep the shape, drop the bulk."""
    if scope == "full diff" or len(scope) <= limit:
        return scope
    parts = [p.strip() for p in scope.split(",")]
    if len(parts) == 1:
        return "…" + parts[0][-(limit - 1):]
    return f"{Path(parts[0]).name} +{len(parts) - 1} more"


def _default_tracer() -> Tracer:
    level = _LEVELS.get(os.environ.get("REVIEWER_TRACE", "normal").lower(), NORMAL)
    return Tracer(level=level, trace_file=os.environ.get("REVIEWER_TRACE_FILE") or None)


tracer = _default_tracer()


def configure(
    level: int | None = None,
    trace_file: str | os.PathLike[str] | None = None,
) -> Tracer:
    """Reconfigure the process-wide tracer **in place**.

    Never rebind the module global: every module binds this instance at import
    time, so replacing the object would leave them writing to a stale one.
    """
    if level is not None:
        tracer.level = level
    resolved = trace_file if trace_file is not None else os.environ.get("REVIEWER_TRACE_FILE")
    if resolved:
        tracer.set_trace_file(resolved)
    tracer.reset_clock()
    return tracer
