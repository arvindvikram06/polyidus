# The codebase, file by file

A GitHub App that reviews pull requests when someone tags it, posts each
finding as an inline comment on the line it concerns, and answers people who
reply disagreeing.

Two things are worth knowing before reading anything else, because most of the
structure follows from them:

**The repository is cloned, and specialists read files from that clone.** Not
through the API. Reviewing through the API was measured: three specialists
opened one file between them, and every code search returned nothing, because
GitHub's `search_code` has no `ref` parameter and structurally cannot see a
pull request's own code.

**The GitHub thread is the database.** A finding posted inline opens a thread,
and a thread already has an id, a file and line, ordered replies, and a
resolved flag. That is the row we would otherwise store — except GitHub also
renders it, notifies on it, and lets a human edit and resolve it. So Postgres
holds the job queue and nothing else.

---

## The path a review takes

```
  a human comments "@polyidus-bot review"
        |
   bot/api.py            four guards, write a job row, return 200  (<100ms)
        |
   bot/db.py             the queue
        |
   bot/worker.py         claim the job, take the per-PR lock
        |
   bot/review/run.py     the orchestration below
        |
        +-- bot/workspace.py             clone the PR head into a temp dir
        +-- bot/review/local_tools.py    read/search/list, scoped to that clone
        +-- reviewer/core/master.py      plan, dispatch specialists concurrently
        +-- bot/review/locate.py         quoted line -> real line number
        +-- bot/review/adjudicate.py     merge duplicates, re-score, summarise
        +-- reviewer/models/anchor.py    LINE / FILE / NONE
        |
   bot/review/publish.py  one review containing every inline comment
```

And when someone replies to a finding:

```
  a human replies in a thread
        |
   bot/api.py            same guards, intent = "dispute"
        |
   bot/worker.py         handle_dispute
        |
   bot/review/recheck.py re-read the code with the objection in hand
        |
   bot/github/client.py  reply, and resolve the thread if withdrawn
```

---

## `bot/` — everything GitHub-facing

This package knows about webhooks, tokens and comments. It owns no review
logic beyond orchestration.

| File | Lines | What it does |
|---|---|---|
| `api.py` | 208 | The webhook endpoint. Four guards in order: verify the signature over the **raw bytes**, drop anything the bot itself sent, record the delivery id (GitHub delivers *at least once*), then classify. Writes one job row and returns. Nothing slow happens here — GitHub allows about ten seconds. |
| `worker.py` | 273 | Claims jobs and does the slow work. `handle_review` runs a review and posts it; `handle_dispute` answers someone who replied to a finding. A crashed worker loses nothing: the job row is already committed. |
| `db.py` | 181 | The queue. Claims use `FOR UPDATE SKIP LOCKED`, so several workers take different rows without blocking or colliding. A job that burns its attempts becomes `dead` rather than retrying forever. |
| `locks.py` | 79 | A Redis lock per pull request, so two workers cannot review the same PR at once. Released by a Lua compare-and-delete, so a worker can never release a lock it no longer holds. |
| `config.py` | 108 | Settings and the trigger word. `check()` names what is missing rather than failing at the first use. |
| `workspace.py` | 109 | Clones the PR head into a temp directory and deletes it afterwards, always. The token appears in the remote URL and is removed before the directory is handed over. **A clone to read** — nothing in the repository is ever executed. |
| `db/schema.sql` | 84 | Two tables: `deliveries` (the replay guard) and `jobs` (the queue). Mostly commentary explaining the five tables that were removed and why. |

### `bot/github/`

| File | Lines | What it does |
|---|---|---|
| `auth.py` | 90 | JWT → installation access token, cached per installation. Callers never handle credentials. |
| `client.py` | 323 | The REST calls, plus two GraphQL ones. GitHub's naming is a trap: a comment in the conversation box is an *issue* comment; a comment on a line of code is a *review* comment. Different endpoints, different reaction endpoints, different webhook events. The GraphQL exists for one reason — REST cannot resolve a review thread, and `isResolved` is not on a review-comment object at all. |

### `bot/review/`

