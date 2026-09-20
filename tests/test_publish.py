from __future__ import annotations

import asyncio
import json

import pytest

from legacy.reviewer_cli.github.publish import StaleReviewError, build_review, post_pending_review
from legacy.reviewer_cli.mcp.github_client import GitHubMcpError
from legacy.reviewer_cli.orchestrator import ReviewReport, anchor_findings
from legacy.reviewer_cli.session import load_session, save_session
from legacy.reviewer_cli.sources.base import ReviewTarget
from reviewer.models.findings import Finding, FindingStatus, Severity

DIFF = """diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -2,3 +2,5 @@ def login(request):
     user = get_user(request)
+    token = request.args["t"]
+    query = "SELECT * WHERE t=" + token
     return session
"""

TARGET = ReviewTarget(
    kind="pull_request", owner="acme", repo="widgets", number=7,
    title="add login", head_sha="a" * 40, base_ref="main",
)


def _finding(path="auth.py", line=(4, 4), severity=Severity.HIGH, title="SQLi"):
    return Finding(
        subagent="security", file_path=path, line_range=line, severity=severity,
        title=title, message="concatenated into SQL", verified_by="read auth.py",
        diff_context=DIFF,
    )


def _report(findings):
    report = ReviewReport(summary="1 finding", findings=findings, target=TARGET, diff_text=DIFF)
    anchor_findings(report.findings, DIFF)
    return report


class _FakeTool:
    def __init__(self, name, handler):
        self.name = name
        self._handler = handler

    async def ainvoke(self, args):
        return self._handler(args)


def _fake_tools(head_sha=TARGET.head_sha, reject: str | None = None):
    """Stand in for the writable GitHub MCP toolset."""
    calls: dict[str, list] = {"create": [], "comment": []}

    def pull_request_read(args):
        if args.get("method") == "get":
            return json.dumps({"head": {"sha": head_sha}, "title": "add login"})
        return ""

    def review_write(args):
        calls["create"].append(args)
        return json.dumps({"state": "PENDING"})

    def add_comment(args):
        if reject is not None and args.get("path") == reject:
            raise RuntimeError("422 line must be part of the diff")
        calls["comment"].append(args)
        return json.dumps({"ok": True})

    tools = {
        "pull_request_read": _FakeTool("pull_request_read", pull_request_read),
        "pull_request_review_write": _FakeTool("pull_request_review_write", review_write),
        "add_comment_to_pending_review": _FakeTool("add_comment_to_pending_review", add_comment),
    }
    return tools, calls


# --- payload shaping -------------------------------------------------------


def test_anchored_findings_become_inline_comments():
    body, comments, posted = build_review(_report([_finding()]).findings, "summary")
    assert len(comments) == 1
    assert comments[0]["line"] == 4 and comments[0]["side"] == "RIGHT"
    assert "SQLi" in comments[0]["body"]
    assert posted[0].title == "SQLi"


def test_findings_outside_the_diff_go_into_the_review_body():
    """They must not be dropped, and must not be posted at a line that 422s."""
    report = _report([_finding(path="untouched.py")])
    body, comments, posted = build_review(report.findings, "summary")
    assert comments == [] and posted == []
    assert "untouched.py" in body and "could not be attached inline" in body


def test_a_line_not_in_any_hunk_becomes_a_file_level_comment():
    report = _report([_finding(line=(99, 99))])
    _, comments, _ = build_review(report.findings, "summary")
    assert comments[0] == {
        "path": "auth.py",
        "body": comments[0]["body"],
        "subject_type": "file",
    }


def test_severity_floor_drops_findings_and_says_so():
    report = _report([_finding(severity=Severity.INFO), _finding()])
    body, comments, _ = build_review(report.findings, "summary", min_severity=Severity.HIGH)
    assert len(comments) == 1
    assert "1 finding(s) below the severity floor" in body


