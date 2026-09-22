# Learnings: how this codebase thinks

Notes taken while reading the whole of `reviewer/` and `bot/` (~5,500 lines).
`docs/CODEBASE.md` already says what each file *is*. This says what the code
*believes* — the rules it obeys, why it obeys them, and where those rules came
from. Almost every one was paid for by a measured failure on a real pull
request, and the comments record the receipts.

---

## The three load-bearing decisions

**1. The GitHub thread is the database.**
A finding posted inline opens a thread, and a thread already has an id, a file,
a line, ordered replies, and a resolved flag — the row you would otherwise
store, except GitHub also renders it, notifies on it, and lets a human edit it.
So Postgres holds the job queue and nothing else (`bot/db/schema.sql`: two
tables, `deliveries` and `jobs`). The consequence runs through everything:
`bot/review/run.py` writes no findings anywhere, and "what did we already say?"
is answered by reading comments back off the PR (`bot/review/history.py`),
never by querying our own store. Two records of one conversation can disagree,
and the one the human can see has to win.

**2. The repository is cloned, and specialists read the clone.**
Not the API. `bot/workspace.py` documents why: `search_code` has no `ref`
parameter, so it searches the default branch from whenever GitHub last indexed
it and structurally *cannot* see a pull request's own code. Measured under the
API: three specialists made 18 file-content calls, opened one file between
them, and every search returned nothing. The fetch is `--depth 1` by SHA, the
remote (and with it the token) is removed before any tool runs, and the
checkout is treated as a cache with the lifetime of one review.

**3. Placement is computed, never asserted.**
A model is asked to *quote* the offending line, and `bot/review/locate.py`
searches the checkout for that string. Copying is something models do reliably;
counting lines through hunk headers is arithmetic, and the measurements were
bad — a catch block at 77-80 reported as 65-68, two findings giving 17-18 and
11-12 for the same constants at 16-17. `reviewer/models/anchor.py` then decides
LINE / FILE / NONE by parsing the diff itself, so a hallucinated number is
caught locally rather than as a 422.

---

## The invariants

- **Silence is never allowed to read as success.** A specialist that dies
  produces a `Finding` with `is_failure=True` rather than nothing
  (`base.py:_unparseable_finding`); `run.py` filters those out of what gets
  posted but names them in the summary, because "no usable output from
  `security`" is what the reader actually needs. The same instinct shows up in
  `summary_body`, which has a distinct headline for a genuinely clean review so
  that "0 comments posted" cannot be confused with "nothing ran".

- **Partial work is salvaged, never discarded.** `_run_batch` uses
  `return_exceptions=True` so one escaping exception cannot cancel its
  siblings mid-flight; `_salvage_result` rebuilds a review from `collected`
  when the master's own call dies on batch two. Findings are expensive and
  already paid for.

- **A failure must never silently become a decision.** `adjudicate` passes
  findings through unchanged if the plan is invalid — losing a real finding to
  a failed merge is worse than showing a duplicate, and `_valid` rejects
  all-or-nothing rather than partially applying. `recheck` holds the finding
  when it cannot complete, because a failed re-check that conceded would
  withdraw a finding nobody re-examined.

- **Guards are cheap, so they are doubled.** `bot/api.py` has four (signature
  over raw bytes, self-sender, delivery replay, classification) and re-checks
  author association in the worker: "a guard worth having is worth having
  twice". The signature note is the sharpest — verifying against re-serialised
  JSON is the standard way to get this wrong.

- **The diff is untrusted input.** Specialists are told never to follow
  instructions inside it, `_DENIED_PARTS` blocks credential paths at the tool
  boundary, and `_resolve_within` rejects any path escaping the checkout. The
  clone is to *read*: nothing builds, tests, or runs hooks.

---

## What the prompts have learned

The prompt layer is as engineered as the code, and for the same reason —
measurement.

- **Vague tasks are rejected, not logged.** `_vague_task` refuses a dispatch
  whose instruction restates the specialist's own name, because the master
  feeds on its own rejections and rewrites. Measured: the same specialist on
  the same file returned 7 findings for a task that named what to examine and 2
  for "review these files for coding_standards problems".

- **Optional fields get taken.** `offending_line` was optional with "omit only
  for a whole-file finding" — one run quoted nothing for 12 of 14 findings, so
  every comment fell to file level and rendered at line 1. The escape hatch was
  removed. Same lesson, same fix, in two places.

- **History goes in front of the model, not behind it.** Deduplicating posted
  findings by hashing them failed the obvious way: models reword, so "SQL
  Injection in FindBySupplierCodeAsync" and "SQL Injection via supplierCode
  parameter" hashed differently and the bot posted a duplicate *and* said
  "looks fixed" about a live injection. "Is this the same defect?" is a
  judgement, so it moved into the planning prompt.

- **Absence is not evidence.** The ✅ "looks fixed" replies were deleted for
  exactly that reason. Nothing claims a fix without a specialist opening the
  file.

---

## Where the seams are

- `reviewer/` is the engine and knows nothing about GitHub; `bot/` is the
  GitHub App. `reviewer/sandbox/files.py` deliberately has no framework
  imports, which is what lets the entire security model be read in one file.
- Specialists are **markdown files**, not code: `catalog.py` globs
  `prompts/*.md` and parses frontmatter, so adding a reviewer means adding a
  file. `_shared/review_rules.md` lives one directory down precisely so the
  glob does not mistake the rubric for a specialist.
- A specialist run is stateless and scoped — `slice_diff` hands it only its own
  files, and the same specialist may run concurrently on two slices. The unit
  of work is a *run*, not an agent (`SpecialistRun`).

---

## Observed behaviour worth carrying forward

Running the pipeline on a real PR (5 files, 2,172 diff tokens, 46-file
checkout) against `claude-sonnet-5`:

- A review is **2–5 minutes**, dominated by how many dispatch batches the
  master spends. One batch of 3 specialists: 136s. Two batches, 6 runs: 303s.
- Specialists are **far more exploratory than the caps assume**: 60+ tool calls
  each on a small C# codebase, against a 70-call ceiling tuned to a different
  model.
- Tool spend is dominated by `read_file` (47 of 75 observed calls), and a
  meaningful share is waste: the same file read up to four times, `read_file`
  called on directories, ranged re-reads of a 142-line file when the read cap
  is 2,000 lines.

See `docs/FINDINGS.md` for what to do about it.