| File | Lines | What it does |
|---|---|---|
| `run.py` | 290 | One review, start to finish. Resolves the commit **once**, so a push during the review is detectable rather than silently invalidating every line number. |
| `local_tools.py` | 144 | `read_file`, `search_code`, `list_directory`, scoped to the clone. This is what specialists get instead of API calls. |
| `locate.py` | 238 | Turns a quoted source line into a real line number by searching the checkout. Exists because asking a model to count lines through a diff header was measured wrong by 1 to 14 lines. Refuses rather than guesses; a comment on unrelated code is worse than one attached to the file. |
| `adjudicate.py` | 249 | One call that groups duplicates, scores each group once against the rubric and writes the summary. Specialists run concurrently and cannot see each other, so the same defect arrives several times at several severities — one run produced 14 findings describing 7 problems. Never loses a finding to a failure. |
| `publish.py` | 277 | Builds the comments and posts them as a single review. Line comments go inside the review; file-level ones cannot (a batched review's comments have no `subject_type`, and one of them makes GitHub reject the whole thing with a 422), so they follow individually. Holds a severity floor and a ten-comment cap. |
| `recheck.py` | 143 | Answers a dispute. Re-reads the code with the objection in hand and either holds or withdraws. Deliberately will not concede just because it was pushed back on — that would make every finding fall to the first objection, including the correct ones. A failed re-check **holds**, because silently withdrawing a finding nobody examined is the same failure as dropping one. |

---

## `reviewer/` — the review engine

No knowledge of GitHub at all. Given a diff and some tools, it produces
findings.

| File | Lines | What it does |
|---|---|---|
| `core/master.py` | 420 | The master agent: reads the diff, decides which specialists to run against which files, dispatches them concurrently, and re-plans between batches. It **never authors a finding** — it selects and routes. |
| `core/tracer.py` | 303 | Records what each specialist did: tool calls, what it ruled out, what it confirmed. |
| `agents/subagents/base.py` | 257 | One specialist run. Owns `FindingDraft`, the schema every finding comes back in. Both `offending_line` and `line_range` are **required** — they were optional, and models skipped them, so 12 of 14 findings once came back with no way to place them. |
| `agents/subagents/rules.py` | 55 | Composes the shared severity rubric into every specialist prompt, so the four cannot drift apart. |
| `agents/catalog.py` | 83 | Discovers specialists by scanning `prompts/*.md`. Adding a specialist means adding a file. |
| `agents/llm.py` | 22 | Model construction, one place. |
| `models/findings.py` | 78 | What a finding is. |
| `models/anchor.py` | 167 | Parses the diff to learn every addressable line, then decides `LINE`, `FILE` or `NONE` for each finding. Placement is decided here by **parsing**, never by asking a model where its comment should go. |
| `models/diff_context.py` | 70 | The diff, token counting, and slicing it per file so each specialist sees only its own scope. |
| `sandbox/files.py` | 215 | The file tools themselves, confined beneath the clone root. `read_file` prints **each line's real number in the margin** — without it a specialist with the file open still had to count, and counting is what it got wrong. `search_code` uses ripgrep when available and falls back to Python. |

### `reviewer/agents/prompts/`

One `.md` per specialist — `architecture`, `coding_standards`, `infra`,
`security` — plus `_shared/review_rules.md`, the severity rubric composed into
all of them. The catalog picks these up automatically.

---

## `tests/` — 81 tests

| File | Covers |
|---|---|
| `test_dispatch.py` | The master loop: routing, batching, failure isolation |
| `test_locate.py` | Quote → line number, including the cases it must refuse |
| `test_anchor.py` | Hunk parsing and LINE/FILE/NONE placement |
| `test_adjudicate.py` | Merging findings, and that a malformed plan never loses one |
| `test_local_tools.py` | The clone-scoped tools, including line numbering |
| `test_publish_floor.py` | What becomes a comment and what does not |
| `test_diff_context.py` | Slicing the diff per file |
| `test_tracer.py` | Tool calls staying attached to the right specialist |

---

## `scripts/`

| File | What it does |
|---|---|
| `preflight.py` | Verifies config, the App private key, and the whole auth chain before you start anything. Named in the error message when config is incomplete. |
| `fake_webhook.py` | Sends a signed webhook locally, so the API can be exercised without GitHub. |

---

## What does not contribute

Checked by resolving every import with AST. **No module is orphaned** — the
only two nothing imports are `bot/api.py` and `bot/worker.py`, which are the
entry points (`uvicorn bot.api:app` and `python -m bot.worker`).

Four smaller things earn a mention:

| Thing | Status |
|---|---|
| `client.list_review_comments()` | Written, no caller. It is what reads prior threads back for an incremental review — the next feature. Delete it if that gets dropped. |
| `TRUST_MODEL_LINE_NUMBERS` in `reviewer/config.py` | An experiment switch that skips line verification and posts whatever the model counted. Off by default, warns loudly when on. Kept so the comparison can be re-run; it found 3 wrong line numbers out of 10. |
| `docs/dispute-loop.html` | Describes a four-step dispute design that inline threads made unnecessary. Kept as history, superseded by `threads-as-state.html`. |
| `docs/BOT_GUIDE.md` | Its facts are current; its **shape** is not. It still reads as a twelve-step build plan. Worth regenerating rather than patching. |

---

## Things that look wrong and are not

- **`bot/` and `reviewer/` both have a `config.py`.** Different jobs: one holds
  GitHub App settings, the other model and budget settings. The engine must not
  know about webhooks.
- **A clone, but nothing is executed.** The tools can only read beneath the
  checkout root. None of the risk of running an untrusted contributor's code
  applies, because none of it is run.
- **Two tables for a reviewer bot.** They are not review state. They exist
  because we own the webhook, and GitHub allows it ten seconds while delivering
  at least once.
- **Findings are not stored anywhere.** By design. The comment a human can see
  is the record; a second copy in Postgres could disagree with it, and the one
  they can see would have to win anyway.
