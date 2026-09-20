"""Turn a quoted source line into a real line number, using the checkout.

Specialists were asked to *count* lines through diff hunk headers. That is
arithmetic, and the measurements were bad: on one review a catch block at lines
77-80 was reported as 65-68, and two findings gave 17-18 and 11-12 for the same
pair of constants at 16-17 — contradicting each other inside a single review.

The same review contained an exact cross-file reference,
``ProductService.cs:57-70``, because the specialist had *opened that file*.
Reading gives exact positions; counting does not.

So the schema now asks for the offending line copied verbatim, and this module
finds that string in the checked-out file. Copying is something models do
reliably; searching is something we do exactly.

Three rules, in order:

1. If the quote appears exactly once in the file, that is the line. Done.
2. If it appears several times — a repeated `}` or a common assignment — prefer
   a line the pull request actually touched, then use the model's own
   ``line_range`` as a tie-breaker. Its absolute counting is untrustworthy; as
   a hint about *which* of several identical lines it meant, it is fine.
3. If it appears nowhere, the specialist is wrong about the location. Report
   nothing rather than a guess: a comment on unrelated code is worse than one
   attached to the file, and GitHub will not catch it because it only rejects
   lines outside the diff entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reviewer.models.anchor import FileHunks

log = logging.getLogger("bot.review.locate")

# A quote this short matches everything — a lone brace, `else`, `try`. Resolving
# one tells us nothing, so it is treated as no quote at all.
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

    Leading `+`/`-` because a specialist may copy straight out of the diff;
    whitespace because indentation is the thing most likely to be re-emitted
    slightly differently.
    """
    stripped = text.strip()
    if stripped[:1] in {"+", "-"}:
        stripped = stripped[1:]
    return " ".join(stripped.split())



def _find_block(haystack: list[str], needle: list[str]) -> int | None:
    """Line number where ``needle`` appears as consecutive lines, or None.

    Returns the FIRST line of the match: a comment on a multi-line statement
    belongs at its start, which is where a reader looks.
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
        # A statement split across lines — a chained SQL concatenation, a
        # multi-line method signature. Asked for "the line this is about", a
        # specialist reasonably quotes the whole thing, and matching it against
        # single lines finds nothing. Anchor such a quote at its first line.
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
        # Worth distinguishing in the log: a quote that is nowhere in the file
        # usually means the specialist named the wrong file, which is a
        # different problem from a quote that is merely ambiguous.
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
        # The model's counting is unreliable in absolute terms but fine as a
        # hint about which of several identical lines it meant.
        nearest = min(candidates, key=lambda n: abs(n - hint[0]))
        return Located(nearest, f"{len(matches)} matches, nearest to the reported line")

    return Located(candidates[0], f"{len(matches)} matches, took the first")


def resolve_line_ranges(
    findings: list[Any], repo_root: Path, hunks_by_path: dict[str, FileHunks]
) -> dict[str, int]:
    """Replace each finding's ``line_range`` with one derived from its quote.

    Mutates in place and returns a tally of outcomes for logging. The model's
    own ``line_range`` survives only where no quote could be resolved — it is a
    fallback, not the mechanism.
    """
    tally = {"quoted": 0, "kept_model_line": 0, "no_line": 0}

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
            if previous is not None and previous != found.line:
                # The measurement that justifies this module. Logged every time
                # so the size of the correction stays visible.
                log.info(
                    "located %s:%s by quote (%s) — the specialist said %s",
                    finding.file_path, found.line, found.reason, previous,
                )
        elif finding.line_range:
            tally["kept_model_line"] += 1
            log.info(
                "%s: %s, keeping the reported line %s",
                finding.file_path, found.reason, finding.line_range[0],
            )
        else:
            tally["no_line"] += 1

    return tally