def test_rejected_findings_are_never_posted():
    findings = [_finding()]
    findings[0].status = FindingStatus.REJECTED
    _, comments, _ = build_review(_report(findings).findings, "summary")
    assert comments == []


def test_suggested_patch_becomes_a_github_suggestion_block():
    finding = _finding()
    finding.suggested_patch = "    query = sql('... WHERE t=?', token)"
    _, comments, _ = build_review(_report([finding]).findings, "summary")
    assert "```suggestion" in comments[0]["body"]


# --- posting ---------------------------------------------------------------


def test_review_is_created_pending_and_never_submitted():
    """`submit_review` is the human's act; we must not call it."""
    tools, calls = _fake_tools()
    asyncio.run(post_pending_review(_report([_finding()]), tools=tools))

    assert len(calls["create"]) == 1
    assert calls["create"][0]["method"] == "create", "create, not submit"
    assert len(calls["comment"]) == 1
    assert calls["comment"][0]["line"] == 4
    assert calls["comment"][0]["side"] == "RIGHT"


def test_subject_type_uses_the_upper_case_enum_the_server_requires():
    """The real schema requires subjectType and its enum is ['FILE', 'LINE'].

    Lower case was silently wrong: every comment would have been rejected.
    """
    tools, calls = _fake_tools()
    report = _report([_finding(), _finding(line=(99, 99), title="off-diff")])
    asyncio.run(post_pending_review(report, tools=tools))

    kinds = {c["path"] + str(c.get("line")): c["subjectType"] for c in calls["comment"]}
    assert set(kinds.values()) <= {"FILE", "LINE"}
    assert "LINE" in kinds.values() and "FILE" in kinds.values()


def test_the_pending_review_carries_the_reviewed_commit():
    tools, calls = _fake_tools()
    asyncio.run(post_pending_review(_report([_finding()]), tools=tools))
    assert calls["create"][0]["commitID"] == TARGET.head_sha


def test_no_event_is_sent_so_the_review_stays_pending():
    """`event` present means GitHub submits it immediately, skipping the human."""
    tools, calls = _fake_tools()
    asyncio.run(post_pending_review(_report([_finding()]), tools=tools))
    assert "event" not in calls["create"][0]


def test_posted_findings_are_marked_and_others_are_not():
    tools, _ = _fake_tools()
    report = _report([_finding(), _finding(path="untouched.py", title="orphan")])
    asyncio.run(post_pending_review(report, tools=tools))
    by_title = {f.title: f.status for f in report.findings}
    assert by_title["SQLi"] is FindingStatus.POSTED
    assert by_title["orphan"] is FindingStatus.PENDING, "body-only findings aren't comments"


def test_one_rejected_comment_does_not_discard_the_review():
    """The pending review already exists; the other comments still belong on it."""
    tools, calls = _fake_tools(reject="auth.py")
    report = _report([_finding(), _finding(line=(5, 5), title="second")])
    result = asyncio.run(post_pending_review(report, tools=tools))

    assert result["comments_rejected"] == 2
    assert all(f.status is FindingStatus.HELD for f in report.findings)
    assert result["rejections"][0][0] == "SQLi"


def test_a_moved_pull_request_refuses_to_post():
    """New commits shift every line the findings point at."""
    tools, _ = _fake_tools(head_sha="b" * 40)
    with pytest.raises(StaleReviewError, match="has moved"):
        asyncio.run(post_pending_review(_report([_finding()]), tools=tools))


def test_stale_posting_is_possible_when_explicitly_allowed():
    tools, calls = _fake_tools(head_sha="b" * 40)
    asyncio.run(post_pending_review(_report([_finding()]), tools=tools, allow_stale=True))
    assert len(calls["create"]) == 1


def test_a_staged_review_cannot_be_posted():
    tools, _ = _fake_tools()
    report = ReviewReport(summary="s", findings=[_finding()], target=ReviewTarget(kind="staged"))
    with pytest.raises(GitHubMcpError, match="no pull request"):
        asyncio.run(post_pending_review(report, tools=tools))


