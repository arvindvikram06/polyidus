# Code walkthrough

A tour of the code as it stands, written to be read in an editor with the files
open. Every reference is `path:line` so you can jump straight to it.

If you want to *run* the thing, read [`BOT_GUIDE.md`](BOT_GUIDE.md) instead.
This document is about what the code does and why it is shaped that way.

---

## 1. Two packages

```
reviewer/     the review ENGINE. Pre-existing, also drives the CLI.
bot/          the GitHub App. New. Wraps the engine in a service.
```

The split is deliberate. `reviewer/` knows how to review a diff and nothing
about GitHub Apps, webhooks or Postgres. `bot/` knows about all of those and
almost nothing about reviewing — it hands a diff to the engine and persists
what comes back.

The one rule that keeps this honest: **`reviewer/` must never import from
`bot/`.** The dependency points one way. That is what lets `reviewer review`
keep working from the command line with no database, no web server and no
GitHub App.

```
bot/ ──imports──▶ reviewer/          ✅
reviewer/ ──imports──▶ bot/          ❌ never
```

---

## 2. The path a review takes

Follow it once end to end; everything else is detail.

| # | Where | What happens |
|---|---|---|
| 1 | `bot/api.py:124` | Webhook arrives. Four guards. Job row written. `200` returned. |
| 2 | `bot/worker.py:211` | Worker loop claims the job. |
| 3 | `bot/worker.py:193` | Takes the per-PR lock, dispatches to a handler. |
| 4 | `bot/review/run.py:115` | The review itself. |
| 5 | `bot/workspace.py:76` | PR head fetched into a temp directory. |
| 6 | `bot/review/local_tools.py:122` | Three tools built over that checkout. |
| 7 | `reviewer/core/master.py:371` | Master plans, specialists run concurrently. |
| 8 | `bot/review/adjudicate.py:191` | Duplicates merged, severities normalised, summary written. |
| 9 | `reviewer/models/anchor.py:119` | Findings placed on lines. |
| 10 | `bot/review/store.py:79` | Persisted. |
| 11 | `bot/worker.py:85` | Formatted and posted as a comment. |

Steps 9 and 11 are the weak ones — see §8.

---

## 3. `bot/api.py` — the front door

One route that matters, `POST /webhook`, plus `GET /health`.

**The contract for this whole file: validate, write a row, answer.** Target is
under 100 ms.

GitHub POSTs the webhook and *waits on the connection*. It gives up after about
ten seconds, records the delivery as failed, and retries. A review takes
minutes — so doing the review here means GitHub hangs up, retries, and a second
review starts while the first is still running.

### The four guards, in order (`api.py:124`)

**1 — `verify_signature` (`api.py:57`).** HMAC-SHA256 over the **raw request
body**.

The classic way to get this wrong is verifying against re-serialised JSON.
`json.dumps(json.loads(raw))` is not `raw` — key order, spacing and unicode
escaping all differ — so the digest never matches. That is why `raw` is passed
around as bytes and only parsed *after* this returns `True`. Note also
`hmac.compare_digest`, not `==`: a plain comparison returns early and leaks how
much of the digest matched.

**2 — the loop guard.** If `sender.login` is our own bot, stop immediately.

Our own actions come back to us as events: the bot posts a comment, GitHub
sends an `issue_comment.created` whose sender is `polyidus-bot[bot]`. Without
this the bot reads its own comment, replies, reads that, forever.

Not theoretical — it has fired on real traffic four times, from two comments
the bot posted and two it deleted.

**3 — `record_delivery` (`db.py:61`).** GitHub's delivery is **at-least-once**,
so the same event legitimately arrives twice. Each carries a unique
`X-GitHub-Delivery`; inserting it into a table with a primary key makes a
repeat a no-op. Done with a unique violation rather than check-then-insert,
because two concurrent retries would race between the check and the insert.

**4 — `classify` (`api.py:79`).** Cheap checks only, no database, no network:

- `issue_comment.created` **and** `issue.pull_request` exists **and** the body
  mentions the trigger → `"review"`.
  That middle condition matters: `issue_comment` fires for plain issues too,
  and the `pull_request` key is present only when the issue is a PR.
- `pull_request_review_comment.created` with `in_reply_to_id` → `"dispute"`
  (not handled until the dispute loop is built).

Then the authority check — `author_association` must be `OWNER`, `MEMBER` or
`COLLABORATOR`. This matters more than it looks: once the dispute loop exists, a
human's words become part of an agent's instructions, so without this an
outside contributor could steer the agent from their own pull request.

