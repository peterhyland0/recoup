# ADR-001: Models read, code computes

Status: accepted

## Context

An underpayment finding becomes a letter to a payer asking for money. Every number in
it has to survive a payer's own review, and a wrong number costs credibility on every
later appeal. Language models are good at reading messy contracts and bad at being
exactly right about arithmetic every time.

## Decision

Extraction is the only step any model may do, and its output is data: a term key, a
value, the span ids it came from, and a confidence. A deterministic rate engine
(`recoup.core.engine`) computes every dollar figure from those terms, and writes a trace
of each step with the term it used. Explanations and appeal letters are filled in from
that trace, never written freely.

Between extraction and the engine sits a grounding check (`recoup.ingest.validate`). A
value must be visibly present in the spans it cites, bound to its own context (a fee
after its code, a percentage after its modifier), and must not cite a line flagged as an
injection. Values that fail are dropped, not repaired.

## Consequences

- Any extractor can be swapped in, rules or a model, and the same checks and the same
  eval decide whether it is good enough.
- A wrong extraction shows up as a wrong term with a citation a person can check, not
  as a plausible-looking total.
- The engine has to model the contract mechanics explicitly (carve-outs, modifiers,
  multiple-procedure ranking, lesser-of), which is more code, but it is code with property
  tests.