# --- session round trip ----------------------------------------------------


def test_a_session_survives_a_round_trip(tmp_path):
    saved = _report([_finding()])
    save_session(saved, tmp_path, TARGET)
    loaded = load_session(tmp_path, TARGET)

    assert loaded is not None
    assert loaded.findings[0].anchor.line == 4, "anchors must survive for posting"
    assert loaded.target.head_sha == TARGET.head_sha
    assert loaded.diff_text == DIFF


def test_the_diff_is_stored_once_not_per_finding(tmp_path):
    save_session(_report([_finding(), _finding(), _finding()]), tmp_path, TARGET)
    directory = tmp_path / ".reviewer" / "sessions" / TARGET.slug
    assert (directory / "diff.patch").read_text() == DIFF
    assert "diff --git" not in (directory / "review.json").read_text()


def test_sessions_are_keyed_by_pull_request(tmp_path):
    save_session(_report([_finding()]), tmp_path, TARGET)
    other = TARGET.model_copy(update={"number": 8})
    assert load_session(tmp_path, other) is None, "PR 8 must not read PR 7's review"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- MCP result decoding ---------------------------------------------------


def test_content_blocks_are_unwrapped_before_json_decoding():
    """An MCP text block and a decoded JSON array are both lists.

    Mistaking the first for the second turns an empty result into one phantom
    row, and an object into a list whose every field reads as None.
    """
    from legacy.reviewer_cli.sources.github_pr import _as_json

    assert _as_json([{"type": "text", "text": "[]"}]) == []
    assert _as_json([{"type": "text", "text": '{"number": 7}'}]) == {"number": 7}
    # A genuine JSON array must survive untouched.
    assert _as_json([{"number": 7}, {"number": 8}]) == [{"number": 7}, {"number": 8}]


def test_pull_request_metadata_survives_the_block_wrapper():
    """head_sha reaching None would silently disable the stale-review check."""
    from legacy.reviewer_cli.sources.github_pr import _as_json, _dig

    blocks = [{"type": "text", "text": '{"head": {"sha": "abc123"}, "title": "t"}'}]
    meta = _as_json(blocks)
    assert _dig(meta, "head", "sha") == "abc123"
    assert _dig(meta, "title") == "t"


def test_a_refused_write_is_a_failure_not_a_successful_post():
    """The server answers a permissions failure with ordinary result text, not
    an exception. Reporting "6 comments posted" when every request was refused
    is worse than the refusal: it tells the user to go look at nothing."""
    tools, calls = _fake_tools()

    def refuse(args):
        return "Resource not accessible by personal access token"

    tools["pull_request_review_write"] = _FakeTool("pull_request_review_write", refuse)

    with pytest.raises(GitHubMcpError, match="Resource not accessible"):
        asyncio.run(post_pending_review(_report([_finding()]), tools=tools))
    assert calls["comment"] == [], "no comments after a failed review creation"


def test_a_refused_comment_is_recorded_as_rejected_not_posted():
    tools, calls = _fake_tools()

    def refuse(args):
        return "Resource not accessible by personal access token"

    tools["add_comment_to_pending_review"] = _FakeTool("add_comment_to_pending_review", refuse)

    report = _report([_finding()])
    result = asyncio.run(post_pending_review(report, tools=tools))
    assert result["comments_added"] == 0
    assert result["comments_rejected"] == 1
    assert report.findings[0].status is FindingStatus.HELD


def test_a_plain_text_result_from_a_json_tool_raises():
    from legacy.reviewer_cli.mcp.github_client import GitHubMcpError, call_tool_checked

    tools = {"t": _FakeTool("t", lambda a: "failed to do the thing: 403")}
    with pytest.raises(GitHubMcpError, match="failed to do the thing"):
        asyncio.run(call_tool_checked(tools, "t"))
