from __future__ import annotations

import uuid
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


class EvidenceItem(BaseModel):
    tool: str
    args: dict[str, Any]
    result_excerpt: str


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    subagent: str
    file_path: str
    line_range: tuple[int, int] | None = None
    # Copied verbatim by the specialist, and what places the comment: quoting is
    # a copy, which models do reliably; counting through hunks is arithmetic,
    # which they do not. `line_range` is a tie-breaker and fallback.
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
    # Which model produced this. Write-only today, and kept deliberately:
    # findings are now compared across backends, and "which model said this"
    # is the first question asked of a run.
    model_used: str | None = None
    # This row records that a specialist FAILED, not something it found. A
    # terminal report should show it — a human must know a specialist died
    # rather than silently see fewer findings. Anything that publishes must
    # filter it out: posted, it comments on a file with nothing wrong.
    is_failure: bool = False


class SpecialistRun(BaseModel):
    """One specialist executed against one slice of the diff.

    The same specialist may run several times in a batch with different scopes,
    so a run — not an agent name — is the unit of work.
    """

    agent: str
    task: str
    files: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
