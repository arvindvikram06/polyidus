# Running the reviewer on Claude

Two backends, one switch. GLM stays the default; nothing about it changes.

| `REVIEWER_LLM` | Backend | Model |
|---|---|---|
| unset / `glm` | Presidio gateway | `bedrock/zai.glm-5` |
| `claude` | local CLIProxyAPI | `claude-sonnet-5` |

## One-time setup

1. **Install CLIProxyAPI** — a single Go binary: `brew install cliproxyapi`,
   or grab it from
   [the releases page](https://github.com/router-for-me/CLIProxyAPI/releases).

2. **Sign the proxy into your Claude Pro subscription.** One OAuth login,
   the same one Claude Code uses; the token is written to `~/.cli-proxy-api`
   and reused on every start:

   ```bash
   cliproxyapi -claude-login --config cliproxy/config.yaml
   ```

   A browser opens — approve, and the terminal confirms. Add `-no-browser` if
   you would rather copy the URL yourself.

   There is no API key in this setup. Anthropic's consumer terms cover a Pro
   subscription for use through their own clients, so driving it from here is
   your call and your account's risk. `config.yaml` keeps a commented
   `claude-api-key` block for the day you would rather pay per token than live
   inside the subscription's usage windows.

3. **Add the inbound key to `.env`.** This is what the reviewer sends to reach
   the proxy, and it must match `api-keys` in the config:

   ```
   REVIEWER_CLAUDE_PROXY_KEY=local-reviewer
   ```

> `cliproxy/config.yaml` and its backups are gitignored — they hold live
> credentials. Keep it that way. The OAuth token itself never enters the repo;
> it lives in `~/.cli-proxy-api`.

## Each session

Terminal 1 — the proxy:

```bash
cliproxyapi --config cliproxy/config.yaml
```

Terminal 2 — the worker, on Claude:

```bash
REVIEWER_LLM=claude .venv/bin/python -m bot.worker
```

It prints the backend on startup, so a run is never ambiguous:

```
model backend — claude: claude-sonnet-5 via http://127.0.0.1:8317/v1
```

Drop the prefix and you are back on GLM.

## Checking it works before a review

```bash
curl -s http://127.0.0.1:8317/v1/models \
  -H "Authorization: Bearer local-reviewer" | head -20
```

## Per-setting overrides

The profile sets four things together — base URL, key, master model, subagent
model — because they only make sense together. To change one, use a
profile-scoped variable:

```
REVIEWER_CLAUDE_MASTER_MODEL=claude-sonnet-4-5-20250929
```

Valid ids are whatever `GET /v1/models` lists above — the subscription serves
the full Claude line, `claude-opus-5` and `claude-haiku-4-5-20251001` included.
Opus burns the usage window several times faster than Sonnet, which is why the
profile defaults to `claude-sonnet-5` for both master and subagents.

The unprefixed `REVIEWER_BASE_URL` and `REVIEWER_MASTER_MODEL` in `.env`
configure the **GLM** backend only. They are ignored under another profile on
purpose — otherwise switching would keep pointing at the GLM gateway and ask
it for a model it does not serve.

## When it stops working

- **401 from the proxy** — the OAuth token expired past refresh. Re-run
  `cliproxyapi -claude-login --config cliproxy/config.yaml`.
- **429** — you are out of subscription window, not out of money. Wait for the
  window to roll, or uncomment `claude-api-key` in `config.yaml` to fall back
  to per-token billing.
- **Connection refused** — the proxy is not running. It is a foreground
  process; nothing restarts it for you.
