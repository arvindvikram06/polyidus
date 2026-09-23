"""The prompts quote things the code decides. This locks the two together.

Every drift this file catches has already happened once:

* `security.md` shipped invalid YAML frontmatter, so the master routed on the
  default "Always use this specialist." instead of its real `when_to_use`.
* Two prompts described tools (`get_file_contents`) that no longer existed.
* The rules named a fixture's symbols, so a review of that fixture was scoring
  recall of its own prompt.
* A tool budget written in prose drifted from the constant that enforced it.

A prompt that quotes a wrong number or a renamed field is not a broken test in
production — it is a silently worse review, which is why these are assertions
rather than documentation.
"""

from __future__ import annotations

import re

import pytest
import yaml

from reviewer.agents.catalog import load_specialists
from reviewer.agents.subagents.base import FindingDraft, _user_message
from reviewer.agents.subagents.rules import compose_system_prompt, shared_rules
from reviewer.models.findings import Severity
from reviewer.sandbox.files import _MAX_GREP_MATCHES, _MAX_READ_LINES

SPECIALISTS = sorted(load_specialists())


def _composed(name: str) -> str:
    return compose_system_prompt(load_specialists()[name].system_prompt, 70)


def test_every_specialist_loads():
    assert SPECIALISTS, "the catalog found no specialists at all"


@pytest.mark.parametrize("name", SPECIALISTS)
def test_frontmatter_is_valid_yaml(name: str):
    """A malformed key silently costs the specialist its routing description."""
    spec = load_specialists()[name]
    raw = spec.prompt_file.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    assert match, f"{spec.prompt_file.name}: no frontmatter block"
    meta = yaml.safe_load(match.group(1))
    assert isinstance(meta, dict), f"{spec.prompt_file.name}: frontmatter is not a mapping"
    for key in ("name", "description", "when_to_use"):
        assert meta.get(key), f"{spec.prompt_file.name}: missing or empty {key!r}"
    # The default would otherwise stand in silently, and it routes very differently.
    assert spec.when_to_use != "Always use this specialist."


@pytest.mark.parametrize("name", SPECIALISTS)
def test_no_unsubstituted_placeholders(name: str):
    """A `{placeholder}` that reached the model is a number it cannot use."""
    leftover = re.findall(r"\{[a-z_]+\}", _composed(name))
    assert not leftover, f"{name}: unsubstituted {leftover}"


@pytest.mark.parametrize("name", SPECIALISTS)
def test_quoted_limits_match_the_code(name: str):
    """The figure the model is told must be the figure enforced."""
    composed = _composed(name)
    assert f"{_MAX_READ_LINES} lines" in composed
    assert f"{_MAX_GREP_MATCHES} matches" in composed
    assert "about 70 tool calls" in composed


def test_every_severity_is_defined_in_the_rubric():
    """A severity the model may emit but the rubric never defines is a guess."""
    rules = shared_rules()
    for severity in Severity:
        assert f"`{severity.value}`" in rules, f"{severity.value} is not in the rubric"


def test_named_schema_fields_exist():
    """The rules instruct on specific fields; a rename must not go unnoticed."""
    rules = shared_rules()
    named = {f for f in FindingDraft.model_fields if f in rules}
    assert {"offending_line", "line_range", "verified_by"} <= named
    for field in re.findall(r"`(file_path|offending_line|line_range|verified_by|"
                            r"suggested_patch|severity|title|message)`", rules):
        assert field in FindingDraft.model_fields, f"rules name {field!r}, schema does not"


@pytest.mark.parametrize("name", SPECIALISTS)
def test_prompts_name_only_real_tools(name: str):
    """Tools deleted with the MCP path must not be described to the model."""
    composed = _composed(name)
    for ghost in ("get_file_contents", "repo:owner/name", "search_code(repo:"):
        assert ghost not in composed, f"{name}: describes a tool that does not exist ({ghost})"


@pytest.mark.parametrize("name", SPECIALISTS)
def test_prompts_are_not_fitted_to_one_fixture(name: str):
    """Naming a fixture's symbols turns evaluation into recall of the prompt."""
    composed = _composed(name)
    for symbol in ("UnitPriceAtPurchase", "OrderItem", "OrderStatus",
                   "SupplierCode", "OrderReturnsService", "DateTime.Now"):
        assert symbol not in composed, f"{name}: names fixture symbol {symbol!r}"


@pytest.mark.parametrize("name", SPECIALISTS)
def test_placement_rule_does_not_contradict_the_schema(name: str):
    """The schema says read the margin; the prompt must not say count hunks."""
    composed = _composed(name).lower()
    assert "derived from the diff's hunk headers" not in composed
    assert "read the number" in composed


def test_the_user_message_agrees_with_the_system_prompt():
    """The other half of what the model reads, which this file used to miss.

    The system prompt was corrected while `base.py` still told the model to
    compute `line_range` "from the hunk headers below" — a contradiction that
    survived precisely because the test only looked at the system prompt.
    """
    message = _user_message("ctx", "a task", ["a.py"], "@@ -1,2 +1,2 @@").lower()
    assert "computed from the hunk headers" not in message
    assert "never work one out from a hunk header" in message
    assert "exactly as it appears in the file" in message


def test_the_user_message_names_only_real_schema_fields():
    message = _user_message("ctx", "a task", ["a.py"], "diff")
    for field in re.findall(r"`([a-z_]+)`", message):
        if field in {"read_file", "search_code", "list_directory"}:
            continue
        assert field in FindingDraft.model_fields, f"user message names {field!r}, schema does not"


def test_the_assignment_is_repeated_after_the_diff():
    """A long diff otherwise pushes the instruction out of reach."""
    message = _user_message("", "REVIEW THE REFUND PATH", [], "x" * 5000)
    assert message.count("REVIEW THE REFUND PATH") == 2
    assert message.index("REVIEW THE REFUND PATH") < message.index("<diff>")
    assert message.rindex("REVIEW THE REFUND PATH") > message.index("</diff>")
