# Optimization plan: tool calls first, then everything else

Status: **not started.** Written 2026-09-22 from three instrumented runs
against `arvindvikram06/test-proj#2`.

Read `docs/FINDINGS.md` for the full defect list; this file is the ordered plan
of work, with the baseline to measure against.

---

## Baseline to beat

The run this plan is built on. **Model: `PATH/minimax-m3`** via the Presidio
gateway (`.env` overrides the `glm` profile's `bedrock/zai.glm-5` — the profile
name in `reviewer/config.py` is not what ran). Every number below is a property
of a model-and-system *pair*, not of the system alone.

| Metric | Baseline |
|---|---|
| Total tool calls | **112** (architecture 50, security 34, coding_standards 28) |
| `read_file` calls | ~63, against ~30 distinct files |
| Wall time | **264.7s**, 3 runs, 1 batch |
| Findings | **10** |
| Anchoring | 10 inline, 0 file-level, 0 unanchorable |
| Line numbers | quoted 10 · agreed 9 · corrected 1 (off by −2) |
| Failed specialists | 1 (architecture, `GraphRecursionError`) |

Per-specialist latency, which is its own finding:

| Specialist | Calls | Time | Per call |
|---|---|---|---|
| security | 34 | 105.4s | 3.1s |
| coding_standards | 28 | 225.9s | **8.1s** |

**The success criterion: calls fall sharply, findings do not.** The anchoring
row is the part that must not regress — it is the mechanism the whole design
rests on, and in this run it worked perfectly.

Earlier runs, for context (model: `claude-sonnet-5` via local CLIProxyAPI):
60+ calls per specialist, all three killed at the 70-call cap, 0 findings.
Two different models over-explore against the same ceiling, so the caps and the
missing budget guidance are the problem — not one model's appetite. GLM has
never actually been measured here; any earlier claim that it "stops digging
earlier" was inference, not data.

---

## Phase 0 — undo the regression (blocking)

### 0.1 The turn backstop is measured in the wrong unit

When the subagent tool cap moved from `exit_behavior="end"` to `"continue"`
(`reviewer/agents/subagents/base.py`), a `recursion_limit = cap * 2 + 10`
(= 150) was added to stop a model looping on refused calls. That arithmetic
assumed one tool call costs two graph steps.

It does not. A model turn that calls no tool, a turn whose calls are blocked,
and the final structured-output turn all consume steps without spending tool
calls. Architecture died at **50 tool calls** — far short of its 70-call
budget — having already burned 150 steps:

```
✗ architecture ... failed: GraphRecursionError: Recursion limit of 150 reached
  without hitting a stop condition.                        50 calls · 50.2s
```

**Fix, two parts — the second matters more:**

1. Raise headroom to `cap * 3 + 20` so the *tool* budget binds, not the step
   budget.
2. **Catch `GraphRecursionError` and route it to `_unparseable_finding`**
   rather than letting it become `LLMError`. Today it raises, `_execute_task`
   records a bare error string, and everything the specialist read is lost —
   so the specialist that explored most has its work most completely
   destroyed.

The principle the codebase already holds everywhere else: running out of budget
costs a specialist its **tools**, never its **work**.

---

## Phase 1 — tool-call optimization

### 1.1 Shared read cache — biggest win, zero behaviour change

Specialists are stateless and concurrent against **one immutable checkout**, so
nothing shares what any of them learned and they each rediscover the same
files. In the baseline run, these were read by all three specialists:

`Order.cs` · `OrderItem.cs` · `Product.cs` · `ReturnDtos.cs` ·
`OrderReturnsService.cs` · `ReturnsController.cs` · `IProductRepository.cs` ·
`OrderRepository.cs`

plus repeated `list_directory` on `.`, `src`, `src/OrderApi.Domain`,
`src/OrderApi.Data`. Roughly half of all `read_file` calls were re-reads.

**Fix:** a memo dict inside `repo_tools(repo_root)`
(`bot/review/local_tools.py:122`), keyed on `(tool_name, args)`, living exactly
as long as the review. Safe because the checkout cannot change underneath it —
the same argument that justifies fetching one commit at depth 1. Every
specialist receives identical bytes to what it would have got.

