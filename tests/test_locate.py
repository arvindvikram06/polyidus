"""Resolving a quoted source line to a real line number.

Why this exists: specialists were asked to count lines through diff hunk
headers, and measured against the real file their answers were wrong by 1 to 14
lines. Two findings in a single review gave 17-18 and 11-12 for the same pair
of constants, which actually sit at 16-17 — so at most one was right and
nothing detected it.

The same review cited `ProductService.cs:57-70` exactly, because the specialist
had opened that file. Reading is exact; counting is not. So we ask for a quote
and do the counting ourselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.review.locate import locate, resolve_line_ranges
from reviewer.models.anchor import FileHunks
from reviewer.models.findings import Finding, Severity

SOURCE = """\
namespace OrderApi.Business.Services;

public class ProductImportService
{
    private const string SupplierApiKey = "acme-supplier-feed-prod";

    public async Task ImportAsync(BulkImportRequest request)
    {
        var errors = new List<string>();

        foreach (var row in request.Rows)
        {
            try
            {
                await _unitOfWork.SaveChangesAsync(cancellationToken);
            }
            catch
            {
                failed++;
            }
        }
    }
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    f = tmp_path / "src" / "Service.cs"
    f.parent.mkdir(parents=True)
    f.write_text(SOURCE)
    return tmp_path


def make(quote: str | None, line_range=None) -> Finding:
    return Finding(
        subagent="security",
        file_path="src/Service.cs",
        offending_line=quote,
        line_range=line_range,
        severity=Severity.HIGH,
        title="t",
        message="m",
        verified_by="v",
        diff_context="",
    )


# --- the core behaviour -----------------------------------------------------


def test_a_quote_beats_the_line_number_the_model_counted(repo: Path):
    """The whole point, in one assertion.

    On a real review the specialist reported line 65 for `failed++;`, which is
    actually at 19 in this fixture and was at 79 in the real file. The quote
    resolves it; the count does not.
    """
    found = locate(repo, "src/Service.cs", "failed++;", hint=(65, 65))

    assert found.line == 19
    assert found.reason == "unique match"


def test_a_diff_marker_on_the_quote_is_stripped(repo: Path):
    """A specialist copying out of the diff brings the leading '+' with it.

    Without stripping it, nothing ever matches and every finding silently falls
    back to file level.
    """
    assert locate(repo, "src/Service.cs", '+    var errors = new List<string>();').line == 9


def test_indentation_differences_do_not_break_the_match(repo: Path):
    """Indentation is the thing most likely to be re-emitted differently."""
    assert locate(repo, "src/Service.cs", "var errors = new List<string>();").line == 9


# --- refusing to guess ------------------------------------------------------


def test_a_quote_that_is_not_in_the_file_yields_no_line(repo: Path):
    """A comment on unrelated code is worse than one attached to the file.

    GitHub will not catch this for us — it only rejects lines outside the diff
    entirely, so a plausible-but-wrong line inside the diff posts silently.
    """
    found = locate(repo, "src/Service.cs", "var total = ComputeSomethingElse();")

    assert not found.found
    assert found.reason == "quote not found in the file"


def test_a_quote_too_short_to_identify_anything_is_ignored(repo: Path):
    """`}` matches eleven lines here. Resolving it tells you nothing."""
    assert not locate(repo, "src/Service.cs", "}").found
    assert locate(repo, "src/Service.cs", "}").reason == "no usable quote"


def test_a_missing_file_is_reported_rather_than_raised(repo: Path):
    found = locate(repo, "src/Gone.cs", "var errors = new List<string>();")
    assert not found.found
    assert "could not read" in found.reason


# --- ambiguity --------------------------------------------------------------


def test_a_repeated_line_prefers_one_the_pull_request_added(tmp_path: Path):
    f = tmp_path / "a.cs"
    f.write_text("x = compute();\ny = 1;\nx = compute();\n")
    hunks = FileHunks(path="a.cs", right={3}, added={3})

    found = locate(tmp_path, "a.cs", "x = compute();", hunks)

    assert found.line == 3
    assert "one of them added" in found.reason


def test_a_repeated_line_falls_back_to_the_models_hint(tmp_path: Path):
    """Its absolute counting is untrustworthy; as a hint about *which* of
    several identical lines it meant, it is fine."""
    f = tmp_path / "a.cs"
    f.write_text("x = compute();\ny = 1;\nx = compute();\n")

