-- Reviewer bot schema: the job queue, and nothing else.
--
-- There are deliberately no tables for findings, disputes or review history.
-- Every finding is posted as an inline comment, which opens a thread on the
-- line it concerns — and a thread already carries an id, a file and line, an
-- ordered list of replies, and a resolved flag. That is the row we would have
-- written, except GitHub also renders it, notifies on it, and lets a human
-- edit and resolve it.
--
-- An earlier design mirrored all of that into Postgres. It was deleted: two
-- records of the same conversation can disagree, and the one the human can
-- see is the one that must win.
--
-- What remains has nothing to do with review state. These two tables exist
-- because we own the webhook, and GitHub allows it about ten seconds to answer
-- while delivering at least once.
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
    -- Counted separately from attempts. A deferral means another worker holds
    -- this pull request, which is not the job's fault and must not spend an
    -- attempt — but it still needs a ceiling, or a lock nobody releases keeps
    -- the job alive for ever.
    deferrals    INTEGER     NOT NULL DEFAULT 0,
    claimed_by   TEXT,                      -- WORKER_ID
    claimed_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error        TEXT,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The claim query's index: oldest queued job first.
CREATE INDEX jobs_claimable_idx ON jobs (status, created_at) WHERE status = 'queued';
CREATE INDEX jobs_pr_idx ON jobs (owner, repo, pr_number);
