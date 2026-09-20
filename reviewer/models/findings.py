from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from reviewer.models.anchor import CommentAnchor


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"
    HELD = "held"
    POSTED = "posted"


class EvidenceItem(BaseModel):
    tool: str
    args: dict[str, Any]
    result_excerpt: str


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    subagent: str
    file_path: str
    hunk_header: str | None = None
    line_range: tuple[int, int] | None = None
    # The offending source line, copied verbatim by the specialist. This is
    # what places the comment: quoting is a copy, which models do reliably,
    # where counting lines through diff hunks is arithmetic, which they do
    # not. `line_range` above is kept only as a tie-breaker and a fallback.
    offending_line: str | None = None
    severity: Severity
    title: str
    message: str
    verified_by: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    diff_context: str
    suggested_patch: str | None = None
    # Where this finding can be posted on the PR, resolved after the review.
    anchor: CommentAnchor | None = None
    status: FindingStatus = FindingStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_used: str | None = None
    # True when this row records that a specialist FAILED, rather than something
    # it found. The CLI shows these in its report on purpose — a human reading a
    # terminal should know a specialist died rather than silently seeing fewer
    # findings. A bot must not: posted to a pull request it becomes a comment
    # about a file that has nothing wrong with it. Consumers that publish are
    # expected to filter on this and report the failure in the summary instead.
    is_failure: bool = False


class SpecialistRun(BaseModel):
    """One specialist executed against one slice of the diff.

    The master may run the same specialist several times in a batch with
    different scopes, so a run — not an agent name — is the unit of work.
    """

    agent: str
    task: str
    files: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