---

## 4. `bot/worker.py` — the slow half

A loop (`worker.py:211`): claim a job, take the PR lock, run a handler, release,
mark done.

`run_job` (`worker.py:193`) has three outcomes, and the middle one is not a
failure:

```python
except locks.LockBusy:      # another worker has this PR — requeue, no error
except Exception:           # one bad job must not kill the loop
```

`handle_review` (`worker.py:39`) does two things before the review: adds 👀 to
your comment, then calls `review_pull_request`.

**Why the reaction matters.** From your side nothing has happened since you
pressed enter, and a review takes minutes. Without a signal you assume it is
broken and comment again — which queues a second job. A reaction is one fast
call, sends no notification, and adds no row to the conversation.

`_preview_comment` (`worker.py:85`) formats the result. This is temporary — the
real output is inline review comments, which is not built yet.

---

## 5. `bot/db.py` and `bot/locks.py` — state

### The queue (`db.py:113`)

```sql
SELECT id FROM jobs
 WHERE status = 'queued'
    OR (status = 'claimed' AND claimed_at < now() - ($1::int * interval '1 second'))
 ORDER BY created_at
 FOR UPDATE SKIP LOCKED
 LIMIT 1
```

`FOR UPDATE SKIP LOCKED` is what makes this safe for several workers: each
transaction locks the row it takes and **skips** rows another worker already
holds. No two workers claim the same job, and neither waits for the other.

The second `WHERE` clause is crash recovery — a job claimed longer ago than
`JOB_CLAIM_TIMEOUT_SECONDS` is assumed abandoned and becomes claimable again. A
job row surviving a worker crash is the entire point; an in-memory handoff
would lose it.

> **A bug worth knowing about.** The first version of this query declared two
> parameters and referenced only `$2`. Postgres cannot infer the type of an
> unused parameter, so every claim failed with `IndeterminateDatatypeError`.
> Every `$n` must actually appear in the query.

### The lock (`locks.py:53`)

`SET key token NX EX ttl` to acquire — atomic, and self-expiring so a worker
that dies does not wedge the PR forever. Release is a compare-and-delete in Lua,
because a plain `DEL` would let a worker whose lock had already expired delete a
lock a *different* worker has since taken.

**Why Redis and not a Python variable:** with one worker an in-process lock is
correct. With two it silently stops working — each replica holds its own copy,
both believe they have the lock, and two reviews run on the same PR posting
duplicate comments. That bug does not appear in development.

---

## 6. `bot/github/` — being the bot

### `auth.py` — no long-lived token

```
secrets/app.pem ──sign RS256 JWT (exp ≤ 10 min)──▶ /app/installations/{id}/access_tokens
                                                   ──▶ installation token (~1 hour)
```

That is a feature: the private key never travels, and a leaked installation
token expires on its own. Tokens are cached per installation and renewed five
minutes early (`auth.py:54`) — minting one per API call would spend the
installation's rate limit on authentication.

### `client.py` — plain REST, not MCP

**GitHub's naming is a trap and it is worth learning once:**

| What you see | GitHub calls it | Endpoint | Event |
|---|---|---|---|
| The conversation box | an **issue** comment | `/issues/{n}/comments` | `issue_comment` |
| A comment on a code line | a **review** comment | `/pulls/{n}/comments` | `pull_request_review_comment` |

Pull requests are issues in GitHub's data model. The two have different
endpoints, different *reaction* endpoints, and different webhook events —
subscribing only to `issue_comment` means a reply to an inline comment never
reaches you.

---

## 7. The review itself

### `bot/workspace.py:76` — the checkout

```
git init
git remote add origin https://x-access-token:<token>@github.com/o/r.git
git fetch --depth 1 origin <head_sha>
git checkout FETCH_HEAD
git rev-parse HEAD          # verify we got what we asked for
git remote remove origin    # drop the token before any tool runs
```

Fetched **by SHA at depth 1** — one commit, no history, no other branches. The
working tree cannot contain a version other than the one under review. Deleted
in a `finally`, so it survives errors.

**This clones to read, never to execute.** Nothing runs anything from the
repository — no build, no tests, no hooks — so the risk of executing an
untrusted contributor's code does not apply. `git checkout` does not run
repository code.

**Why it exists at all** is the most important measurement in the project.
Reviewing through GitHub's API:

