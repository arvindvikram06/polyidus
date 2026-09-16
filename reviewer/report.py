from __future__ import annotations

from reviewer.config import MASTER_DISPATCH_CAP
from reviewer.findings import Severity
from reviewer.orchestrator import ReviewReport

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


def render_report(report: ReviewReport) -> str:
    lines = [report.summary, ""]

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
        lines.append(
            f"[{finding.severity.value.upper()}] {finding.title} "
            f"({finding.file_path}) — {finding.subagent}"
        )
        lines.append(f"  {finding.message}")
        lines.append(f"  id: {finding.id}  status: {finding.status.value}")
        lines.append("")

    if report.cap_reached:
        lines.append(
            f"Note: master dispatch cap ({MASTER_DISPATCH_CAP}) was reached; "
            "review may be incomplete."
        )

    return "\n".join(lines)
