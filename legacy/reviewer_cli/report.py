from __future__ import annotations

from legacy.reviewer_cli.orchestrator import ReviewReport
from reviewer.config import MASTER_DISPATCH_CAP
from reviewer.models.anchor import AnchorState
from reviewer.models.findings import Severity

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


_ANCHOR_LABEL = {
    AnchorState.LINE: "inline",
    AnchorState.FILE: "file-level",
    AnchorState.NONE: "not in diff",
}


def render_report(report: ReviewReport) -> str:
    lines = [report.summary, ""]
    if report.target and report.target.is_postable:
        lines.insert(
            0,
            f"{report.target.owner}/{report.target.repo}#{report.target.number} "
            f"— {report.target.title}",
        )

    if report.master_trace:
        lines.append("Dispatch plan:")
        for entry in report.master_trace:
            scope = ", ".join(entry.files) if entry.files else "full diff"
            marker = " (re-run)" if entry.is_recall else ""
            if entry.error:
                lines.append(f"  ✗ {entry.subagent}{marker} [{scope}] — failed: {entry.error}")
            else:
                lines.append(
                    f"  • {entry.subagent}{marker} [{scope}] — "
                    f"{entry.finding_count} finding(s): {entry.task}"
                )
        lines.append("")

    if report.routing_summary:
        lines.append(report.routing_summary)
        lines.append("")

    if not report.findings:
        lines.append("No findings.")
        return "\n".join(lines)

    for finding in sorted(report.findings, key=lambda f: _SEVERITY_ORDER[f.severity]):
        location = finding.file_path
        if finding.anchor and finding.anchor.line:
            location += f":{finding.anchor.line}"
        lines.append(
            f"[{finding.severity.value.upper()}] {finding.title} "
            f"({location}) — {finding.subagent}"
        )
        lines.append(f"  {finding.message}")
        placement = (
            _ANCHOR_LABEL[finding.anchor.state] if finding.anchor else "unanchored"
        )
        lines.append(
            f"  id: {finding.id}  status: {finding.status.value}  posts as: {placement}"
        )
        lines.append("")

    if report.cap_reached:
        lines.append(
            f"Note: master dispatch cap ({MASTER_DISPATCH_CAP}) was reached; "
            "review may be incomplete."
        )
    if report.aborted:
        lines.append(
            f"WARNING: this review is PARTIAL. The master loop stopped early "
            f"({report.aborted}). The findings above completed normally, but "
            "areas the master never dispatched were not reviewed at all."
        )

    return "\n".join(lines)
