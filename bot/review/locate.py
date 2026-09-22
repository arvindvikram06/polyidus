"""Turn a quoted source line into a real line number, using the checkout.

Counting lines through hunk headers is arithmetic, and the measurements were
bad: a catch block at 77-80 reported as 65-68, and two findings giving 17-18 and
11-12 for the same constants at 16-17. The same review cited
``ProductService.cs:57-70`` exactly — because the specialist had *opened* it.

So the schema asks for the offending line copied verbatim and this module finds
that string. Copying is reliable; searching is exact.

Three rules, in order:

1. One match in the file — that is the line.
2. Several matches (a repeated `}`, a common assignment) — prefer a line the PR
   touched, then use the model's ``line_range`` as a tie-breaker. Untrustworthy
   as an absolute, fine as a hint about *which* identical line it meant.
3. No match — the specialist is wrong about the location. Report nothing: a
   comment on unrelated code is worse than one attached to the file, and GitHub
   only rejects lines outside the diff entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewer.models.anchor import FileHunks

log = logging.getLogger("bot.review.locate")

# A lone brace, `else`, `try` — matches everything, so treated as no quote.
_MIN_QUOTE_LENGTH = 8


@dataclass
class Located:
    line: int | None
    reason: str

    @property
    def found(self) -> bool:
        return self.line is not None


def _normalise(text: str) -> str:
    """Compare on content, not formatting.

    Leading `+`/`-` because a specialist may copy from the diff; whitespace
    because indentation is most likely to be re-emitted differently.
    """
    stripped = text.strip()
    if stripped[:1] in {"+", "-"}:
        stripped = stripped[1:]
    return " ".join(stripped.split())



def _find_block(haystack: list[str], needle: list[str]) -> int | None:
    """Line number where ``needle`` appears as consecutive lines, or None.

    Returns the FIRST line: a comment on a multi-line statement belongs at its
    start, where a reader looks.
    """
    if not needle or len(needle) > len(haystack):
        return None
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return start + 1  # 1-indexed
    return None


def locate(
    repo_root: Path,
    file_path: str,
    quote: str | None,
    hunks: FileHunks | None = None,
    hint: tuple[int, int] | None = None,
) -> Located:
    """Find ``quote`` in ``file_path`` and return its line number."""
    if not quote or len(_normalise(quote)) < _MIN_QUOTE_LENGTH:
        return Located(None, "no usable quote")

    path = Path(repo_root) / file_path
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError as exc:
        return Located(None, f"could not read {file_path}: {type(exc).__name__}")

    normalised = [_normalise(line) for line in lines]
    wanted = [n for n in (_normalise(p) for p in quote.splitlines()) if n]
    if not wanted:
        return Located(None, "no usable quote")

    if len(wanted) > 1:
        # A statement split across lines. Asked for "the line this is about" a
        # specialist reasonably quotes the whole thing, which matches no single
        # line — so anchor it at its first.
        span = _find_block(normalised, wanted)
        if span is not None:
            return Located(span, f"{len(wanted)}-line block matched")
        # The block may have been quoted with a line dropped or reflowed. Fall
        # back to whichever of its lines identifies a place on its own.
        for part in wanted:
            if len(part) >= _MIN_QUOTE_LENGTH:
                hits = [i for i, line in enumerate(normalised, start=1) if line == part]
                if len(hits) == 1:
                    return Located(hits[0], "one line of a multi-line quote matched")
        return Located(None, "multi-line quote not found in the file")

    target = wanted[0]
    matches = [i for i, line in enumerate(normalised, start=1) if line == target]

    if not matches:
        # A quote nowhere in the file usually means the wrong file was named —
        # a different problem from an ambiguous one.
        return Located(None, "quote not found in the file")

    if len(matches) == 1:
        return Located(matches[0], "unique match")

    # Several identical lines. Prefer one the pull request touched.
    if hunks:
        added = [n for n in matches if n in hunks.added]
        if len(added) == 1:
            return Located(added[0], f"{len(matches)} matches, one of them added")
        candidates = added or [n for n in matches if n in hunks.right] or matches
    else:
        candidates = matches

    if hint:
        # Unreliable as an absolute, fine as a hint about which identical line.
        nearest = min(candidates, key=lambda n: abs(n - hint[0]))
        return Located(nearest, f"{len(matches)} matches, nearest to the reported line")

    return Located(candidates[0], f"{len(matches)} matches, took the first")


def _has_content(repo_root: Path, file_path: str, line: int) -> bool:
    """Is there actually code on that line?

    A finding is never *about* a blank line, so a number landing on one is off.
    Measured: "SaveChangesAsync is inside a loop" placed on line 74, which is
    empty; the call is on 75.
    """
    try:
        lines = (Path(repo_root) / file_path).read_text(errors="ignore").splitlines()
    except OSError:
        return False
    return 1 <= line <= len(lines) and bool(lines[line - 1].strip())


def resolve_line_ranges(
    findings: list[Any], repo_root: Path, hunks_by_path: dict[str, FileHunks]
) -> dict[str, int]:
    """Replace each finding's ``line_range`` with one derived from its quote.

    Mutates in place and returns a tally for logging. The model's own
    ``line_range`` survives only where no quote resolved — a fallback, not the
    mechanism.
    """
    # `agreed`/`corrected` measure how often the counted line matches where the
    # quote actually sits — the whole case for searching ourselves.
    tally = {"quoted": 0, "kept_model_line": 0, "no_line": 0, "agreed": 0, "corrected": 0}

    for finding in findings:
        found = locate(
            repo_root,
            finding.file_path,
            getattr(finding, "offending_line", None),
            hunks_by_path.get(finding.file_path),
            finding.line_range,
        )
        if found.found:
            previous = finding.line_range[0] if finding.line_range else None
            finding.line_range = (found.line, found.line)
            tally["quoted"] += 1
            if previous is None:
                pass
            elif previous == found.line:
                tally["agreed"] += 1
            else:
                tally["corrected"] += 1
                # Logged every time, so the size of the correction stays visible.
                log.info(
                    "located %s:%s by quote (%s) — the specialist counted %s, off by %+d",
                    finding.file_path, found.line, found.reason,
                    previous, previous - found.line,
                )
        elif finding.line_range and _has_content(repo_root, finding.file_path,
                                                 finding.line_range[0]):
            # Unresolvable quote, usually a `catch` or a brace. The reported
            # number carries it: `read_file` prints margins, so it was read.
            tally["kept_model_line"] += 1
            log.info(
                "%s: %s, using the reported line %s",
                finding.file_path, found.reason, finding.line_range[0],
            )
        else:
            # A number we just refused must not reach `anchor_findings`.
            finding.line_range = None
            tally["no_line"] += 1
            # One run reported `no_line: 12` without saying why. The causes need
            # different fixes: a missing quote is a prompt or schema problem, a
            # non-matching one is the model copying the code wrong.
            quote = getattr(finding, "offending_line", None)
            log.warning(
                "%s: no line for %r — %s%s",
                finding.file_path,
                finding.title,
                found.reason,
                f" (quoted {quote!r})" if quote else " (the specialist quoted nothing)",
            )

    return tally
