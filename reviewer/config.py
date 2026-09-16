import os

DEFAULT_MASTER_MODEL = os.environ.get("REVIEWER_MASTER_MODEL", "bedrock/zai.glm-5")
DEFAULT_SUBAGENT_MODEL = os.environ.get("REVIEWER_SUBAGENT_MODEL", "bedrock/zai.glm-5")
DEFAULT_BASE_URL = os.environ.get("REVIEWER_BASE_URL", "https://llm-proxy.innovation.studio.presidio.ai")
DEFAULT_API_KEY = os.environ.get("REVIEWER_API_KEY")

# How many `dispatch_specialists` batches the master may issue. One batch can
# hold many tasks, so this is a re-planning budget, not a specialist budget.
MASTER_DISPATCH_CAP = int(os.environ.get("REVIEWER_MASTER_DISPATCH_CAP", "3"))

# Tool calls (grep/read_file/list_dir) a single specialist may make.
SUBAGENT_TOOL_ITERATION_CAP = int(os.environ.get("REVIEWER_SUBAGENT_TOOL_ITERATION_CAP", "50"))

# Tasks accepted in one batch, and how many of them run concurrently.
MAX_TASKS_PER_BATCH = int(os.environ.get("REVIEWER_MAX_TASKS_PER_BATCH", "50"))
MAX_FANOUT = int(os.environ.get("REVIEWER_MAX_FANOUT", "4"))

MAX_DIFF_INPUT_TOKENS = int(os.environ.get("REVIEWER_MAX_DIFF_INPUT_TOKENS", "200000"))

# Specialists read the repository through this MCP server; the orchestrator's
# dispatch tool stays native in-process.
FS_MCP_URL = os.environ.get("REVIEWER_FS_MCP_URL", "http://127.0.0.1:8000/mcp")
