from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reviewer.orchestrator import ReviewReport


def _session_dir(repo_root: Path) -> Path:
    directory = repo_root / ".reviewer"
    directory.mkdir(exist_ok=True)
    return directory


def save_session(report: "ReviewReport", repo_root: Path) -> Path:
    path = _session_dir(repo_root) / f"session-{report.report_id}.json"
    path.write_text(report.model_dump_json(indent=2))
    return path
    