```
get_file_contents   18 calls, 17 errored (94%)   — all the identical no-path call
search_code          4 calls,  4 returned total_count: 0
security specialist  read zero files
```

Two API limits caused that, neither fixable with prompting:

- `get_file_contents` marks `path` **optional**, and called without one returns
  a directory listing — a *successful* response. A model that omits it gets no
  signal it did anything wrong.
- `search_code` has **no `ref` parameter**. It searches the default branch from
  whenever GitHub last indexed it, so it can never see the code a pull request
  adds.

From a checkout, `grep` is a filesystem walk over exactly the commit being
reviewed, and it works on a repository pushed ten seconds ago.

### `bot/review/local_tools.py:122` — the three tools

`read_file`, `search_code`, `list_directory`. The implementations are
`reviewer/mcp/fs_server/tools.py` — the same path containment, deny-list and
output caps the CLI already used, which is why that module was written with no
framework imports.

The sandbox is `_resolve_within` (`tools.py:33`) plus `_DENIED_PARTS`
(`tools.py:17`). Two assertions in `tests/test_local_tools.py` cover it:

```
../../../etc/passwd  → error: path escapes the repository root
.git/config          → error: path is not readable by review tools
```

`.git/config` matters specifically — it held the fetch URL, which carried an
access token.

`grep` (`tools.py:132`) tries ripgrep and falls back to a pure-Python walk
(`tools.py:57`). Four flags there are load-bearing: `--no-config` (a host
`~/.ripgreprc` could otherwise change what a review sees), `--regexp` (a
model-supplied pattern starting with `-` would otherwise parse as a flag), no
`--follow` (symlinks stay inside), and `--hidden` plus explicit excludes
(`.github/workflows` matters to a reviewer).

### `reviewer/core/master.py:371` — the engine

Not new, and worth reading on its own. The master plans and routes; it never
reviews code itself. `build_dispatch_tool` (`master.py:254`) gives it exactly
one tool, `dispatch_specialists`, taking a batch of `(agent, task, files)`.

Three levels of failure isolation:

- `_execute_task` (`master.py:149`) — one specialist
- `_run_batch` (`master.py:189`) — `asyncio.gather(..., return_exceptions=True)`,
  so one exception does not cancel its siblings
- `_salvage_result` (`master.py:320`) — the master dying still returns the runs
  that completed

### `reviewer/agents/subagents/base.py:163` — one specialist

`FindingDraft` (`base.py:18`) is the schema the model fills in. Read its field
descriptions — they *are* the prompt.

`compose_system_prompt` (`rules.py:44`) glues the specialist's own markdown to
the shared rules. **Why the rules are not in the four prompt files:** four
copies of a rubric become four rubrics. `severity` was previously the only field
in the schema with no description at all, while every field around it had a
paragraph — so each specialist invented its own scale, and the same defect came
back `critical` from one and `high` from another.

`_unparseable_finding` (`base.py:125`) fabricates a `Finding` when a specialist
dies, flagged `is_failure=True`. The CLI shows it on purpose — someone reading a
terminal report should know a specialist produced nothing. The bot must not: its
`file_path` is just the first changed file, so publishing it means commenting on
a file with nothing wrong with it. `run.py` splits on that flag.

### `bot/review/adjudicate.py:191` — merging

One LLM pass that **groups, re-scores, drops and summarises** — and never
rewrites a finding's text. It has read the findings, not the code, so any
sentence it wrote about the code would be unverifiable.

The property that matters is negative: **a failed adjudication must never lose a
finding.** `_valid` (`adjudicate.py:172`) rejects a plan that references a
missing index, uses one twice, or leaves one unaccounted for, and the originals
pass through untouched. All-or-nothing on purpose — a partially applied plan
could silently discard a SQL injection.

Why it exists: one real review produced **14 findings describing 7 problems**,
with per-row `SaveChangesAsync` reported four times at `high`, `medium`, `low`
and `low`. Every finding was true. The review was accurate and unreadable.

### `bot/review/fingerprint.py:90`

`sha256(file_path + normalised(title + message))`, truncated to 16 hex chars.
Normalising strips backticked spans, quoted spans, numbers and stopwords, then
sorts the remaining words.

Measured behaviour — these produce the **same** fingerprint:

```
Hardcoded credential `sk-123` found at line 41
Hardcoded credential `sk-999` found at line 87     (literal and line moved)
found hardcoded credential at line 41 `sk-123`     (reordered)
```

