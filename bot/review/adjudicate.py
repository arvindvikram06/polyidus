"""Collapse a review's findings into what a person should actually read.

Specialists work concurrently on overlapping slices and cannot see each
other's output, so the same defect is reported several times. Measured on one
real pull request: **14 findings describing 7 distinct problems**, with
per-row `SaveChangesAsync` reported four times at three different severities
(`high`, `medium`, `low`, `low`).

Every one of those findings was factually correct. That is the point — the
review was accurate and unreadable. A maintainer who opens seven `critical`
items, three of which are the same sentence, stops trusting the tool whatever
the words say.

What this does:

* **groups** findings that describe the same defect
* **re-scores** each group once, against the shared rubric
* **drops** findings the evidence does not support
* **writes** the summary a person reads first

What it deliberately does not do: **rewrite any finding's text**. It chooses a
primary and keeps that specialist's words. The adjudicator has read the
findings, not the code, so any sentence it wrote about the code would be
unverifiable — and a confident, unsourced sentence is exactly the failure this
whole design is built to avoid.

If adjudication fails for any reason the original findings pass through
unchanged. Losing a real finding to a failed merge is far worse than showing a
duplicate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from reviewer.agents.llm import get_chat_model
from reviewer.agents.subagents.rules import shared_rules
from reviewer.config import DEFAULT_MASTER_MODEL
from reviewer.models.findings import Finding, Severity

log = logging.getLogger("bot.review.adjudicate")


# --------------------------------------------------------------- schema ----
class Group(BaseModel):
    primary: int = Field(
        description=(
            "Index of the finding to keep. Prefer one that cites a line number, "
            "then one whose `verified_by` names a file that was actually opened."
        )
    )
    duplicates: list[int] = Field(
        default_factory=list,
        description="Indices of other findings describing this same defect. May be empty.",
    )
    severity: Severity = Field(
        description="One severity for the whole group, decided from the rubric."
    )
    rationale: str = Field(
        description="One sentence: why this severity, and why these are one defect."
    )


class Dropped(BaseModel):
    index: int
    reason: str = Field(description="Why this finding should not be shown.")


class Adjudication(BaseModel):
    groups: list[Group] = Field(default_factory=list)
    dropped: list[Dropped] = Field(default_factory=list)
    summary: str = Field(
        description=(
            "Two to four sentences a reviewer reads before the findings: what this "
            "change does, and what actually matters in it. Name the real risks. Do "
            "not list every finding — they are printed below this."
        )
    )


# --------------------------------------------------------------- result ----
@dataclass
class Adjudicated:
    findings: list[Finding]
    summary: str = ""
    # finding id -> the other specialists that independently reported it.
    # Agreement is signal worth showing, so it survives the merge.
    agreed_by: dict[str, list[str]] = field(default_factory=dict)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    ran: bool = False


_PROMPT = """\
You are the lead reviewer. Several specialists have reviewed one pull request \
concurrently, on overlapping files, unable to see each other's work. Your job \
is to turn their raw output into what a person should actually read.

You have their findings. You have NOT read the code. So you may group, \
re-score, drop and summarise — you must never write a new claim about the \
code, and never change a finding's wording.

Do four things.

1. GROUP. Two findings are the same defect when they describe the same problem \
in the same code, however differently they are worded, and even when they cite \
different line numbers or different files for the same thing. Put them in one \
group. Choose as `primary` the one with a line number, or failing that the one \
whose `verified_by` names a file it actually opened. Most reviews contain \
several such groups: specialists overlap heavily.

2. RE-SCORE. Give each group ONE severity, from the rubric below. When members \
disagree, the rubric decides — not the highest, not the average, and not the \
one the security specialist happened to pick.

3. DROP. Some findings should not be shown at all. Drop a finding when:

   - **it says it did not check.** Text like "I have not inspected", "ensure \
that", "verify whether", "without verifying" means the specialist is asking \
someone else to do the work. That is not a finding.
   - **it concludes there is no problem.** "This compiles cleanly, so no \
issue", "this appears correct" — a finding whose own conclusion is that \
nothing is wrong wastes the reader's attention.
   - **another finding already covers it with real detail.** If one specialist \
