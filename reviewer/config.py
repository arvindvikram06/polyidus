import os

DEFAULT_MASTER_MODEL = os.environ.get("REVIEWER_MASTER_MODEL", "bedrock/zai.glm-5")
DEFAULT_SUBAGENT_MODEL = os.environ.get("REVIEWER_SUBAGENT_MODEL", "bedrock/zai.glm-5")
DEFAULT_BASE_URL = os.environ.get("REVIEWER_BASE_URL", "https://llm-proxy.innovation.studio.presidio.ai")
DEFAULT_API_KEY = os.environ.get("REVIEWER_API_KEY")

# How many `dispatch_specialists` batches the master may issue. One batch can
# hold many tasks, so this is a re-planning budget, not a specialist budget.
MASTER_DISPATCH_CAP = int(os.environ.get("REVIEWER_MASTER_DISPATCH_CAP", "3"))

# Tool calls (grep/read_file/list_dir) a single specialist may make.
SUBAGENT_TOOL_ITERATION_CAP = int(os.environ.get("REVIEWER_SUBAGENT_TOOL_ITERATION_CAP", "70"))

# Tasks accepted in one batch, and how many of them run concurrently.
MAX_TASKS_PER_BATCH = int(os.environ.get("REVIEWER_MAX_TASKS_PER_BATCH", "50"))
MAX_FANOUT = int(os.environ.get("REVIEWER_MAX_FANOUT", "4"))

MAX_DIFF_INPUT_TOKENS = int(os.environ.get("REVIEWER_MAX_DIFF_INPUT_TOKENS", "200000"))

# Specialists read the repository through this MCP server; the orchestrator's
# dispatch tool stays native in-process. Used for local (staged) reviews.
FS_MCP_URL = os.environ.get("REVIEWER_FS_MCP_URL", "http://127.0.0.1:8000/mcp")

# GitHub's hosted MCP server. Toolsets are selected by URL path, and the
# `/readonly` suffix is what makes "specialists cannot write" a guarantee
# enforced by the server rather than by our own tool filtering.
GITHUB_MCP_BASE = os.environ.get("REVIEWER_GITHUB_MCP_BASE", "https://api.githubcopilot.com/mcp")
GITHUB_READ_TOOLSETS = tuple(
    os.environ.get(
        "REVIEWER_GITHUB_READ_TOOLSETS", "repos/readonly,pull_requests/readonly"
    ).split(",")
)
GITHUB_WRITE_TOOLSET = os.environ.get("REVIEWER_GITHUB_WRITE_TOOLSET", "pull_requests")

# The readonly toolsets expose ~16 tools; a specialist reviewing a diff needs
# two. The rest — collaborators, branches, tags, releases — cannot answer a
# question about this change, but each one is a plausible-looking detour that
# costs an iteration, and specialists were exhausting their budget on them
# before producing any findings.
# Arguments a tool technically accepts as optional but which are useless to omit.
# `get_file_contents` without `path` returns the repository root listing — a
# *successful* response, so a model that forgot it gets no signal and repeats
# the same call until its budget is gone. Enforcing it turns a silent no-op into
# feedback the model can act on.
GITHUB_REQUIRED_TOOL_ARGS: dict[str, tuple[str, ...]] = {
    "get_file_contents": ("path",),
}

# GitHub writes its tool descriptions for a general-purpose assistant with a
# whole repository to wander. A specialist has one diff, a small iteration
# budget, and no idea what files exist. Measured on a real run with the stock
# descriptions: 17 of 18 `get_file_contents` calls omitted `path`, and all four
# `search_code` calls returned nothing because the repository was not indexed.
# Between them the specialists read one file.
#
# `{owner}` and `{repo}` are filled in per review.
GITHUB_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_file_contents": (
        "Read a file, or list a directory, in {owner}/{repo} at the exact commit "
        "under review. This is your main tool — use it.\n"
        "\n"
        "`path` is REQUIRED and says what to read:\n"
        "  - a file      -> `src/OrderApi.Domain/Entities/Product.cs` returns that "
        "file's contents\n"
        "  - a directory -> `src/OrderApi.Domain/Entities` returns what is in it\n"
        "  - the root    -> `.` returns the top level, if you need to find your way\n"
        "\n"
        "If you do not know a path, list a directory first and then read the file. "
        "Do not guess a path; a wrong one returns an error and costs you a step.\n"
        "\n"
        "`owner`, `repo` and `ref` are replaced with the repository and commit under "
        "review before the call is sent, so whatever you pass for them is ignored "
        "and cannot reach anywhere else.\n"
        "\n"
        "Use this whenever a finding depends on something the diff does not show — "
        "the definition of a type, what a base class does, whether a field exists. "
        "A claim about code you have not opened is unverified, and unverified "
        "findings must be reported at `info` severity."
    ),
    "search_code": (
        "Search code across {owner}/{repo} by keyword. Scope every query with "
        "`repo:{owner}/{repo}`.\n"
        "\n"
        "IMPORTANT: this reads GitHub's code search index, which does not include "
        "recently pushed or small repositories. On those it returns zero results "
        "for symbols that certainly exist, and it will keep doing so however you "
        "rephrase the query. If a search comes back empty once, assume the "
        "repository is not indexed and stop searching.\n"
        "\n"
        "Prefer `get_file_contents`: list a directory to find the file, then read "
        "it. That always works, and this does not."
    ),
}

GITHUB_READ_TOOL_ALLOWLIST = tuple(
    t.strip()
    for t in os.environ.get(
        "REVIEWER_GITHUB_READ_TOOLS", "get_file_contents,search_code"
    ).split(",")
    if t.strip()
)

# GitHub allows 900 REST points per minute (GET 1, write 5). Left a little under
# the ceiling so an unrelated client sharing the token does not tip us over.
GITHUB_MCP_POINTS_PER_MINUTE = int(
    os.environ.get("REVIEWER_GITHUB_POINTS_PER_MINUTE", "800")
)
