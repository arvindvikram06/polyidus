#!/usr/bin/env python
"""Send synthetic, correctly-signed webhooks at a running bot.

Exercises every guard in bot/api.py without waiting on GitHub. Much faster
than commenting on a real PR, and it can send the cases GitHub will not
easily produce on demand — a replayed delivery, a comment from the bot
itself, an unauthorised author.

    .venv/bin/python scripts/fake_webhook.py            # run every case
    .venv/bin/python scripts/fake_webhook.py --case dup # just one

Reads GITHUB_WEBHOOK_SECRET from .env, so a signature mismatch here means the
same mismatch would happen with GitHub.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent

env = {
    m.group(1): m.group(2).strip().strip('"')
    for line in (ROOT / ".env").read_text().splitlines()
    if (m := re.match(r"^([A-Z_]+)=(.*)$", line.strip()))
}
SECRET = env["GITHUB_WEBHOOK_SECRET"]
BOT = env.get("GITHUB_BOT_LOGIN", "polyidus-bot[bot]")
TRIGGER = env.get("BOT_TRIGGER", "@polyidus-bot")
OWNER, _, REPO = env.get("BOT_TEST_REPO", "arvindvikram06/test-proj").partition("/")

URL = "http://127.0.0.1:8000/webhook"
INSTALLATION_ID = 162818087
PR = 1


def comment_event(
    *,
    body: str = f"{TRIGGER} review",
    sender: str = "arvindvikram06",
    association: str = "OWNER",
    is_pr: bool = True,
) -> dict:
    """An `issue_comment.created` payload, trimmed to the fields we read."""
    issue: dict = {"number": PR}
    if is_pr:
        # Present ONLY when the issue is a pull request. Its absence is what
        # tells the handler a plain issue comment is not for us.
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/{PR}"}
    return {
        "action": "created",
        "issue": issue,
        "comment": {
            "id": 999000001,
            "body": body,
            "user": {"login": sender},
            "author_association": association,
        },
        "repository": {"name": REPO, "owner": {"login": OWNER}},
        "sender": {"login": sender},
        "installation": {"id": INSTALLATION_ID},
    }


def send(event: str, payload: dict, *, delivery: str | None = None, sign: bool = True):
    raw = json.dumps(payload).encode()
    delivery = delivery or str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }
    if sign:
        digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    else:
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    req = urllib.request.Request(URL, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except OSError as e:
        print(f"cannot reach {URL}: {e}", file=sys.stderr)
        print("start it with:  .venv/bin/uvicorn bot.api:app --port 8000", file=sys.stderr)
        sys.exit(2)


# name -> (description, expected status, expected `skipped` value or None)
CASES: dict[str, tuple[str, int, str | None]] = {
    "badsig": ("signature does not match", 401, None),
    "valid": ("a maintainer tags the bot on a PR", 200, None),
    "dup": ("the same delivery id arrives twice", 200, "duplicate"),
    "self": ("the bot's own comment comes back as an event", 200, "self"),
    "notpr": ("issue_comment on a plain issue, not a PR", 200, "not for us"),
    "notrigger": ("a comment that never mentions the bot", 200, "not for us"),
    "outsider": ("a CONTRIBUTOR tries to command the bot", 200, "not permitted"),
}


def run(name: str) -> bool:
    desc, want_status, want_skip = CASES[name]

    if name == "badsig":
        status, body = send("issue_comment", comment_event(), sign=False)
    elif name == "valid":
        status, body = send("issue_comment", comment_event())
    elif name == "dup":
        fixed = str(uuid.uuid4())
        send("issue_comment", comment_event(), delivery=fixed)  # first time
        status, body = send("issue_comment", comment_event(), delivery=fixed)
    elif name == "self":
        status, body = send("issue_comment", comment_event(sender=BOT))
    elif name == "notpr":
        status, body = send("issue_comment", comment_event(is_pr=False))
    elif name == "notrigger":
        status, body = send("issue_comment", comment_event(body="looks good to me"))
    elif name == "outsider":
        status, body = send(
            "issue_comment", comment_event(sender="someone-else", association="CONTRIBUTOR")
        )
    else:  # pragma: no cover
        raise SystemExit(f"unknown case {name}")

    got_skip = body.get("skipped") if isinstance(body, dict) else None
    ok = status == want_status and got_skip == want_skip
    mark = "  ok  " if ok else " FAIL "
    print(f"[{mark}] {name:<10} {desc}")
    print(f"           -> {status} {json.dumps(body) if isinstance(body, dict) else body}")
    if not ok:
        print(f"           expected {want_status} skipped={want_skip!r}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(CASES))
    args = ap.parse_args()

    names = [args.case] if args.case else list(CASES)
    results = [run(n) for n in names]
    print()
    print(f"{sum(results)}/{len(results)} guards behaved correctly")
    sys.exit(0 if all(results) else 1)
