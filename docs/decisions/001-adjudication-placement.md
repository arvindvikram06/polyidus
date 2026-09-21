# 001 — Where adjudication runs: the master loop, or a separate call?

**Status:** open. Currently a separate call. Not settled.
**Date:** 2026-09-21
**Decide by:** the first review of a genuinely large pull request, or when the
token gate is revisited — whichever comes first.

---

## The question

After specialists return, something has to group duplicates, score each group
once against the rubric, drop unsupported findings, and write the summary.

Today that is a separate LLM call: `bot/review/adjudicate.py`.

The alternative is to hand the findings back to the master — which is already an
agent loop, already holds the diff, and already receives findings between
batches — and let it decide.

---

## Why this came up

Not theory. It followed a real failure.

Cross-run dedupe was originally a content hash (`fingerprint.py`): normalise the
message, hash it, enforce uniqueness with a database constraint. It **collided**
on two hardcoded credentials in one file — both messages reduced to
`credential found hardcoded`, so `ON CONFLICT DO NOTHING` silently dropped the
second. That is the dangerous direction: a real finding nobody is ever shown.

The hash was removed. "Is this the same defect?" is a semantic question, and the
adjudicator already answers exactly that question within a run. Which
immediately raised: if a model is answering it anyway, why not the master?

---

## What the master sees today

Only a routing summary, not findings:

```python
# reviewer/core/master.py
f"{f.severity.value}: {f.title} ({f.file_path})"
```

Severity, title, file. No message, no `verified_by`, no evidence. Enough to
decide "do I need another batch?", nowhere near enough to judge whether two
findings are the same defect. **Merging is therefore not free** — it means
feeding the master every finding in full.

---

## The measurements

Taken on `arvindvikram06/test-proj#1` (7 files, 180 added lines).

```
WHAT THE MASTER CARRIES
  system prompt + catalog            889 tokens
  the diff (test-proj#1)           2,076
  batch summaries, tool calls       ~600
  --------------------------------------
  total on THIS pull request       3,565      <- enormous headroom

  but MAX_DIFF_INPUT_TOKENS permits    200,000
  so on a large PR the master holds    201,489

WHAT A SEPARATE JUDGE CARRIES — independent of diff size
  prompt + rubric                 ~1,400
  14 findings, full text          ~3,000
  their evidence                  ~1,500
  --------------------------------------
  total, and it does not grow     ~5,900
```

---

## The case for merging into the master

1. **Context is free — on normal pull requests.** 3,565 tokens used. Adding
   findings and evidence costs roughly 4,500 more and nobody notices.
2. **One fewer LLM call.** Though the saving is small: one call against the ~13
   the specialists already make.
3. **The master has the diff.** For "are these the same defect?", seeing the
   code is genuinely useful. The separate judge only sees finding text.
4. **It has the dispatch history** — it knows which specialist was asked what,
   and why.
5. **Fewer moving parts.** One agent instead of an agent plus a pass.

## The case for keeping it separate

1. **The master's context scales with the diff; the judge's does not.**
   `MAX_DIFF_INPUT_TOKENS = 200000` explicitly permits a diff that fills the
   whole window. On that pull request the master has *zero* headroom, and
   adding findings overflows — or truncates, which is worse, because a
   truncated context does not error. It quietly judges fewer findings.
   The system would work until someone opens a big PR, then fail invisibly.
2. **Testability.** `tests/test_adjudicate.py` stubs the model and proves *a
   malformed plan never loses a finding* — five tests on that one property.
   Reaching that path inside an agent loop means driving the whole loop.
3. **Failure isolation.** Today a failed adjudication passes findings through
   untouched. Inside the master, a failure loses the run.
4. **The invariant gets blurry.** "The master plans, it never authors a
   finding" is easy to hold when planning and judging are different calls. In
   one conversation already discussing the diff *and* the findings, the line
   between selecting and rewriting is one token away.
5. **Different shapes.** The master is an agentic loop with a tool and a
   dispatch budget. Adjudication is a pure function: findings in, decision out.

---

## The honest summary

**Context is the weakest argument on the "separate" side.** It only bites at
scale, and it is fixable — see the option below. Points 2 through 4 are the ones
worth defending, and they hold regardless of window size.

**"Context is free" is true today and false on exactly the large pull requests
where a review matters most.** Both halves of that sentence are true.

---

## The option that would settle it

**Lower `MAX_DIFF_INPUT_TOKENS` from 200,000 to roughly 60,000.**

200k is the model's entire window; allowing a diff to fill it leaves nothing for
anything else. A tighter gate would mean the master *reliably* has room, which
removes objection 1 entirely and makes merging sound.

It is a one-line change in `reviewer/config.py`, and arguably right on its own
merits: a 200k-token diff is not reviewable in a single pass anyway.

If that gate comes down, revisit this decision. Objections 2–4 would still
apply, but they are about engineering discipline rather than correctness, and
are a reasonable thing to trade for one fewer moving part.

---

## A related point, independent of the outcome

**Whatever does the judging should see the evidence.** It is stored and no model
has ever read it:

```python
# bot/review/adjudicate.py — what the judge is told
f"      tool calls made by this specialist: {len(f.evidence or [])}"
```

A *count*. Meanwhile the row holds which files were opened and what they
returned — about 653 bytes per finding, measured. That is exactly what is needed
to judge whether a finding's evidence supports it, and it would have caught the
`verified_by` overclaim found earlier, where a specialist wrote "confirmed …
Product entity" about a file it never opened.

Showing the judge the evidence is worth doing **either way**, and is not blocked
by this decision.

---

## And one for later

The suggestion that prompted this — *store findings and let the master read them
from storage* — is the right shape **once findings outgrow a prompt**. The
storage already exists.

The reason not to do it yet:

> **A prompt guarantees the model saw everything. A tool does not.**

With 6–14 findings in a prompt, the model cannot skip one. With
`list_findings()` / `get_finding(n)` it might read four and decide. For a
judgement whose failure mode is silently dropping a real finding, that is the
wrong way to be wrong — the same asymmetry that killed the hash.

At 50+ findings the prompt stops fitting (evidence alone would be ~30KB) and
tool access becomes necessary. Revisit then.
