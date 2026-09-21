# Decisions

Open questions and settled trade-offs, kept so they are not re-argued from
scratch — and so the reasoning survives the conversation it happened in.

One file per decision, numbered. A decision stays here whether it was settled
or not; an open one records what would settle it.

| # | Decision | Status |
|---|---|---|
| [001](001-adjudication-placement.md) | Where adjudication runs: the master loop, or a separate call | **open** — revisit if the diff token gate comes down |

## What belongs here

A decision where **the reasoning is not recoverable from the code**. If someone
can read the implementation and see why, it does not need a file.

These usually qualify:

- two defensible options with a real trade between them
- a choice driven by a measurement, where the measurement would otherwise be lost
- something already tried and abandoned, with the reason

Include the numbers. "The master carries 3,565 tokens on this PR but the gate
permits 200,000" is worth more than "context might be tight".
