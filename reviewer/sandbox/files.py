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


def _ripgrep_excludes() -> list[str]:
    """Deny-list entries as ripgrep glob exclusions.

    Both forms are needed: `!.git` excludes a file or directory with that name,
    `!**/.git/**` excludes everything beneath one.
    """
    globs: list[str] = []
    for part in sorted(_DENIED_PARTS):
        globs += ["--glob", f"!{part}", "--glob", f"!**/{part}/**"]
    return globs


def _grep_ripgrep(
    root: Path, pattern: str, path_glob: str, max_matches: int
) -> list[str] | None:
    """Search with ripgrep, or return None if it is unavailable.

    Returns None rather than raising so the caller can fall back to the pure
    Python walk — `rg` is not present in a slim container unless installed, and
    a missing binary must not break reviews.

    Notes on the flags, since several are load-bearing:

    * `--no-config` — a `~/.ripgreprc` on the host could otherwise change what
      a review can see. Results must depend only on the repository.
    * `--regexp` — the pattern comes from a model. Passed positionally, one
      beginning with `-` would be parsed as a flag; behind `--regexp` it cannot
      be.
    * no `--follow` — ripgrep does not follow symlinks by default, which is
      what keeps a link pointing outside the checkout from being read.
    * `--hidden` with explicit excludes — dotfiles matter to a reviewer
      (`.github/workflows`), so they are searched, and the deny-list is applied
      on top rather than relying on them being hidden.
    """
    import shutil
    import subprocess

    if shutil.which("rg") is None:
        return None

    args = [
        "rg",
        "--no-config",
        "--line-number",
        "--no-heading",
        "--with-filename",
        "--color=never",
        "--hidden",
        # Per file. The total is capped by the caller after reading.
        "--max-count",
        str(max_matches),
        *_ripgrep_excludes(),
    ]
    if path_glob and path_glob != "**/*":
        args += ["--glob", path_glob]
    args += ["--regexp", pattern, "--", "."]

    try:
        completed = subprocess.run(
            args, cwd=root, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode == 1:  # ran fine, found nothing
        return []
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        # Rust's regex engine rejects some patterns Python accepts, such as
        # lookarounds. Surface that as the same error the caller expects.
        if "regex parse error" in stderr.lower() or "unrecognized" in stderr.lower():
            raise ToolError(f"invalid regex: {stderr.splitlines()[-1][:160]}")
        return None  # anything else: fall back rather than fail the review

    matches: list[str] = []
    for line in completed.stdout.splitlines():
        # `path:line:text` -> the caller's `path:line: text`
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno, text = parts
        matches.append(f"{path.removeprefix('./')}:{lineno}: {text.strip()}")
        if len(matches) >= max_matches:
            break
    return matches


def grep(repo_root: Path, pattern: str, path_glob: str = "**/*", max_matches: int = _MAX_GREP_MATCHES) -> str:
    """Search the repository, preferring ripgrep and falling back to Python.

    ripgrep is many times faster on a large checkout, which matters because a
    specialist's time is part of its budget. The pure Python walk below stays
    as the fallback so a container without `rg` still works.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc

    root = Path(repo_root).resolve()

    fast = _grep_ripgrep(root, pattern, path_glob, max_matches)
    if fast is not None:
        return "\n".join(fast) if fast else "no matches"
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
    first = 1
    if start_line is not None or end_line is not None:
        start = max((start_line or 1) - 1, 0)
        end = end_line if end_line is not None else len(lines)
        lines = lines[start:end]
        first = start + 1

    truncated = len(lines) > _MAX_READ_LINES
    shown = lines[:_MAX_READ_LINES]

    # Every line carries its real number. Without this a specialist that has
    # the file open still has to COUNT to report where something is, and
    # counting is what it gets wrong: one review placed a finding on line 74,
    # which is blank, when the offending call was on 75.
    #
    # The number is not decoration. It is the answer to the only question the
    # model cannot work out reliably on its own, and it costs about six
    # characters a line to hand it over.
    body = "\n".join(f"{first + i:>5} | {line}" for i, line in enumerate(shown))
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