And these do **not**:

```
Hardcoded credential at line 41                    (a content word dropped)
A secret is embedded directly in the source        (fully reworded)
```

It absorbs mechanical variation but not semantic rewording. An exact hash
cannot fix that, because the ledger's uniqueness is a database constraint and a
constraint needs an exact key — so cross-run dedupe will need a similarity
fallback alongside it. The module docstring says so.

---

## 8. The two weak spots

### Anchoring (`reviewer/models/anchor.py:119`)

`AnchorState` is `LINE`, `FILE` or `NONE`. Placement is decided by parsing the
diff's hunk headers — **never** by trusting the model's line number, because a
miscount would post a confident comment on unrelated code and GitHub only
rejects lines outside the diff entirely.

That protects against *unpostable* lines. It does not protect against *wrong*
ones. On the last run **7 of 12 findings carried no line at all**, and the
evidence for why is inside a single review:

```
a file it opened and read
  "ProductService.cs:57-70 validates SKU, price, stock"   exactly right

lines it counted through the diff
  "catch block at lines 65-68"     actual 77-80    wrong
  "constants at lines 17-18"       actual 16-17    wrong
  "constants at lines 11-12"       actual 16-17    wrong, and contradicts the line above
```

Counting lines through hunk headers is arithmetic; quoting a line is copying.
The fix is to ask for the quote and locate it in the checkout ourselves.

### Nothing is posted inline yet

`_preview_comment` (`worker.py:85`) dumps everything into one comment. The real
thing is: create a pending review, add one inline comment per anchored finding
with an invisible `<!-- reviewer:fp:… -->` marker, submit with
`event: "COMMENT"` — never `REQUEST_CHANGES`, so the bot can hold an opinion but
never block a merge.

`reviewer/github/publish.py` already does this for the CLI, over MCP. The bot
will either reuse it or do the same over REST.

---

## 9. Where to look for X

| Question | File |
|---|---|
| Why was this delivery ignored? | `bot/api.py:79` `classify` |
| How does a job get picked up? | `bot/db.py:113` `claim_job` |
| What can a specialist read? | `bot/review/local_tools.py:122` |
| What stops it reading `/etc/passwd`? | `reviewer/mcp/fs_server/tools.py:33` |
| What does a specialist get told? | `reviewer/agents/subagents/base.py:18` + `prompts/_shared/review_rules.md` |
| How are tasks split? | `reviewer/core/master.py:254` |
| Why did two findings become one? | `bot/review/adjudicate.py:191` |
| Why is this finding file-level? | `reviewer/models/anchor.py:119` |
| What is in the database? | `bot/db/schema.sql` |

---

## 10. Things that will surprise you

- **`reviewer/config.py` reads `os.environ` at import time.** Anything that
  imports it before `.env` is loaded sees nothing. `bot/config.py` loads dotenv
  at *its* import, and both entry points import `bot.config` first. But a bare
  `python -c "from reviewer.config import DEFAULT_API_KEY"` reports it missing.
- **The model endpoint is internal.** Without the VPN, calls hang on connect
  rather than failing — no error, no output. Indistinguishable from a bug in the
  review code.
- **`bot/db/schema.sql` runs once**, on first boot of an empty volume. Editing
  it later does nothing; `docker compose down -v` re-applies it.
- **The bot uses no MCP.** The only `mcp` in `bot/` is a module *name*. It used
  to read code through GitHub's MCP server; since the checkout it reads local
  files. `reviewer/` still uses MCP for the CLI's own paths.
- **Evidence is stored per finding**, so one run's tool calls appear once per
  finding it produced. Counting them naively inflates the numbers about fivefold
  — a mistake worth not repeating when reading the `findings` table.

---

## 11. Tests

`.venv/bin/python -m pytest -q` → **157 passing**.

The ones that encode a decision rather than checking a function:

| File | The property |
|---|---|
| `test_adjudicate.py` | A malformed merge plan never loses a finding — 5 of its 8 tests |
| `test_local_tools.py` | A path escaping the checkout is refused; `.git` and `.env` unreadable |
| `test_github_mcp.py` | Specialists only ever reach `/readonly` URLs; a required argument is promoted in the schema |
| `test_dispatch.py` | A failed specialist is flagged so publishers exclude it; every specialist receives the rubric |
| `test_fs_server_tools.py` | ripgrep and the Python fallback produce the identical output contract |