    assert locate(tmp_path, "a.cs", "x = compute();", None, (3, 3)).line == 3
    assert locate(tmp_path, "a.cs", "x = compute();", None, (1, 1)).line == 1


# --- the batch pass ---------------------------------------------------------


def test_resolving_a_batch_overwrites_bad_numbers_and_keeps_good_fallbacks(repo: Path):
    findings = [
        make("failed++;", (65, 65)),                      # quote wins over a bad count
        make("var errors = new List<string>();", None),   # quote supplies a missing one
        make("this line is not in the file", (9, 9)),     # unresolvable, use the number
        make(None, None),                                 # nothing to work with
        make("failed++;", (19, 19)),                      # the model read it correctly
    ]

    tally = resolve_line_ranges(findings, repo, {})

    assert findings[0].line_range == (19, 19), "the counted 65 must be overwritten"
    assert findings[1].line_range == (9, 9)
    assert findings[2].line_range == (9, 9), "an unmatched quote falls back to the number"
    assert findings[3].line_range is None
    assert tally == {
        "quoted": 3, "kept_model_line": 1, "no_line": 1,
        # The comparison that decides whether counting can be trusted at all.
        "agreed": 1, "corrected": 1,
    }


def test_a_reported_line_with_nothing_on_it_is_refused(repo: Path):
    """A finding is never *about* a blank line, or one past the end of a file.

    Measured: asked to count, a model put "SaveChangesAsync is inside a loop"
    on line 74 — which is empty; the call is on 75. Falling back to a number
    is only safe if the number lands on actual code.
    """
    blank = make("nowhere in this file", (10, 10))    # line 10 is empty
    past_end = make("nowhere in this file", (400, 400))
    real = make("nowhere in this file", (9, 9))       # line 9 has code

    tally = resolve_line_ranges([blank, past_end, real], repo, {})

    assert blank.line_range is None, "a blank line points at nothing"
    assert past_end.line_range is None, "outside the file is not a location"
    assert real.line_range == (9, 9)
    assert tally["kept_model_line"] == 1
    assert tally["no_line"] == 2


def test_the_tally_separates_a_correct_count_from_a_corrected_one(repo: Path):
    """`agreed` vs `corrected` is the measurement, so it gets its own test.

    Trusting the model's arithmetic — which is what Oswald does — is only safe
    if `corrected` stays at zero. This is how we find out.
    """
    findings = [
        make("failed++;", (19, 19)),   # right
        make("failed++;", (65, 65)),   # wrong by 46
    ]

    tally = resolve_line_ranges(findings, repo, {})

    assert tally["agreed"] == 1
    assert tally["corrected"] == 1
    assert all(f.line_range == (19, 19) for f in findings), "both end up on the real line"


# --- statements split across lines -------------------------------------------

MULTILINE = """\
public class Repo
{
    public async Task Find(string supplierCode)
    {
        var sql = "SELECT p.* FROM Products p "
                + "INNER JOIN product_supplier ps ON ps.ProductId = p.Id "
                + "WHERE ps.SupplierCode = '" + supplierCode + "' "
                + "ORDER BY p.Name";
    }
}
"""


@pytest.fixture
def multiline_repo(tmp_path: Path) -> Path:
    (tmp_path / "Repo.cs").write_text(MULTILINE)
    return tmp_path


def test_a_quoted_multi_line_statement_anchors_at_its_first_line(multiline_repo: Path):
    """The case that left the SQL injection finding with no line at all.

    Asked for "the line this finding is about", a specialist reasonably quotes
    the whole chained concatenation. Matched against single lines that finds
    nothing, so the most important finding in the review fell back to file
    level.
    """
    quote = (
        'var sql = "SELECT p.* FROM Products p "\n'
        '        + "INNER JOIN product_supplier ps ON ps.ProductId = p.Id "\n'
        '        + "WHERE ps.SupplierCode = \'" + supplierCode + "\' "\n'
        '        + "ORDER BY p.Name";'
    )

    found = locate(multiline_repo, "Repo.cs", quote)

    assert found.line == 5, "a comment on a statement belongs at its start"
    assert "block matched" in found.reason


def test_one_line_of_the_statement_still_resolves_precisely(multiline_repo: Path):
    """Quoting only the offending line is better, and must keep working."""
    found = locate(
        multiline_repo, "Repo.cs",
        '+ "WHERE ps.SupplierCode = \'" + supplierCode + "\' "',
    )
    assert found.line == 7


def test_a_reflowed_block_falls_back_to_a_line_that_identifies_itself(multiline_repo: Path):
    """A model may drop or rewrap a line when copying a long statement.

    Rather than giving up, take any single line of the quote that pins down a
    unique location on its own.
    """
    quote = (
        'var sql = "SELECT p.* FROM Products p "\n'
        '        + "ORDER BY p.Name";'          # not consecutive in the file
    )

    found = locate(multiline_repo, "Repo.cs", quote)

    assert found.found
    assert "one line of a multi-line quote" in found.reason

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
