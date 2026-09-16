"""Sandboxed, read-only access to a repository.

Pure filesystem logic with no framework imports: path containment, the
deny-list, symlink handling and output caps live here, and ``server.py`` is the
only thing that wraps them for a protocol. Keeping this module free of MCP and
LangChain is what lets the whole security model be read in one file.
"""

from __future__ import annotations

import re
from pathlib import Path

# Never readable through these tools, even inside the repository. Specialists
# are driven by an LLM whose input is an untrusted diff, so a prompt-injected
# instruction to read a credential must fail at the tool boundary.
_DENIED_PARTS = frozenset(
    {".git", ".env", ".venv", "venv", "node_modules", "__pycache__", ".ssh", ".aws", ".reviewer", ".xml", "pom.xml"}
)

_MAX_GREP_MATCHES = 50
_MAX_READ_LINES = 2000


class ToolError(Exception):
    pass


def _is_denied(relative: Path) -> bool:
    return any(part in _DENIED_PARTS for part in relative.parts)


def _resolve_within(repo_root: Path, path: str) -> Path:
    """Resolve ``path`` against the repo, rejecting anything that escapes it."""
    root = Path(repo_root).resolve()
    target = (root / path).resolve()
    if target != root and not target.is_relative_to(root):
        raise ToolError(f"path escapes the repository root: {path}")
    relative = target.relative_to(root) if target != root else Path()
    if _is_denied(relative):
        raise ToolError(f"path is not readable by review tools: {path}")
    return target


def grep(repo_root: Path, pattern: str, path_glob: str = "**/*", max_matches: int = _MAX_GREP_MATCHES) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc

    root = Path(repo_root).resolve()
    try:
        candidates = root.glob(path_glob)
    except (NotImplementedError, ValueError) as exc:
        raise ToolError(f"invalid path_glob: {exc}") from exc

    matches: list[str] = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue 
        relative = resolved.relative_to(root)
        if _is_denied(relative):
            continue
        try:
            text = resolved.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{relative}:{lineno}: {line.strip()}")
                if len(matches) >= max_matches:
                    return "\n".join(matches)
    return "\n".join(matches) if matches else "no matches"


def read_file(repo_root: Path, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    target = _resolve_within(repo_root, path)
    if not target.is_file():
        raise ToolError(f"no such file: {path}")

    lines = target.read_text(errors="ignore").splitlines()
    if start_line is not None or end_line is not None:
        start = max((start_line or 1) - 1, 0)
        end = end_line if end_line is not None else len(lines)
        lines = lines[start:end]

    truncated = len(lines) > _MAX_READ_LINES
    body = "\n".join(lines[:_MAX_READ_LINES])
    if truncated:
        body += f"\n... truncated at {_MAX_READ_LINES} lines; request a narrower range."
    return body


def list_dir(repo_root: Path, path: str = ".") -> str:
    target = _resolve_within(repo_root, path)
    if not target.is_dir():
        raise ToolError(f"no such directory: {path}")
    entries = sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in target.iterdir()
        if entry.name not in _DENIED_PARTS
    )
    return "\n".join(entries) if entries else "empty directory"
