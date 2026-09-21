#!/usr/bin/env python
"""Check that everything built so far still works.

Run this at the start of a session, or after changing anything in .env:

    .venv/bin/python scripts/preflight.py

Every check is independent and prints PASS, FAIL, or SKIP. SKIP means the
thing it checks has not been built yet — that is expected, not a problem.
Nothing here writes to GitHub or to the database.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    mark = {PASS: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[status]
    print(f"[{mark}] {name}" + (f"\n          {detail}" if detail else ""))


def load_env() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def gh(path: str, token: str, method: str = "GET"):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # network down, DNS, timeout
        return 0, str(e)


# ---------------------------------------------------------------- 1. config --
env = load_env()
required = [
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_BOT_LOGIN",
    "SMEE_URL",
    "BOT_TEST_REPO",
    "DATABASE_URL",
    "REDIS_URL",
]
missing = [k for k in required if not env.get(k)]
record(
    "1. .env has every key the bot needs",
    FAIL if missing else PASS,
    f"missing or empty: {', '.join(missing)}" if missing else f"{len(required)} keys set",
)

# ------------------------------------------------------------ 2. private key --
pem_path = ROOT / env.get("GITHUB_APP_PRIVATE_KEY_PATH", "secrets/app.pem").lstrip("./")
if not pem_path.exists():
    record("2. App private key present and valid RSA", FAIL, f"not found: {pem_path}")
else:
    proc = subprocess.run(
        ["openssl", "rsa", "-in", str(pem_path), "-noout", "-check"],
        capture_output=True,
        text=True,
        check=False,
    )
    mode = oct(pem_path.stat().st_mode)[-3:]
    ok = proc.returncode == 0
    record(
        "2. App private key present and valid RSA",
        PASS if ok else FAIL,
        f"{pem_path} mode {mode}" + ("" if ok else f" — {proc.stderr.strip()[:120]}"),
    )
    if ok and mode != "600":
        record("2b. key permissions are 600", FAIL, f"mode is {mode}; run: chmod 600 {pem_path}")

# --------------------------------------------------------- 3. the auth chain --
if missing or not pem_path.exists():
    record("3. GitHub App auth chain", SKIP, "needs config and key above")
else:
    try:
        import jwt  # PyJWT
    except ImportError:
        record("3. GitHub App auth chain", SKIP, "PyJWT not installed in this interpreter")
    else:
        now = int(time.time())
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": env["GITHUB_APP_ID"]},
            pem_path.read_text(),
            algorithm="RS256",
        )
        st, app = gh("/app", app_jwt)
        if st != 200:
            record("3. GitHub App auth chain", FAIL, f"GET /app -> {st}: {app}")
        else:
            record(
                "3a. key signs a JWT the App accepts",
                PASS,
                f"{app['slug']} (id {app['id']})",
            )

            expected_login = f"{app['slug']}[bot]"
            configured = env.get("GITHUB_BOT_LOGIN", "")
            record(
                "3b. GITHUB_BOT_LOGIN matches the real App slug",
                PASS if configured == expected_login else FAIL,
                f"configured {configured!r}"
                + ("" if configured == expected_login else f", expected {expected_login!r}"),
            )

            want_events = {"issue_comment", "pull_request", "pull_request_review_comment"}
            have_events = set(app.get("events") or [])
            record(
                "3c. subscribed to all three events",
                PASS if want_events <= have_events else FAIL,
                f"has {sorted(have_events)}"
                + ("" if want_events <= have_events else f", missing {sorted(want_events - have_events)}"),
            )

            want_perms = {"contents": "read", "pull_requests": "write", "issues": "write"}
            have_perms = app.get("permissions") or {}
            bad = {k: (v, have_perms.get(k)) for k, v in want_perms.items() if have_perms.get(k) != v}
            record(
                "3d. permissions are right",
                PASS if not bad else FAIL,
                "contents:read, pull_requests:write, issues:write"
                if not bad
                else f"wrong: {bad}",
            )

            st, insts = gh("/app/installations", app_jwt)
            if st != 200 or not insts:
                record("3e. installed somewhere", FAIL, f"-> {st}: {insts}")
            else:
                inst = insts[0]
                record(
                    "3e. installed somewhere",
                    PASS,
                    f"install {inst['id']} on {inst['account']['login']} "
                    f"({inst.get('repository_selection')})",
                )
                st, tok = gh(f"/app/installations/{inst['id']}/access_tokens", app_jwt, "POST")
                if st != 201:
                    record("3f. can mint an installation token", FAIL, f"-> {st}: {tok}")
                else:
                    record("3f. can mint an installation token", PASS, f"expires {tok['expires_at']}")
                    owner, _, repo = env["BOT_TEST_REPO"].partition("/")
                    st, pr = gh(f"/repos/{owner}/{repo}/pulls/1", tok["token"])
                    record(
                        "3g. can read the test PR as the bot",
                        PASS if st == 200 else FAIL,
                        f"#{pr['number']} {pr['title']!r}, head {pr['head']['sha'][:8]}, "
                        f"{pr['changed_files']} files"
                        if st == 200
                        else f"-> {st}: {pr}",
                    )

# ------------------------------------------------------------- 4. the engine --
try:
    from reviewer.agents.catalog import load_specialists

    specs = load_specialists()
    record(
        "4. review engine importable, specialists load",
        PASS if len(specs) == 4 else FAIL,
        f"{len(specs)}: {', '.join(sorted(specs))}",
    )
except Exception as e:
    record("4. review engine importable, specialists load", FAIL, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- 5. stores --
def port_open(url: str) -> bool:
    import socket

    m = re.search(r"@?([\w.-]+):(\d+)", url)
    if not m:
        return False
    try:
        with socket.create_connection((m.group(1), int(m.group(2))), timeout=2):
            return True
    except OSError:
        return False


for label, key in [("Postgres", "DATABASE_URL"), ("Redis", "REDIS_URL")]:
    url = env.get(key, "")
    if not url:
        record(f"5. {label} reachable", SKIP, f"{key} not set")
    elif port_open(url):
        record(f"5. {label} reachable", PASS, url.split("@")[-1])
    else:
        record(
            f"5. {label} reachable",
            SKIP,
            "not running — start with: docker compose up -d postgres redis",
        )

# --------------------------------------------------------------- 6. not yet --
for label, path in [
    ("Dockerfile", "Dockerfile"),
    ("bot package", "bot/__init__.py"),
    ("database schema", "bot/db/schema.sql"),
]:
    p = ROOT / path
    record(f"6. {label}", PASS if p.exists() else SKIP, str(path) if p.exists() else "step 2")

# ----------------------------------------------------------------- summary --
print()
n_fail = sum(1 for _, s, _ in results if s == FAIL)
n_pass = sum(1 for _, s, _ in results if s == PASS)
n_skip = sum(1 for _, s, _ in results if s == SKIP)
print(f"{n_pass} passed, {n_fail} failed, {n_skip} not built yet")
if n_fail:
    print("\nfailed:")
    for name, status, detail in results:
        if status == FAIL:
            print(f"  - {name}: {detail}")
sys.exit(1 if n_fail else 0)
