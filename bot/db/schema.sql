-- Reviewer bot schema.
--
-- Applied once, on first boot of an empty Postgres volume, by the bind mount
-- in docker-compose.yml. Changing this file does NOT re-run it — wipe the
-- volume with `docker compose down -v` while iterating, or write a migration
-- once there is data worth keeping.

-- ---------------------------------------------------------------------------
-- 1. deliveries — the replay guard.
--
-- GitHub guarantees AT-LEAST-ONCE webhook delivery, so the same event can
-- legitimately arrive twice. The primary key is what makes a repeat a no-op:
-- the second INSERT raises a unique violation and the handler returns 200
-- without enqueueing a second job.
-- ---------------------------------------------------------------------------
CREATE TABLE deliveries (
    delivery_id  TEXT        PRIMARY KEY,   -- X-GitHub-Delivery header (a UUID)
    event        TEXT        NOT NULL,      -- X-GitHub-Event header
    action       TEXT,                      -- payload.action, when present
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deliveries older than a couple of weeks cannot still be retried by GitHub,
-- so this table is safe to prune on that index.
CREATE INDEX deliveries_received_at_idx ON deliveries (received_at);

-- ---------------------------------------------------------------------------
-- 2. jobs — the queue.
--
-- The API server INSERTs and returns. Workers claim with
--   SELECT ... FOR UPDATE SKIP LOCKED
-- which lets several workers take different rows concurrently without
-- blocking each other and without two of them taking the same row.
--
-- A job row surviving a worker crash is the point: the claim goes stale and
-- another worker picks it up. An in-memory handoff would lose it.
-- ---------------------------------------------------------------------------
CREATE TYPE job_status AS ENUM ('queued', 'claimed', 'done', 'failed', 'dead');
CREATE TYPE job_intent AS ENUM ('review', 'dispute');

CREATE TABLE jobs (
    id           BIGSERIAL   PRIMARY KEY,
    delivery_id  TEXT        NOT NULL REFERENCES deliveries (delivery_id),
    intent       job_intent  NOT NULL,
    status       job_status  NOT NULL DEFAULT 'queued',

    -- Denormalised out of the payload so a worker can take the PR lock and
    -- report progress without parsing JSON first.
    owner        TEXT        NOT NULL,
    repo         TEXT        NOT NULL,
    pr_number    INTEGER     NOT NULL,
    installation_id BIGINT   NOT NULL,
    requested_by TEXT        NOT NULL,      -- the login that triggered it
    trigger_comment_id BIGINT,              -- for the reaction, and for replies

    payload      JSONB       NOT NULL,      -- the raw webhook body

    attempts     INTEGER     NOT NULL DEFAULT 0,
    claimed_by   TEXT,                      -- WORKER_ID
    claimed_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error        TEXT,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The claim query's index: oldest queued job first.
CREATE INDEX jobs_claimable_idx ON jobs (status, created_at) WHERE status = 'queued';
CREATE INDEX jobs_pr_idx ON jobs (owner, repo, pr_number);

-- ---------------------------------------------------------------------------
-- 3. runs — one review invocation, for audit.
--
-- Tier 1 of the three persistence tiers: answers "why did it say that?" weeks
-- later. Prunable; nothing depends on it.
-- ---------------------------------------------------------------------------
CREATE TABLE runs (
    id           UUID        PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES jobs (id),
    owner        TEXT        NOT NULL,
    repo         TEXT        NOT NULL,
    pr_number    INTEGER     NOT NULL,
    head_sha     TEXT        NOT NULL,
    base_sha     TEXT,

    summary      TEXT,
    routing      JSONB,                     -- the master's dispatched tasks
    master_trace JSONB,
    aborted      TEXT,                      -- non-null when it stopped early

    prompt_tokens     INTEGER,
    completion_tokens INTEGER,

    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

CREATE INDEX runs_pr_idx ON runs (owner, repo, pr_number, started_at DESC);

-- ---------------------------------------------------------------------------
-- 4. findings — the PR ledger. Tier 2, and the load-bearing table.
--
-- One row per (PR, fingerprint). The fingerprint is a hash of the file path
-- plus the message with numbers and identifiers normalised out, so the same
-- problem hashes the same across runs even when the wording drifts and the
-- line has moved.
--
-- That unique constraint is the entire dedupe mechanism. Without it the bot
-- re-posts its whole review on every invocation, which makes it unusable on
-- any PR that iterates.
-- ---------------------------------------------------------------------------
CREATE TYPE finding_status AS ENUM (
    'proposed',    -- a specialist emitted it, not yet placed
    'suppressed',  -- master dropped it: duplicate, thin evidence, below floor
    'orphaned',    -- no line in this diff; goes in the review body
    'posted',      -- published on the PR
    'disputed',    -- a human replied disagreeing
    'upheld',      -- re-checked after a dispute, still stands
    'rejected',    -- re-checked after a dispute, conceded. NEVER raise again.
    'resolved',    -- the code changed and it is gone
    'stale'        -- the head moved before it could be posted
);

CREATE TABLE findings (
    id           UUID        PRIMARY KEY,
    run_id       UUID        NOT NULL REFERENCES runs (id),

    owner        TEXT        NOT NULL,
    repo         TEXT        NOT NULL,
    pr_number    INTEGER     NOT NULL,
    fingerprint  TEXT        NOT NULL,

    -- provenance: who says so, and on what grounds
    subagent     TEXT        NOT NULL,
    verified_by  TEXT,
    evidence     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    model_used   TEXT,

    -- location. head_sha matters: a line number is meaningless without the
    -- commit it refers to.
    file_path    TEXT        NOT NULL,
    line_start   INTEGER,
    line_end     INTEGER,
    hunk_header  TEXT,
    -- The source line the specialist quoted. Kept because it is the input to
    -- line resolution: when a finding ends up with no line, this is the only
    -- way to tell whether the specialist gave no quote or gave one that did
    -- not match the file.
    offending_line TEXT,
    head_sha     TEXT        NOT NULL,

    severity     TEXT        NOT NULL,      -- critical|high|medium|low|info
    title        TEXT        NOT NULL,
    message      TEXT        NOT NULL,
    suggested_patch TEXT,

    anchor_state TEXT,                      -- LINE | FILE | NONE
    status       finding_status NOT NULL DEFAULT 'proposed',
    github_comment_id BIGINT,               -- filled once published

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT findings_pr_fingerprint_key UNIQUE (owner, repo, pr_number, fingerprint)
);

CREATE INDEX findings_ledger_idx ON findings (owner, repo, pr_number, status);
-- The dispute loop looks a finding up by the comment a human replied to.
CREATE INDEX findings_comment_idx ON findings (github_comment_id)
    WHERE github_comment_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. scratchpads — a specialist's working notes.
--
-- Specialists re-read files they already read, because nothing persists
-- between tool calls except the context window, which is the thing we are
-- trying to protect. `ruled_out` doubles as evidence when a human disputes a
-- finding: you can show what the agent already considered.
-- ---------------------------------------------------------------------------
CREATE TABLE scratchpads (
    run_id       UUID        NOT NULL REFERENCES runs (id),
    agent        TEXT        NOT NULL,
    task_index   INTEGER     NOT NULL,

    task         TEXT        NOT NULL,
    files_scoped TEXT[]      NOT NULL DEFAULT '{}',
    files_read   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ruled_out    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    open_questions JSONB     NOT NULL DEFAULT '[]'::jsonb,
    confirmed    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    tool_calls   INTEGER     NOT NULL DEFAULT 0,

    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, agent, task_index)
);

-- ---------------------------------------------------------------------------
-- 6. dispute_threads — the conversation.
--
-- Keyed by fingerprint rather than finding id: a re-review creates a new
-- findings row with a new UUID but the same fingerprint, and a thread opened
-- three days ago must still resolve to it.
-- ---------------------------------------------------------------------------
CREATE TYPE dispute_outcome AS ENUM ('open', 'upheld', 'conceded');

CREATE TABLE dispute_threads (
    root_comment_id BIGINT   PRIMARY KEY,   -- the thread root on GitHub
    owner        TEXT        NOT NULL,
    repo         TEXT        NOT NULL,
    pr_number    INTEGER     NOT NULL,
    fingerprint  TEXT        NOT NULL,

    turns        JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- {author, body, at, was_agent}
    outcome      dispute_outcome NOT NULL DEFAULT 'open',

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX dispute_threads_pr_idx ON dispute_threads (owner, repo, pr_number);

-- ---------------------------------------------------------------------------
-- 7. rejections — tier 3, the corpus. Outlives the PR on purpose.
--
-- Every finding a human overturned, with their stated reason. Grouped by
-- specialist, this is the only honest signal about which agent is
-- confidently wrong and about what.
-- ---------------------------------------------------------------------------
CREATE TABLE rejections (
    id           BIGSERIAL   PRIMARY KEY,
    fingerprint  TEXT        NOT NULL,
    subagent     TEXT        NOT NULL,
    severity     TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    message      TEXT        NOT NULL,
    human_reason TEXT        NOT NULL,      -- verbatim, from the dispute
    rejected_by  TEXT        NOT NULL,
    owner        TEXT        NOT NULL,
    repo         TEXT        NOT NULL,
    pr_number    INTEGER     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX rejections_subagent_idx ON rejections (subagent, created_at DESC);
