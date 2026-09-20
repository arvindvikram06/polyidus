"""Collapsing a review's findings into what a person should read.

Measured on one real pull request before this existed: 14 findings describing
7 distinct problems, with per-row `SaveChangesAsync` reported four times at
`high`, `medium`, `low` and `low`. Every finding was factually correct — the
review was accurate and unreadable.

The property that matters most here is negative: **a failed adjudication must
never lose a finding.** Showing a duplicate is a nuisance; silently dropping a
SQL injection because a merge plan was malformed is not.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.review import adjudicate as adj
from reviewer.models.findings import Finding, Severity


def make(subagent: str, title: str, severity: Severity, line: int | None = None) -> Finding:
    return Finding(
        subagent=subagent,
        file_path="src/Service.cs",
        line_range=(line, line) if line else None,
        severity=severity,
        title=title,
        message=f"{title} — details.",
        verified_by=f"Read Service.cs:{line or 1}",
        diff_context="",
    )


def plan(**kwargs) -> adj.Adjudication:
    return adj.Adjudication(**kwargs)


def run(findings, plan_or_exc):
    """Adjudicate with a stubbed model, so no network or tokens are involved."""

    class _LLM:
        async def ainvoke(self, _prompt):
            if isinstance(plan_or_exc, Exception):
                raise plan_or_exc
            return plan_or_exc

    class _Model:
        def with_structured_output(self, _schema):
            return _LLM()

    original = adj.get_chat_model
    adj.get_chat_model = lambda *a, **k: _Model()
    try:
        return asyncio.run(adj.adjudicate(findings))
    finally:
        adj.get_chat_model = original


# --- the merge works --------------------------------------------------------


def test_duplicates_collapse_to_one_finding_at_one_severity():
    """Four reports of one defect at three severities is what this fixes."""
    findings = [
        make("architecture", "Per-row SaveChanges", Severity.HIGH),
        make("coding_standards", "SaveChangesAsync in loop", Severity.MEDIUM, line=75),
        make("coding_standards", "Missing transaction boundary", Severity.LOW),
    ]
    result = run(
        findings,
        plan(
            groups=[
                adj.Group(
                    primary=1,
                    duplicates=[0, 2],
                    severity=Severity.MEDIUM,
                    rationale="one defect; cost is developer time",
                )
            ],
            summary="Bulk import commits per row.",
        ),
    )

    assert result.ran
    assert len(result.findings) == 1
    assert result.findings[0].severity is Severity.MEDIUM
    assert result.summary == "Bulk import commits per row."


def test_independent_agreement_survives_the_merge():
    """Three specialists finding the same defect is stronger evidence than one.

    The duplicates are removed; the fact that they agreed is not.
    """
    findings = [
        make("security", "Hardcoded credentials", Severity.CRITICAL, line=16),
        make("architecture", "Secrets in source", Severity.CRITICAL),
        make("coding_standards", "Credentials committed", Severity.CRITICAL),
    ]
    result = run(
        findings,
        plan(
            groups=[
                adj.Group(primary=0, duplicates=[1, 2], severity=Severity.CRITICAL, rationale="same")
            ],
            summary="s",
        ),
    )

    kept = result.findings[0]
    assert result.agreed_by[kept.id] == ["architecture", "coding_standards"]


def test_a_dropped_finding_is_returned_with_its_reason():
    """Dropping is a decision someone may disagree with, so it must be visible."""
    findings = [make("a", "Real", Severity.HIGH), make("b", "Unsupported", Severity.HIGH)]
    result = run(
        findings,
        plan(
            groups=[adj.Group(primary=0, severity=Severity.HIGH, rationale="r")],
            dropped=[adj.Dropped(index=1, reason="verified_by does not support it")],
            summary="s",
        ),
    )

    assert [f.title for f in result.findings] == ["Real"]
    assert result.dropped[0][0].title == "Unsupported"
    assert "does not support" in result.dropped[0][1]


# --- the safety property ----------------------------------------------------


@pytest.mark.parametrize(
    "bad, why",
    [
        (
            plan(groups=[adj.Group(primary=0, severity=Severity.HIGH, rationale="r")], summary="s"),
            "finding 1 is unaccounted for",
        ),
        (
            plan(
                groups=[adj.Group(primary=0, duplicates=[0, 1], severity=Severity.HIGH, rationale="r")],
                summary="s",
            ),
            "index 0 used twice",
        ),
        (
            plan(
                groups=[adj.Group(primary=0, duplicates=[1, 9], severity=Severity.HIGH, rationale="r")],
                summary="s",
            ),
            "index 9 does not exist",
        ),
    ],
)
def test_a_malformed_plan_never_loses_a_finding(bad, why):
    """All-or-nothing on purpose.

    A partially applied plan could silently discard a critical finding. No
    output is worth that, so an invalid plan means the originals pass through.
    """
    findings = [make("a", "SQL injection", Severity.CRITICAL), make("b", "Other", Severity.LOW)]

    result = run(findings, bad)

    assert not result.ran, why
    assert len(result.findings) == 2
    assert {f.title for f in result.findings} == {"SQL injection", "Other"}


def test_a_model_failure_passes_findings_through_untouched():
    findings = [make("a", "SQL injection", Severity.CRITICAL), make("b", "Other", Severity.LOW)]

    result = run(findings, RuntimeError("proxy unreachable"))

    assert not result.ran
    assert len(result.findings) == 2
    assert result.findings[0].severity is Severity.CRITICAL


def test_one_finding_skips_adjudication_entirely():
    """Nothing to merge, so do not spend a model call on it."""
    findings = [make("a", "Only", Severity.HIGH)]
    result = run(findings, RuntimeError("must not be called"))

    assert not result.ran
    assert len(result.findings) == 1



def test_a_specialist_is_not_credited_with_agreeing_with_itself():
    """One specialist reporting a defect twice is not independent agreement.

    Observed on a real review: `coding_standards · also found by architecture,
    coding_standards`, which reads as a bug to anyone looking at the comment.
    """
    findings = [
        make("coding_standards", "Swallowed exception", Severity.MEDIUM, line=79),
        make("coding_standards", "No error context", Severity.MEDIUM),
        make("architecture", "Failures not recorded", Severity.HIGH),
    ]
    result = run(
        findings,
        plan(
            groups=[
                adj.Group(primary=0, duplicates=[1, 2], severity=Severity.MEDIUM, rationale="r")
            ],
            summary="s",
        ),
    )

    kept = result.findings[0]
    assert result.agreed_by[kept.id] == ["architecture"]


def test_agreement_is_omitted_entirely_when_only_one_specialist_found_it():
    findings = [
        make("security", "SQL injection", Severity.CRITICAL, line=57),
        make("security", "Raw SQL concat", Severity.CRITICAL),
    ]
    result = run(
        findings,
        plan(
            groups=[adj.Group(primary=0, duplicates=[1], severity=Severity.CRITICAL, rationale="r")],
            summary="s",
        ),
    )

    assert result.agreed_by == {}

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
