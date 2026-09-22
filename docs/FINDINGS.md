# Findings: improvements worth making

From a full read of `reviewer/` and `bot/`, plus three instrumented runs
against `arvindvikram06/test-proj#2` on `claude-sonnet-5`. Ordered by what
costs the most review quality.

---

## 1. Two specialist prompts name tools that do not exist — HIGH

`reviewer/agents/prompts/security.md:6,10` and
`reviewer/agents/prompts/architecture.md:6` tell the model its tools are:

> `get_file_contents` (pass a repository-relative `path`) and `search_code`
> (scope with `repo:owner/name`)

The tools actually bound are `read_file`, `search_code(pattern, path_glob)` and
`list_directory` (`bot/review/local_tools.py:127-141`). `get_file_contents`
does not exist, and `repo:owner/name` is GitHub-search syntax that, passed to
the local regex grep, matches nothing.

This is leftover from the MCP/GitHub-API era removed in `5c796cb`. Two of four
specialists are affected; `coding_standards.md` and `infra.md` are clean.

Why it matters more than a stale docstring: `_repo_context`
(`bot/review/run.py:69-104`) exists *specifically* to stop this failure — its
own comment says that without it a model "writes `repo:owner/name` literally
into search queries and calls the file reader with no path, burning its whole
iteration budget on calls that cannot succeed". The system prompt now
reintroduces exactly that, and contradicts the user message the same run
carries. Consistent with the observed waste: `read_file` called on directories,
searches returning nothing, budgets exhausted.

**Fix:** delete the tool sentences from both prompt files. `_repo_context`
already describes the real tools once, correctly, in one place.

---

## 2. `pom.xml` is unreadable to every specialist — MEDIUM

`reviewer/sandbox/files.py:17`:

```python
_DENIED_PARTS = frozenset(
    {".git", ".env", ".venv", "venv", "node_modules", "__pycache__",
     ".ssh", ".aws", ".reviewer", ".xml", "pom.xml"}
)
```

Every other entry is a secret store or a vendor directory. These two are
neither:

- `pom.xml` — a Maven build file, blocked outright. On a Java repo the
  specialists cannot read the one file that carries dependency versions, so a
  whole class of finding (vulnerable dependency, wrong scope, plugin
  misconfiguration) is impossible. `list_dir` also hides it.
- `.xml` — matched against path *parts*, so it only ever matches a file or
  directory literally named `.xml`. It reads like "block XML files" and blocks
  essentially nothing. Whichever was intended, the code does not do it.

**Fix:** drop both. If XML genuinely needs restricting, do it by suffix and say
so; a deny-list whose entries do not do what they look like is worse than none.

---

## 3. `recheck` throws away its work at the cap — MEDIUM

`bot/review/recheck.py:121` uses `exit_behavior="end"` with `_TOOL_CALL_CAP =
12`. This is the same defect just fixed in `subagents/base.py`: hitting the cap
ends the run with no turn to produce the `Verdict`, `structured_response` comes
back `None`, and the fallback posts "I could not complete a re-check just now".

The failure is quieter here than in the review path. A dispute that exhausts 12
calls always resolves to **hold** — the bot tells a human their objection was
not examined, having in fact examined it. Given specialists routinely spend
60+ calls, 12 is not a generous ceiling.

**Fix:** `exit_behavior="continue"` plus a `recursion_limit`, matching
`base.py`; tell the model its budget in `_PROMPT` the way the shared rules now
do.

---

## 4. Specialists re-read the same files independently — MEDIUM (cost)

Specialists are stateless by design and run concurrently over one checkout, so
they duplicate reads. Measured in one run: **47 `read_file` calls**, with
`OrderReturnsService.cs` read four times and `ReturnsController.cs` three, all
from the same immutable directory.

The checkout is immutable for the review's lifetime, so this is a free win.

**Fix:** memoise in `repo_tools(repo_root)` (`local_tools.py:122`) — one cache
per review, keyed on `(tool, args)`. Cuts latency and token spend without
touching the statelessness that makes specialists safe to run in parallel.