**Expect ~35% of all calls to disappear.**

### 1.2 Hand them a repo map instead of making them crawl

Architecture spent its first **10 calls** walking the tree (`.`, `src`,
`src/OrderApi.Business`, `.../Services`, `src/OrderApi.Domain`,
`.../Entities`, `.../Enums`, …) before reading anything. Coding standards did
the same walk independently. Structure discovery is identical for every
specialist on every run.

**Fix:** compute the file tree once per review and put it in `_repo_context`
(`bot/review/run.py:69`). Replaces N crawls with one local operation.

It also removes the *cause* of most wrong-path guesses — architecture guessed
`src/OrderApi.Business/OrderReturnsService.cs`, got an error, then found it at
`.../Services/OrderReturnsService.cs`. With the tree in front of it, that guess
never happens.

### 1.3 `read_file` on a directory should not lie

Twice in the baseline run a specialist called `read_file` on a directory and
got `error: no such file: src/OrderApi.Business/Dtos`. The path exists — it is
simply not a file — and the model then spends a second call on
`list_directory` for the same path.

`reviewer/sandbox/files.py:read_file` raises `no such file` whenever
`is_file()` is false, conflating "does not exist" with "is a directory".

**Fix:** detect the directory case and either return the listing or say *"that
is a directory — use `list_directory`"*. Turns a two-call detour into zero.

### 1.4 Delete the stale tool names from two prompts

`reviewer/agents/prompts/security.md:6,10` and
`reviewer/agents/prompts/architecture.md:6` still name `get_file_contents` and
`search_code` scoped with `repo:owner/name`. Neither exists; the real tools are
`read_file`, `search_code(pattern, path_glob)`, `list_directory`. Residue from
the MCP path deleted in `5c796cb`.

Beyond tidiness: `_repo_context` exists *specifically* to prevent this failure.
Its own comment says that without it a model "writes `repo:owner/name`
literally into search queries and calls the file reader with no path, burning
its whole iteration budget on calls that cannot succeed". The system prompt now
reinstates exactly what the user message exists to prevent, and the two
contradict each other inside one run.

Worth noting: architecture is both the worst crawler and one of the two with
wrong tool docs. May be coincidence — the fix is a deletion either way.

---

## Phase 2 — after the calls come down

### 2.1 Understand the latency asymmetry before tuning concurrency

8.1s/call vs 3.1s/call on the same gateway and model (table above). Tool calls
are local filesystem reads costing milliseconds, so the gap is model time —
longer generations or larger contexts. Find out which **before** touching
`MAX_FANOUT`: if it is context size, 1.1 and 1.2 may fix it for free.

### 2.2 Check whether adjudication still earns its call

Baseline: `adjudicated 10 finding(s) -> 10 (0 merged, 0 dropped)` — a full
model call that changed nothing. Not a bug: the specialists were well-scoped
and genuinely did not overlap, which is the design working. But adjudication
exists because of a measured 14-findings-describing-7-problems run, so it is
worth measuring how often it merges now. Better scoping may already have solved
the problem it was built for.

### 2.3 Two small correctness items (detail in `FINDINGS.md`)

- **`bot/review/recheck.py:121`** still uses `exit_behavior="end"` at a 12-call
  cap — the same defect fixed in `base.py`, and quieter, because it always
  resolves to **hold** and tells a human their objection was not examined when
  it was.
- **The `◆ Done · N findings` counter** includes `is_failure` records. That is
  how "6 findings" printed on a review that had none.

### 2.4 Carried over from `FINDINGS.md`

`pom.xml` in the deny-list (#2) · config defaults disagreeing with
`.env.example` (#5) · `slice_diff` silently widening a mis-scoped task (#7).

---

## Verification

Re-run PR 2 after each phase and record the baseline table again, **noting the
model id every time**. Phase 1 is successful when total calls drop substantially
and the findings and anchoring rows hold. If findings fall, the cache or the
repo map changed what specialists see — stop and diagnose rather than tuning
further.
