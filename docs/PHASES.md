# Recoup: implementation phases

Recoup finds underpayments in medical billing. It reads payer contracts, extracts the
rates with a citation for every value, checks remittances (X12 835) against those rates,
flags underpayments, and drafts appeals that a human approves before anything is sent.

## Constraint: $0 AI API budget

Nothing in this repo needs a paid model API to build, test, evaluate or demo.

- **Extraction is a provider interface.** The default `rules` provider is deterministic
  and runs offline. A `claude` provider is implemented against the Anthropic SDK and
  switches on only when `RECOUP_EXTRACTOR=claude` and `ANTHROPIC_API_KEY` are set. CI
  never calls it; its unit tests use a fake client.
- **The eval harness does not care which provider runs.** The same gate scores `rules`
  today and would score `claude` the day a key exists, so the comparison is one flag.
- **Appeal letters come from templates** filled in from the rule trace. A model could
  rewrite the wording later, but the numbers always come from code.
- **The agent surface is an MCP server.** Any MCP client (Claude Desktop, Claude Code)
  can drive the tools under the user's own subscription, with no API spend in this repo.
- **The math never needed a model anyway.** Code computes every dollar figure (ADR-001).

## Phases

Each phase ends with: all checks green (`pytest`, `ruff`, `mypy`), an independent code
review of that phase's files, fixes for confirmed findings, and an entry in
`docs/REVIEWS.md`.

| # | Phase | Delivers | Done when |
|---|---|---|---|
| 1 | Core domain | Contract term models, synthetic Medicare fee table, rate engine with a full `rule_trace`, X12 835 parser and writer, CARC codes | Property tests pass (allowed amount never negative, lesser-of never above either side); the 835 round-trips |
| 2 | Synthetic data | Seeded generator for payers, contracts (with amendments) as text and PDF, `clean`/`messy`/`scanned`/`adversarial` variants, claims and 835 remits with planted underpayments, ground-truth JSON. Datasets: `smoke`, `full`, `adversarial`, `holdout` | Same seed gives byte-identical output; every planted error has a label |
| 3 | Ingest and audit | PDF to pages to numbered spans, section segmentation, `rules` extractor with span citations, grounding validator, amendment consistency, confidence routing, `claude` provider (opt-in), LangGraph ingest and audit graphs, findings with explanations | `recoup audit` on the smoke set finds the planted underpayments; every value cites a span that contains it |
| 4 | Evals and CI gate | Metrics (term precision/recall, citation validity, finding precision/recall, dollar accuracy, injection safety), `baseline.json`, `recoup eval --gate`, GitHub Actions workflow that posts a PR comment and fails on regression | A deliberately broken extractor fails the gate locally |
| 5 | Service | SQLAlchemy + Alembic schema (SQLite for dev, Postgres for prod), FastAPI, job queue (`SKIP LOCKED` on Postgres), review queue, appeal graph with a human-approval interrupt, mock clearinghouse / payer portal / PMS (429s, 503s, idempotency keys, HMAC webhooks, OAuth2), resilient client, MCP server | An end-to-end API test goes from upload to an approved, submitted appeal against the mocks |
| 6 | Operator UI | Next.js + TypeScript: dashboard, findings, finding detail with highlighted cited spans and the rule trace, review queue, appeal approval, eval report | `npm run build` passes; the UI works against the local API |
| 7 | Production setup | OpenTelemetry spans per graph node, Dockerfiles, Terraform for ECS Fargate + RDS (no NAT gateway), deploy workflow, README with real eval numbers, ADRs | `terraform validate` passes; the README numbers come from the eval run |

## Out of scope (stated, not hidden)

- Real PHI or real payer contracts. All data is synthetic.
- Real CMS fee schedule values. The fee table is synthetic, shaped like the CMS PFS.
- Image OCR. "Scanned" contracts carry an OCR-noise text layer (character swaps, broken
  lines). A real OCR engine would plug in at the same `pages` interface.

## Status (5 Nov 2026)

All seven phases are built and reviewed. Each review's findings, and what was done about
them, are in `docs/REVIEWS.md`. Not done:

- nothing is deployed: no AWS apply, no image built (no Docker on the build machine);
- nothing is committed, so CI has not run on GitHub yet;
- the Claude extractor has never been called, because there is no API budget.