---

## 5. Defaults in code disagree with `.env.example` — MEDIUM

| Setting | `reviewer/config.py` | `.env.example` | `.env` |
|---|---|---|---|
| `REVIEWER_MAX_TASKS_PER_BATCH` | **50** | 8 | 8 |
| `REVIEWER_SUBAGENT_TOOL_ITERATION_CAP` | **70** | 15 | 70 |

A fresh clone with no `.env` gets 50 tasks per batch — the master may dispatch
50 specialist runs in one call, throttled only by `MAX_FANOUT=4`. A clone that
copies `.env.example` gets a 15-call tool budget, which after the budget-prompt
change now tells the model "you have about 15 tool calls" on a codebase where
they spend 60+.

Neither number is defensible as a default; both are silent.

**Fix:** make the code defaults match the example (8 and 70), and add the
Claude-profile variables (`REVIEWER_CLAUDE_PROXY_KEY`) to `.env.example`, which
still documents only the GLM backend.

---

## 6. The run counter reports failures as findings — MEDIUM (observability)

The tracer's closing line counts every `Finding` object, including the
`is_failure=True` records that stand in for a dead specialist. A run where all
three specialists died printed:

```
◆ Done · 6 findings · 6 runs · 303.0s
```

…and the review contained zero findings. `run.py` gets this right — it splits
`failures` from `findings` before anything is published — but the terminal line
a human actually watches does not. This is the same class of bug the
`is_failure` flag was introduced to fix (a failed specialist rendering as
"✓ 1 finding"), surviving one level up.

**Fix:** count `not f.is_failure` for the headline and report failures
separately: `3 findings · 3 failed · 6 runs`.

---

## 7. `slice_diff` silently widens a mis-scoped task — LOW

`reviewer/models/diff_context.py:slice_diff` returns the **whole diff** when
none of the requested paths match, on the reasoning that "a specialist
reviewing everything is wasteful, but one reviewing nothing is useless". Fair.
But `_validate_tasks` (`master.py`) has already stripped unknown paths, so a
task scoped entirely to paths the master got wrong arrives with `files=[]` and
silently becomes a full-diff review — the one case the widening is least likely
to be right, and nothing logs it.

**Fix:** log when the fallback fires. It is a signal the master is inventing
paths.

---

## 8. Open question: why Sonnet reports nothing — NARROWED

After the `exit_behavior` + budget-prompt change, the same PR produced:

```
◆ Done · 0 findings · 5 runs · 238.1s    (0 cap hits, 0 failed specialists)
```

The mechanical fix worked — nothing was killed at the ceiling, and every
specialist returned parseable output. But every specialist returned *empty*,
and the master's batch-2 dispatch in that same run was chasing a concrete
concern ("endpoints accept an `OrderId` in the request body with no check that
the caller owns that order"), which suggests there was something to report.

Two candidate causes, and they need different fixes:

- the budget language ("stop at two-thirds", "report what you are confident
  of, then stop") reads as permission to report nothing; or
- the required `offending_line` + `line_range` fields make an unanchorable
  finding easier to drop than to file.

**Next step:** one run with the tracer's per-specialist final messages logged
(`_last_model_words` already captures them, but only on the unparseable path).
Log the final message for empty-but-parsed runs too — that single line
distinguishes "I found nothing" from "I found things I could not anchor".

**Update (same day).** A later run on `PATH/minimax-m3` with the identical
budget prompt produced **10 findings, all 10 anchored inline** (9 line numbers
agreed, 1 corrected). So the budget language does not inherently suppress
findings, and the pipeline does produce reviews — this is no longer a blocker
for the optimisation work.

What remains open is narrower and model-specific: on `claude-sonnet-5` every
specialist returned parseable but empty output. Still worth logging
`_last_model_words` on the empty-but-parsed path, because that one line
separates "I found nothing" from "I found things I could not anchor" — but it
is now a Sonnet-calibration question, not a pipeline question.

See `docs/OPTIMIZATION_PLAN.md` for the ordered work and the baseline to
measure against.
