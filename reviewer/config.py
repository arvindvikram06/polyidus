import os

# One switch, because these four settings move together — pointing a Claude
# model at the GLM gateway just 404s.
#
#   REVIEWER_LLM=glm      (default) the Presidio gateway, bedrock/zai.glm-5
#   REVIEWER_LLM=claude             a local CLIProxyAPI, Claude Sonnet
#
# Both speak the OpenAI chat-completions shape, so nothing above this line
# changes when you switch — only the base URL, key and model names.
LLM_PROFILE = os.environ.get("REVIEWER_LLM", "").strip().lower() or "glm"

_PROFILES: dict[str, dict[str, str]] = {
    "glm": {
        "base_url": "https://llm-proxy.innovation.studio.presidio.ai",
        "master": "bedrock/zai.glm-5",
        "subagent": "bedrock/zai.glm-5",
        "key_env": "REVIEWER_API_KEY",
    },
    # CLIProxyAPI (github.com/router-for-me/CLIProxyAPI) run locally, signed
    # into a Claude Pro subscription and exposing an OpenAI-compatible endpoint.
    "claude": {
        "base_url": "http://127.0.0.1:8317/v1",
        # Real ids from `GET /v1/models`. The "-latest" aliases in CLIProxyAPI's
        # config examples are not in the list it serves, so they 404.
        "master": "claude-sonnet-5",
        "subagent": "claude-sonnet-5",
        "key_env": "REVIEWER_CLAUDE_PROXY_KEY",
    },
}

_DEFAULT_PROFILE = "glm"
_profile = _PROFILES.get(LLM_PROFILE) or _PROFILES[_DEFAULT_PROFILE]


def _setting(name: str, fallback: str) -> str:
    """Resolve one setting for the active profile.

    A profile-scoped variable wins (REVIEWER_CLAUDE_MASTER_MODEL). The
    unprefixed ones configure the default backend only — `.env` pins them to the
    default gateway, so leaking them would ask one gateway for another's model.
    """
    scoped = os.environ.get(f"REVIEWER_{LLM_PROFILE.upper()}_{name}")
    if scoped:
        return scoped
    if LLM_PROFILE == _DEFAULT_PROFILE:
        return os.environ.get(f"REVIEWER_{name}", fallback)
    return fallback


DEFAULT_MASTER_MODEL = _setting("MASTER_MODEL", _profile["master"])
DEFAULT_SUBAGENT_MODEL = _setting("SUBAGENT_MODEL", _profile["subagent"])
DEFAULT_BASE_URL = _setting("BASE_URL", _profile["base_url"])
DEFAULT_API_KEY = os.environ.get(_profile["key_env"]) or (
    os.environ.get("REVIEWER_API_KEY") if LLM_PROFILE == _DEFAULT_PROFILE else None
)


def llm_summary() -> str:
    """One line naming the backend in use, for a startup log."""
    return f"{LLM_PROFILE}: {DEFAULT_MASTER_MODEL} via {DEFAULT_BASE_URL}"

# How many `dispatch_specialists` batches the master may issue. One batch can
# hold many tasks, so this is a re-planning budget, not a specialist budget.
MASTER_DISPATCH_CAP = int(os.environ.get("REVIEWER_MASTER_DISPATCH_CAP", "3"))

# Tool calls (grep/read_file/list_dir) a single specialist may make.
SUBAGENT_TOOL_ITERATION_CAP = int(os.environ.get("REVIEWER_SUBAGENT_TOOL_ITERATION_CAP", "70"))

# Tasks accepted in one batch, and how many of them run concurrently.
MAX_TASKS_PER_BATCH = int(os.environ.get("REVIEWER_MAX_TASKS_PER_BATCH", "50"))
MAX_FANOUT = int(os.environ.get("REVIEWER_MAX_FANOUT", "4"))

MAX_DIFF_INPUT_TOKENS = int(os.environ.get("REVIEWER_MAX_DIFF_INPUT_TOKENS", "200000"))

# Post the line number the model counted instead of searching the checkout for
# the line it quoted, with no verification. Off by default; the comparison arm.
#   REVIEWER_TRUST_MODEL_LINE_NUMBERS=1
TRUST_MODEL_LINE_NUMBERS = os.environ.get(
    "REVIEWER_TRUST_MODEL_LINE_NUMBERS", ""
).strip().lower() in {"1", "true", "yes"}
