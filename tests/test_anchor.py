from __future__ import annotations

import pytest

from reviewer.models.anchor import AnchorState, anchor_finding, parse_hunks

# Verified against a real `git diff`: the two added lines are 5 and 6 in the
# post-image file, and `session = create(user)` moves from old 5 to new 7.
DIFF = """diff --git a/auth.py b/auth.py
index 6129216..657a63d 100644
--- a/auth.py
+++ b/auth.py
@@ -2,5 +2,7 @@ def login(request):
     user = get_user(request)
     if not user:
         return None
+    token = request.args["t"]
+    query = "SELECT * FROM s WHERE t=" + token
     session = create(user)
     return session
"""


def test_added_lines_get_their_post_image_numbers():
    hunks = parse_hunks(DIFF)
    assert hunks["auth.py"].added == {5, 6}


def test_context_lines_are_addressable_on_both_sides():
    right = parse_hunks(DIFF)["auth.py"].right
    left = parse_hunks(DIFF)["auth.py"].left
    # `session = create(user)` is old line 5, new line 7 — the shift additions cause.
    assert 7 in right and 5 in left
    assert {2, 3, 4} <= right & left, "leading context is on both sides"


def test_deletions_advance_only_the_old_counter():
    diff = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -10,4 +10,3 @@
 keep_one
-dropped
 keep_two
 keep_three
"""
    hunks = parse_hunks(diff)["m.py"]
    # The header promises 4 old lines and 3 new ones; the counters must agree.
    assert hunks.left == {10, 11, 12, 13}, "-10,4 covers old 10-13"
    assert hunks.right == {10, 11, 12}, "+10,3 covers new 10-12"
    assert hunks.added == set(), "a pure deletion introduces nothing"


def test_a_finding_on_an_added_line_anchors_inline():
    anchor = anchor_finding(parse_hunks(DIFF), "auth.py", (6, 6))
    assert anchor.state is AnchorState.LINE
    assert (anchor.line, anchor.side) == (6, "RIGHT")
    assert anchor.start_line is None


def test_a_multiline_finding_carries_start_line():
    anchor = anchor_finding(parse_hunks(DIFF), "auth.py", (5, 6))
    assert (anchor.start_line, anchor.start_side) == (5, "RIGHT")
    assert (anchor.line, anchor.side) == (6, "RIGHT")


def test_a_line_outside_every_hunk_falls_back_to_file_level():
    """The 422 case: the file changed, but line 40 is not in the diff."""
    anchor = anchor_finding(parse_hunks(DIFF), "auth.py", (40, 40))
    assert anchor.state is AnchorState.FILE
    assert anchor.line is None


def test_a_file_outside_the_diff_is_unanchorable():
    anchor = anchor_finding(parse_hunks(DIFF), "other.py", (1, 1))
    assert anchor.state is AnchorState.NONE


def test_a_finding_with_no_line_range_is_file_level():
    assert anchor_finding(parse_hunks(DIFF), "auth.py", None).state is AnchorState.FILE


def test_single_line_hunk_header_without_counts():
    """`@@ -1 +1 @@` is legal and means a count of 1."""
    diff = """diff --git a/x.txt b/x.txt
--- a/x.txt
+++ b/x.txt
@@ -1 +1 @@
-old
+new
"""
    hunks = parse_hunks(diff)["x.txt"]
    assert hunks.added == {1} and hunks.left == {1}


def test_multiple_hunks_in_one_file_each_reset_the_counters():
    diff = """diff --git a/big.py b/big.py
--- a/big.py
+++ b/big.py
@@ -1,2 +1,3 @@
 alpha
+beta
 gamma
@@ -50,2 +51,3 @@
 delta
+epsilon
 zeta
"""
    assert parse_hunks(diff)["big.py"].added == {2, 52}


def test_a_new_file_anchors_from_line_one():
    diff = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+one
+two
+three
"""
    hunks = parse_hunks(diff)["new.py"]
    assert hunks.added == {1, 2, 3}
    assert anchor_finding(parse_hunks(diff), "new.py", (2, 2)).line == 2


def test_comment_payloads_match_the_review_api_shape():
    hunks = parse_hunks(DIFF)
    inline = anchor_finding(hunks, "auth.py", (5, 6)).as_comment("bad")
    assert inline == {
        "path": "auth.py", "body": "bad", "line": 6, "side": "RIGHT",
        "start_line": 5, "start_side": "RIGHT",
    }
    file_level = anchor_finding(hunks, "auth.py", (40, 40)).as_comment("bad")
    assert file_level == {"path": "auth.py", "body": "bad", "subject_type": "file"}
    with pytest.raises(ValueError):
        anchor_finding(hunks, "gone.py", (1, 1)).as_comment("bad")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_a_finding_without_a_line_range_can_only_be_file_level():
    """Six of seven findings in the first real run had line_range=None, so every
    comment landed as "Comment on file" instead of inside the code."""
    from reviewer.models.anchor import AnchorState, anchor_finding, parse_hunks

    hunks = parse_hunks(DIFF)
    assert anchor_finding(hunks, "auth.py", None).state is AnchorState.FILE
    assert anchor_finding(hunks, "auth.py", (6, 6)).state is AnchorState.LINE


def test_the_schema_asks_for_a_quote_rather_than_an_arithmetic_result():
    """Counting lines through diff hunks is arithmetic; quoting one is a copy.

    Measured on a real review: a file the specialist opened produced an exact
    cross-file reference ("ProductService.cs:57-70"), while every line number it
    counted through the diff was wrong — 65-68 for a catch block at 77-80, and
    two findings giving 17-18 and 11-12 for the same constants at 16-17.

    So the schema now asks for the offending source line verbatim, and we
    resolve the number ourselves against the checkout.
    """
    import json

    from reviewer.agents.subagents.base import FindingDraft

    quote = FindingDraft.model_fields["offending_line"].description or ""
    assert "EXACTLY" in quote, "the value of a quote is that it is not paraphrased"
    assert "+" in quote, "diff markers must be stripped or nothing will match"

    fallback = FindingDraft.model_fields["line_range"].description or ""
    assert "fallback" in fallback.lower(), "must not read as the primary mechanism"

    # These descriptions were four times this size. The run after that change
    # lost a whole specialist to unparseable output and dropped from 9 findings
    # to 5 — a large schema is a cost paid on every structured-output call.
    size = len(json.dumps(FindingDraft.model_json_schema()))
    assert size < 2600, f"schema has grown to {size} chars; it degrades output"
