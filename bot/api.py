"""The webhook endpoint. Fast, suspicious, and does no real work.

GitHub POSTs here and *waits on the connection* for a response. It gives up
after roughly ten seconds and records the delivery as failed — then retries.
A review takes minutes, so doing the review inside this handler means GitHub
hangs up, retries, and a second review starts while the first is still
running: double the cost, duplicate comments.

So the contract for everything in this file is: validate, write a row, answer.
Target is under 100ms. The worker does the rest, after GitHub has gone.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import FastAPI, Header, Request, Response

from bot import config, db

log = logging.getLogger("bot.api")

app = FastAPI(title="reviewer-bot", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def startup() -> None:
    problems = config.check()
    if problems:
        # Loud, but not fatal: the endpoint should still answer so you can see
        # deliveries arriving while you finish setting things up.
        log.error("configuration incomplete: %s", "; ".join(problems))
    await db.pool()
    log.info("api up — bot login %r, trigger %r", config.BOT_LOGIN, config.TRIGGER)


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "bot": config.BOT_LOGIN,
        "trigger": config.TRIGGER,
        "config_problems": config.check(),
        "jobs": await db.job_counts(),
    }


# --------------------------------------------------------------- guard 1 ----
def verify_signature(raw: bytes, header: str | None) -> bool:
    """HMAC-SHA256 over the RAW REQUEST BODY.

    The single most common way to get this wrong is to verify against
    re-serialized JSON. `json.dumps(json.loads(raw))` is not `raw` — key order,
    spacing and unicode escaping all differ — so the digest never matches and
    every delivery 401s. Hence `raw` is passed around as bytes and only parsed
    after this returns True.
    """
    if not config.WEBHOOK_SECRET:
        log.error("GITHUB_WEBHOOK_SECRET is not set; refusing every delivery")
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        config.WEBHOOK_SECRET.encode(), raw, hashlib.sha256
    ).hexdigest()
    # Constant time: a plain `==` leaks how much of the digest matched.
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# --------------------------------------------------------------- routing ----
def classify(event: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Decide whether this event is work for us, and what kind.

    Returns ``(intent, fields)`` or None. Cheap checks only — no database, no
    network. Anything needing either belongs in the worker.
    """
    action = payload.get("action")

    # --- a human summoning a review -------------------------------------
    if event == "issue_comment" and action == "created":
        issue = payload.get("issue") or {}
        # An issue_comment fires for plain issues too. This key is present
        # ONLY when the issue is a pull request.
        if "pull_request" not in issue:
            return None
        comment = payload.get("comment") or {}
        if config.TRIGGER not in (comment.get("body") or "").lower():
            return None
        return "review", {
            "pr_number": issue.get("number"),
            "trigger_comment_id": comment.get("id"),
            "requested_by": (comment.get("user") or {}).get("login", ""),
            "association": (comment.get("author_association") or "").upper(),
        }

    # --- a human disputing a finding ------------------------------------
    if event == "pull_request_review_comment" and action == "created":
        comment = payload.get("comment") or {}
        # A reply, not a new top-level comment. Whether the thread is one of
        # OURS is decided in the worker, which can look up the marker.
        if not comment.get("in_reply_to_id"):
            return None
        return "dispute", {
            "pr_number": (payload.get("pull_request") or {}).get("number"),
            "trigger_comment_id": comment.get("id"),
            "requested_by": (comment.get("user") or {}).get("login", ""),
            "association": (comment.get("author_association") or "").upper(),
        }

    # `pull_request` events are subscribed for later (detecting a push that
    # invalidates a stored review). Nothing consumes them yet.
    return None


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    # Raw bytes first, before anything parses them.
    raw = await request.body()

    # GUARD 1 — is this really from GitHub?
    if not verify_signature(raw, x_hub_signature_256):
        log.warning("rejected delivery %s: bad signature", x_github_delivery or "?")
        return Response(status_code=401, content='{"error":"bad signature"}',
                        media_type="application/json")

    payload = await request.json()
    event = x_github_event
    action = payload.get("action")
    sender = (payload.get("sender") or {}).get("login", "")

    # GUARD 2 — is it us? Our own comments and reactions come back as events.
    # Without this the bot answers itself in an unbounded loop.
    if config.BOT_LOGIN and sender == config.BOT_LOGIN:
        return _ok(x_github_delivery, "self")

    # GUARD 3 — have we already handled this delivery? GitHub's delivery is
    # at-least-once, so a repeat is normal rather than an error. Recorded
    # before the work is decided, so a crash mid-handler cannot double-enqueue.
    if x_github_delivery:
        first_time = await db.record_delivery(x_github_delivery, event, action)
        if not first_time:
            return _ok(x_github_delivery, "duplicate")

    # GUARD 4 — is it work for us at all?
    classified = classify(event, payload)
    if classified is None:
        return _ok(x_github_delivery, "not for us")
    intent, fields = classified

    repository = payload.get("repository") or {}
    owner = ((repository.get("owner") or {}).get("login")) or ""
    repo = repository.get("name") or ""
    installation_id = (payload.get("installation") or {}).get("id")

    if not (owner and repo and fields.get("pr_number") and installation_id):
        log.warning("delivery %s classified %s but incomplete", x_github_delivery, intent)
        return _ok(x_github_delivery, "incomplete payload")

    # Authority. A human's words become part of an agent instruction, so
    # whoever can comment could otherwise write the agent's prompt. Checked
    # here to avoid queueing work we would refuse anyway; the worker checks
    # again, because a guard worth having is worth having twice.
    if fields["association"] not in config.ALLOWED_ASSOCIATIONS:
        log.info(
            "ignoring %s from @%s (%s not permitted)",
            intent, fields["requested_by"], fields["association"] or "NONE",
        )
        return _ok(x_github_delivery, "not permitted")

    job_id = await db.enqueue(
        delivery_id=x_github_delivery,
        intent=intent,
        owner=owner,
        repo=repo,
        pr_number=fields["pr_number"],
        installation_id=installation_id,
        requested_by=fields["requested_by"],
        trigger_comment_id=fields["trigger_comment_id"],
        payload=payload,
    )
    log.info(
        "queued job %s: %s %s/%s#%s by @%s",
        job_id, intent, owner, repo, fields["pr_number"], fields["requested_by"],
    )
    return _ok(x_github_delivery, None, job_id=job_id)


def _ok(delivery: str, skipped: str | None, **extra: Any) -> Response:
    import json as _json

    body: dict[str, Any] = {"ok": True, "delivery": delivery}
    if skipped:
        body["skipped"] = skipped
    body.update(extra)
    return Response(content=_json.dumps(body), media_type="application/json")
