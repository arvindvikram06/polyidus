# legacy/

The original command-line reviewer, and the MCP machinery it used. **Archived,
not deleted** — it still imports, still runs, and its tests still pass.

```bash
reviewer review --pr 1          # still works
reviewer --help
```

## Why it moved

The bot replaced how this code gets its input, its credentials and its output:

| | CLI (here) | Bot (`bot/`) |
|---|---|---|
| Trigger | you type a command | a comment on a pull request |
| Credential | `GITHUB_TOKEN`, a personal token | GitHub App installation token |
| Reads code | GitHub's hosted **MCP** server | a **git checkout** on disk |
| Stores results | JSON files in `.reviewer/` | Postgres |
| Publishes | MCP pending review | not yet built |

The measurement that ended the MCP path: reviewing through GitHub's API, three
specialists made 18 `get_file_contents` calls of which **17 failed**, and all
four `search_code` calls returned nothing. `search_code` has no `ref`
parameter, so it can only ever search a repository's default branch — never the
code a pull request adds. From a checkout, the same search returns five matches
instantly.

## What is still shared

The **engine** stays in `reviewer/` and both consumers use it:

```
reviewer/
├── config.py
├── core/master.py          the master/specialist orchestration
├── core/tracer.py
├── agents/                 specialist prompts, catalog, shared rules
├── models/                 findings, anchoring, diff handling
└── sandbox/files.py        read / grep / list, with path containment
```

`reviewer/` must never import from `bot/` or from `legacy/`. The dependency
points one way, which is what lets both consumers exist.

## Why keep it running rather than delete it

Two reasons, and the second is the real one.

1. It is a working reference for the parts the bot has not built yet —
   `legacy/reviewer_cli/github/publish.py` already posts a pending review with
   inline comments over MCP, which is exactly what the bot's next step needs.

2. **It is a second consumer of the engine.** As long as the CLI runs with no
   database, no web server and no GitHub App, the engine cannot quietly grow a
   dependency on any of them. Delete this and that invariant is enforced by
   nothing.

If it ever becomes a burden, delete it then — the history has it.

## What is in here

```
legacy/reviewer_cli/
├── cli.py                  typer commands: review, post, pr
├── orchestrator.py         the CLI's run_review, sources and sessions
├── report.py               terminal rendering
├── session.py              JSON session files under .reviewer/
├── github/publish.py       pending review over MCP — the human gate
├── sources/                ReviewSource: staged diff, or a GitHub PR
├── utils/git_utils.py      staged diff, remote parsing
└── mcp/
    ├── github_client.py    read/write split across two MCP connections
    ├── fs_client.py        repo tools over the local MCP server
    ├── fs_server_app.py    that server
    ├── ratelimit.py        GitHub's points budget
    └── tracing.py          tool wrapper: tracing, arg pinning, repeat guard
```

## One piece worth reading before the bot repeats it

`mcp/github_client.py` opens **two** connections: specialists get
`/readonly` toolset URLs where the server exposes no write tool at all, and only
the publisher gets the writable one. The guarantee that a model driven by an
untrusted diff cannot post was GitHub's, not ours.

The bot now gets the same property differently — specialists hold three local
read functions — but the reasoning is identical and worth not relearning.
