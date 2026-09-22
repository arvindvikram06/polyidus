"""What this pull request already knows, read back off GitHub.

A second review is not a first review run twice: by then there are findings
posted, threads resolved, and findings the bot withdrew when someone pushed back.

Fingerprinting comments and dropping matching hashes failed because models
reword — one SQL injection came back as "SQL Injection in
FindBySupplierCodeAsync" and "SQL Injection via supplierCode parameter". Different
hash, so the code posted a duplicate *and* replied "looks fixed" on a live bug.

So the comparison happens in front of the model, before it plans: "is this the
same defect?" is a judgement, not a string match.

The withdrawal case matters most. A finding the bot conceded must never be
raised again — the human already spent the effort arguing it down.
"""

from __future__ import annotations

import logging
from typing import Any

from bot.github import client

log = logging.getLogger("bot.review.history")

# Newest first: a finding from the most recent review is the one most likely to
# still stand, and fifty threads is telling us something other than "all of them".
MAX_THREADS = 25
_BODY_CHARS = 400
_REPLY_CHARS = 300


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        text = line.strip().strip("*").strip()
        if text:
            return text
    return "(no title)"


def _body_without_marker(body: str) -> str:
    keep = [
        line for line in (body or "").splitlines()
        if not line.strip().startswith("<!--") and not line.strip().startswith("<sub>")
    ]
    return "\n".join(keep).strip()


async def fetch(
    installation_id: int, owner: str, repo: str, pr_number: int, bot_login: str
) -> tuple[str, list[dict[str, Any]]]:
    """Render the previous review as prompt text.

    Returns ``(text, roots)``; ``roots`` is the thread-root comments, needed to
    reply into the right thread. Never raises — a review that cannot read its
    own history is still a useful review.
    """
    try:
        comments = await client.list_review_comments(installation_id, owner, repo, pr_number)
    except client.GitHubError as exc:
        log.warning("could not read previous comments (%s); reviewing without history", exc)
        return "", []

    roots = [
        c for c in comments
        if not c.get("in_reply_to_id") and (c.get("user") or {}).get("login") == bot_login
    ]
    if not roots:
        return "", []

    replies: dict[int, list[dict[str, Any]]] = {}
    for c in comments:
        parent = c.get("in_reply_to_id")
        if parent:
            replies.setdefault(parent, []).append(c)

    resolved_ids = await _resolved_root_ids(installation_id, owner, repo, pr_number)

    roots = sorted(roots, key=lambda c: c.get("id", 0), reverse=True)[:MAX_THREADS]

    lines = [
        "PREVIOUS REVIEW OF THIS PULL REQUEST",
        "",
        (
            "You have reviewed this pull request before. These are the comments "
            "you left, and any reply. Read them before planning."
        ),
        "",
    ]
    for index, root in enumerate(roots, start=1):
        where = root.get("path", "?")
        line_no = root.get("line") or root.get("original_line")
        lines.append(f"[{index}] {where}:{line_no}  {_first_line(root.get('body') or '')}")
        body = _body_without_marker(root.get("body") or "")
        if body:
            lines.append(f"      {body[:_BODY_CHARS]}")

        state = "RESOLVED by a human" if root.get("id") in resolved_ids else "open"
        lines.append(f"      status: {state}")

        for reply in replies.get(root.get("id"), []):
            who = (reply.get("user") or {}).get("login", "?")
            text = " ".join((reply.get("body") or "").split())[:_REPLY_CHARS]
            label = "you replied" if who == bot_login else f"@{who} replied"
            lines.append(f"      {label}: {text}")
        lines.append("")

    lines += [
        "How to use this:",
        "",
        (
            "1. A finding below is ALREADY REPORTED. Do not report it again — "
            "the comment is still on the pull request where the author can see "
            "it. Judge by meaning, not wording: the same defect described in "
            "different words is the same defect."
        ),
        (
            "2. Where you replied that a finding was WITHDRAWN or CONCEDED, it "
            "is settled. Never raise it again, however it is worded."
        ),
        (
            "3. A RESOLVED thread was closed by a human. Leave it closed "
            "unless the problem is demonstrably back."
        ),
        (
            "4. Spend this review on what is NEW. Where it is cheap, dispatch a "
            "specialist to open the file and check whether an open finding "
            "above has actually been fixed — say so explicitly if it has, "
            "because a finding that is merely absent from your output is not "
            "evidence that it was fixed."
        ),
        "",
    ]
    return "\n".join(lines), roots


async def _resolved_root_ids(
    installation_id: int, owner: str, repo: str, pr_number: int
) -> set[int]:
    """Which threads a human has resolved.

    REST has no `isResolved` on a review comment, so this needs GraphQL. Best
    effort: without it a resolved thread reads as open, the safe direction.
    """
    try:
        thread = await client.review_threads(installation_id, owner, repo, pr_number)
    except client.GitHubError as exc:
        log.info("could not read thread resolution (%s); treating all as open", exc)
        return set()
    return {t["root_id"] for t in thread if t["isResolved"] and t.get("root_id")}
