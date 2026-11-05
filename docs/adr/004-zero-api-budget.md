# ADR-004: Build, test and evaluate with no model API spend

Status: accepted

## Context

The project had no budget for model API calls. It still had to show the parts that
matter in a production AI system: extraction with provenance, evals that gate changes,
guardrails, human review, and an agent surface.

## Decision

- The default extractor is deterministic rules (`recoup.ingest.extractors.rules`), and
  everything downstream of it treats it as untrusted, exactly as it would a model.
- A Claude extractor is implemented against the Anthropic SDK
  (`messages.parse` with a Pydantic output schema, adaptive thinking) and switches on
  only with `RECOUP_EXTRACTOR=claude` plus a key. Its tests use a fake client. Any model
  failure gives an empty extraction and a warning, so it fails closed.
- The eval suite is extractor-agnostic. CI scores the rules extractor on every PR; the
  `eval-claude` label scores the model on the same ground truth when a key exists, behind
  an approval environment.
- The agent surface is an MCP server. Claude Desktop or Claude Code can drive it on the
  user's own subscription, so tool-use works with no API spend in this repo.
- A held-out wording pack measures the cost of not using a model: rules read 14% of
  holdout terms and every holdout contract fails closed.

## Consequences

The holdout gap is the honest argument for paying for a model here, with a number on
it, and the harness to prove a model closes it is already in place.
