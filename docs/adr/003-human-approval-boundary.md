# ADR-003: The human approval boundary

Status: accepted (rewritten after the Phase 5 review)

## Decision

Nothing reaches a payer unless a named person approved that exact appeal, and nothing is
filed twice.

- **Drafting is cheap and automatic.** The LangGraph appeal graph drafts the letter,
  checks policy (deadline, minimum amount, review status, unverified terms) and pauses at
  an interrupt. Its checkpoints make that pause durable across restarts.
- **Deciding is one guarded row update.** `decide_appeal` moves the appeal from
  `awaiting_approval` to `approved` or `rejected` with
  `UPDATE ... WHERE status = 'awaiting_approval'`. Exactly one caller wins. A second
  click, a late rejection, or a replay finds the row already moved and gets 409.
- **Sending is not a graph node.** `submit` runs only after that transition, uses the
  appeal id as the idempotency key, and moves `submitting -> submitted` or
  `submit_failed`. A failed send can be retried (safely, because of the key) or
  withdrawn, which frees the findings.
- **Policy is checked again at approval**, because deadlines pass while a draft waits.
- **The person comes from the credential.** API keys are bound to a person; keys without
  one (services, the MCP server) can read and draft but get 403 on any decision.

## Why not let the graph send

The first version did. The Phase 5 review showed that resuming a graph that had already
passed its interrupt runs the pending submit regardless of the new decision: a rejection
could end in a submission. Checkpoints are a good place for "where was I"; they are the
wrong place for "who said yes". The row is the record.
