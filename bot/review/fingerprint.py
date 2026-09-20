"""A stable identity for "this problem, in this file".

Needed now because `findings` has a NOT NULL fingerprint and a unique
constraint on `(owner, repo, pr_number, fingerprint)`. Step 7 uses it for the
real dedupe across runs; even in step 5 it already prevents two specialists
who found the same thing from creating two rows.

The whole point is that the fingerprint survives things that change without the
problem changing:

* the model rewording its message between runs
* the line moving because code above it was edited
* the severity being reassessed

So identifiers and numbers are stripped out and what remains is the set of
content words plus the file it is about.

Measured behaviour — these all produce the SAME fingerprint:

    Hardcoded credential `sk-123` found at line 41
    Hardcoded credential `sk-999` found at line 87     (literal and line moved)
    found hardcoded credential at line 41 `sk-123`     (reordered)
    HARDCODED CREDENTIAL found at LINE 41              (case)

And these produce DIFFERENT ones:

    Hardcoded credential at line 41                    (a content word dropped)
    Hardcoded credential found at line 41 in source    (a content word added)
    A secret is embedded directly in the source        (fully reworded)

**Known limitation.** This matches on an exact set of content words, so it
absorbs mechanical variation (line numbers, identifiers, ordering, case) but
not semantic rewording. A model that says the same thing with one word
different produces a different fingerprint, and step 7's dedupe would then
re-post a finding it has already made — the expensive direction of the two
errors.

An exact hash cannot fix that, because the ledger's uniqueness is enforced by a
database constraint and a constraint needs an exact key. Step 7 should keep
this hash as the fast path and add a similarity fallback: compare the
normalised word sets of a new finding against the still-open findings for the
same file, and treat a high overlap as the same problem. That check belongs in
application code, not in the schema.
"""

from __future__ import annotations

import hashlib
import re

# Anything that varies run-to-run without the underlying problem changing.
_NUMBERS = re.compile(r"\d+")
_BACKTICKED = re.compile(r"`[^`]*`")
_QUOTED = re.compile(r"[\"'][^\"']*[\"']")
_NON_WORD = re.compile(r"[^a-z\s]")
_SPACES = re.compile(r"\s+")

# Words that carry no distinguishing signal. Dropping them means a reworded
# message with the same substance still matches.
_STOPWORDS = frozenset(
    [
        "a", "an", "the", "this", "that", "these", "those",
        "is", "are", "was", "were", "be", "been", "being",
        "it", "its", "of", "to", "in", "on", "at", "for", "from",
        "with", "without", "by", "and", "or", "but", "if", "then",
        "should", "could", "would", "may", "might", "must", "can",
        "will", "shall",
        # Positional words specifically: a message that says "on line 41" and
        # the same message saying "at line 87" must hash identically.
        "line", "lines", "code", "which", "where", "when", "what", "who",
    ]
)


def normalise(message: str) -> str:
    """Reduce a finding's message to its stable core."""
    text = message.lower()
    # Quoted and backticked spans are usually identifiers or literals, which
    # are exactly what a reworded message changes.
    text = _BACKTICKED.sub(" ", text)
    text = _QUOTED.sub(" ", text)
    text = _NUMBERS.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    words = [w for w in _SPACES.split(text) if w and w not in _STOPWORDS]
    # Sorted, so a sentence reordered between runs still matches. Deduplicated,
    # so emphasis by repetition does not change the hash.
    return " ".join(sorted(set(words)))


def fingerprint(file_path: str, message: str, *, title: str = "") -> str:
    """A short, stable hash identifying this problem in this file.

    Truncated to 16 hex characters: enough that a collision across one pull
    request is not a practical concern, short enough to sit readably inside an
    HTML comment marker in a review body.
    """
    basis = f"{file_path}\n{normalise(title + ' ' + message)}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]
