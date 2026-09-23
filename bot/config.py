"""Bot settings, read from the environment once at import.

Same shape as ``reviewer/config.py`` — module-level constants from
``os.environ`` — so there is one convention, not two. ``.env`` is loaded here
because both the API and the worker need it and neither owns it.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Real env vars win, so compose's `environment:` overrides the file.
load_dotenv(find_dotenv(usecwd=True), override=False)
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


class ConfigError(RuntimeError):
    """A setting the bot cannot run without is missing."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Run `python scripts/preflight.py` to see what is missing."
        )
    return value


# ------------------------------------------------------------- the App ------
APP_ID = os.environ.get("GITHUB_APP_ID", "").strip()
PRIVATE_KEY_PATH = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "./secrets/app.pem").strip()
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()

# Exactly as GitHub shows it, `[bot]` suffix included. The loop guard compares
# `sender.login` to this: a typo means the bot answers itself forever.
BOT_LOGIN = os.environ.get("GITHUB_BOT_LOGIN", "").strip()

GITHUB_API = os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def private_key() -> str:
    path = Path(PRIVATE_KEY_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / PRIVATE_KEY_PATH.lstrip("./")
    if not path.exists():
        raise ConfigError(f"App private key not found at {path}")
    return path.read_text()


# ------------------------------------------------------------- stores -------
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://reviewer:reviewer@localhost:5432/reviewer"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ------------------------------------------------------------- worker ------
WORKER_ID = os.environ.get("WORKER_ID", "worker-local")

# Must exceed the longest plausible review, or a slow one runs twice.
JOB_CLAIM_TIMEOUT_SECONDS = int(os.environ.get("BOT_JOB_CLAIM_TIMEOUT", "1800"))

# Empty-queue poll. LISTEN/NOTIFY would be tidier; one query a second is nothing.
POLL_INTERVAL_SECONDS = float(os.environ.get("BOT_POLL_INTERVAL", "1.0"))

# A deferral is not a failure, so it must not spend an attempt — but without a
# ceiling a stale lock from a `kill -9` retries for ever, one log line a second.
DEFER_BACKOFF_SECONDS = float(os.environ.get("BOT_DEFER_BACKOFF", "15"))
MAX_JOB_DEFERRALS = int(os.environ.get("BOT_MAX_JOB_DEFERRALS", "20"))

MAX_JOB_ATTEMPTS = int(os.environ.get("BOT_MAX_JOB_ATTEMPTS", "3"))

# Per-PR lock TTL. Bounds the damage if a worker dies without releasing.
PR_LOCK_TTL_SECONDS = int(os.environ.get("BOT_PR_LOCK_TTL", "2400"))

# ------------------------------------------------------------- trigger ------
# What a human types to summon the bot. Matched case-insensitively against the
# comment body.
TRIGGER = os.environ.get("BOT_TRIGGER", "@polyidus-bot").strip().lower()

# Review, but print every comment instead of posting it. GitHub only allows
# deleting PENDING reviews, so each test run would otherwise leave a permanent
# summary. Implies force: with nothing posted there is no record of the commit
# having been reviewed.
#
#   BOT_DRY_RUN=1 .venv/bin/python -m bot.worker
DRY_RUN = os.environ.get("BOT_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

# A human comment becomes part of an agent instruction, so without this an
# outside contributor could steer the agent from their own pull request.
ALLOWED_ASSOCIATIONS = frozenset(
    a.strip().upper()
    for a in os.environ.get("BOT_ALLOWED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR").split(",")
    if a.strip()
)


def check() -> list[str]:
    """Names of settings that are missing. Empty means good to start."""
    problems = []
    for name, value in [
        ("GITHUB_APP_ID", APP_ID),
        ("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET),
        ("GITHUB_BOT_LOGIN", BOT_LOGIN),
    ]:
        if not value:
            problems.append(name)
    try:
        private_key()
    except ConfigError as exc:
        problems.append(str(exc))
    return problems
