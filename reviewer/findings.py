from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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
    severity: Severity
    title: str
    message: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    diff_context: str
    suggested_patch: str | None = None
    status: FindingStatus = FindingStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_used: str | None = None


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
