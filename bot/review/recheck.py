"""Answer a human who disagreed with a finding.

The bot published a finding as an inline comment; someone replied saying it is
wrong. This decides whether to stand by it or withdraw it — by reading the code
again, not by re-reading its own sentence.

Two rules shape the prompt:

**The human is usually right, but not automatically.** They know the codebase
and the intent; the specialist saw a diff. So their reason is treated as
evidence, not as an instruction. Conceding on request would make the loop
worthless — every finding would fall to the first objection, including the
correct ones.

**Concede plainly.** A conceded finding that hedges ("you may be right, but
consider…") leaves the thread open and the reader unsure. If the finding is
withdrawn, say so in a sentence and let the thread be resolved.

The re-check gets the same tools the specialists had, pointed at a fresh
checkout, because the whole reason the original finding may be wrong is that
the specialist did not open enough of the repository.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from reviewer.agents.llm import get_chat_model
from reviewer.config import DEFAULT_SUBAGENT_MODEL

log = logging.getLogger("bot.review.recheck")

# A dispute is one question about one finding. It needs far less room than a
# review: enough calls to open the file and follow a symbol or two.
_TOOL_CALL_CAP = 12


class Verdict(BaseModel):
    outcome: Literal["hold", "concede"] = Field(
        description=(
            "`concede` if the objection is right and the finding should be "
            "withdrawn. `hold` if the finding still stands. Judge the code, "
            "not the confidence of the objection."
        )
    )
    reasoning: str = Field(
        description=(
            "Two or three sentences for the reply. Name the file and line you "
            "re-read. If conceding, say plainly that the finding is withdrawn "
            "and why it was wrong. If holding, address their specific point — "
            "never restate the original finding."
        )
    )


_PROMPT = """You published this review comment on a pull request:

FILE: {path}
LINE: {line}

{finding}

A human replied, disagreeing:

{objection}

Re-check it. You have a full checkout of the repository at this commit:

  `list_directory(path)` — what is in a directory. Start at `.`
  `search_code(pattern)` — regex across the repository, returns file:line
  `read_file(path)`      — one file, with its real line numbers in the margin

Open the file. Follow whatever the objection points at — a caller, a base
class, a config value, a framework default. The original finding was made from
a diff; the objection usually comes from context the diff did not show, so the
question is almost always whether that context exists in the repository.

Decide:

- The objection names something real that makes the finding wrong  -> concede.
- The objection is a preference, a misunderstanding, or names something you
  checked and did not find                                          -> hold.

Being disagreed with is not evidence. Conceding because someone pushed back
makes every finding worthless, including the true ones. Equally, holding to
protect a bad finding wastes their time. Read the code and say what it shows.
"""


async def recheck(
    *,
    repo_root: Path,
    tools: list[BaseTool],
    file_path: str,
    line: int | None,
    finding_body: str,
    objection: str,
    model: str = DEFAULT_SUBAGENT_MODEL,
) -> Verdict:
    """Re-examine one disputed finding. Never raises; holds on failure."""
    prompt = _PROMPT.format(
        path=file_path,
        line=line if line is not None else "(attached to the file)",
        finding=finding_body.strip(),
        objection=objection.strip(),
    )

    try:
        agent = create_agent(
            model=get_chat_model(model),
            tools=tools,
            response_format=Verdict,
            middleware=[ToolCallLimitMiddleware(run_limit=_TOOL_CALL_CAP, exit_behavior="end")],
        )
        result = await agent.ainvoke({"messages": [HumanMessage(prompt)]})
        verdict = result.get("structured_response")
    except Exception:
        # Full traceback, not just the message. This swallowed an AttributeError
        # once — a wrong argument type in this very call — and the one-line
        # warning made it look like a model timeout, so the real bug shipped.
        log.exception("re-check failed; holding the finding")
        verdict = None

    if not isinstance(verdict, Verdict):
        # Holding is the safe default. A failed re-check that conceded would
        # silently withdraw a finding nobody re-examined — the same failure
        # mode as dropping one, reached by a different route.
        return Verdict(
            outcome="hold",
            reasoning=(
                "I could not complete a re-check just now, so the finding "
                "stands for the moment. Reply again and I will retry."
            ),
        )
    return verdict