says "verify this method is parameterised" and another has already reported \
the actual SQL injection in that method, drop the vague one rather than \
grouping it — it adds nothing.
   - **its `verified_by` does not support what it claims.**
   - **it contradicts another finding** and the evidence cannot settle which \
is right. Watch for two findings citing different line numbers for the same \
code: at most one is right, and if you cannot tell which, neither should be \
shown with a line.
   - **it is malformed** — truncated, or containing text in another language \
or obvious model noise.

   A genuine duplicate is GROUPED, not dropped. Dropping is for findings with \
nothing behind them.

4. SUMMARISE. Write the paragraph a reviewer reads first.

Every index must appear exactly once, in a group as `primary` or in its \
`duplicates`, or in `dropped`. Do not invent indices.

--- THE SEVERITY RUBRIC ---
{rules}

--- THE FINDINGS ---
{findings}
"""


def _render(findings: list[Finding]) -> str:
    lines = []
    for index, f in enumerate(findings):
        where = f.file_path
        if f.line_range:
            where += f":{f.line_range[0]}"
        lines += [
            f"[{index}] severity={f.severity.value} specialist={f.subagent} at {where}",
            f"      title: {f.title}",
            f"      says : {' '.join(f.message.split())[:400]}",
            f"      verified_by: {' '.join((f.verified_by or '').split())[:200]}",
            f"      tool calls made by this specialist: {len(f.evidence or [])}",
            "",
        ]
    return "\n".join(lines)


def _valid(plan: Adjudication, count: int) -> str | None:
    """Reject a plan that would lose or duplicate a finding. None means good."""
    seen: list[int] = []
    for group in plan.groups:
        seen.append(group.primary)
        seen.extend(group.duplicates)
    seen.extend(d.index for d in plan.dropped)

    if any(i < 0 or i >= count for i in seen):
        return f"index out of range: {sorted(set(seen))}"
    duplicated = {i for i in seen if seen.count(i) > 1}
    if duplicated:
        return f"index used more than once: {sorted(duplicated)}"
    missing = set(range(count)) - set(seen)
    if missing:
        return f"findings unaccounted for: {sorted(missing)}"
    return None


async def adjudicate(
    findings: list[Finding], model: str = DEFAULT_MASTER_MODEL
) -> Adjudicated:
    """Group, re-score and summarise. Never loses a finding to a failure."""
    if len(findings) < 2:
        return Adjudicated(findings=findings, ran=False)

    prompt = _PROMPT.format(rules=shared_rules(), findings=_render(findings))

    try:
        llm = get_chat_model(model).with_structured_output(Adjudication)
        plan: Any = await llm.ainvoke(prompt)
    except Exception as exc:  # a failed merge must not cost a real finding
        log.warning("adjudication failed (%s: %s); passing findings through",
                    type(exc).__name__, exc)
        return Adjudicated(findings=findings, ran=False)

    if plan is None:
        log.warning("adjudication returned nothing; passing findings through")
        return Adjudicated(findings=findings, ran=False)

    problem = _valid(plan, len(findings))
    if problem:
        # Deliberately all-or-nothing. A partially applied plan could silently
        # discard a critical finding, and no output is worth that risk.
        log.warning("adjudication plan rejected (%s); passing findings through", problem)
        return Adjudicated(findings=findings, ran=False)

    kept: list[Finding] = []
    agreed: dict[str, list[str]] = {}
    for group in plan.groups:
        primary = findings[group.primary]
        primary.severity = group.severity
        # The primary's own specialist is excluded: one specialist reporting a
        # defect twice is not independent agreement, and crediting it to itself
        # reads as a bug to anyone looking at the comment.
        others = sorted(
            {findings[i].subagent for i in group.duplicates} - {primary.subagent}
        )
        if others:
            agreed[primary.id] = others
        kept.append(primary)

    dropped = [(findings[d.index], d.reason) for d in plan.dropped]

    log.info(
        "adjudicated %d finding(s) -> %d (%d merged, %d dropped)",
        len(findings), len(kept), len(findings) - len(kept) - len(dropped), len(dropped),
    )
    for finding, reason in dropped:
        log.info("  dropped [%s] %s — %s", finding.subagent, finding.title, reason)

    return Adjudicated(
        findings=kept,
        summary=plan.summary.strip(),
        agreed_by=agreed,
        dropped=dropped,
        ran=True,
    )
