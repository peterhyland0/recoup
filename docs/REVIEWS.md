# Phase reviews

Each phase gets an independent review: a separate agent is given only the phase's files
and asked for confirmed defects, each with a failing input. Fixes land before the next
phase closes. Findings that were not fixed are listed with the reason.

## Phase 1: core domain (rate engine, models, 835)

Review: 13 confirmed defects (2 high, 10 medium, plus low items), all reproduced with probe scripts.

| Severity | Finding | Resolution |
|---|---|---|
| High | CLP02 statuses 19, 20, 21, 23 and 25 were unknown, so one forwarded claim rejected the whole file | Added to `ClaimStatus`; test covers status 19 |
| High | `int()` truncated fractional SVC05 units (0.5 became 0, expected $0) | Units are `Decimal` end to end; round-trip and pricing tests |
| Medium | A second ST without SE was silently dropped | Open-set tracking; "transaction set has no SE" error |
| Medium | The writer let delimiters through in free text (`SMITH*JONES`) | The writer rejects any field containing `* ~ : ^` |
| Medium | Line-level DTM*150 (service period) was ignored | Used when there is no DTM*472 |
| Medium | One line outside the contract window aborted the whole claim | That line is unpriced with a reason; the others still price |
| Medium | An unpriced line in a reduction group silently promoted the next line to 100% | The other lines in the group get a `review_reason` |
| Medium | Reduction ranked by line total, and extra units of a lone procedure were never reduced | Every unit is a slot, ranked by per-unit amount, as CMS does (ADR-002 updated in the engine docstring) |
| Medium | Same-date amendments were ordered by string ID (`AMD-10` before `AMD-2`) | Applied in document order |
| Medium | An amendment could not remove a term | `TermsPatch.unset` takes term keys |
| Medium | A file without DTM*405 was rejected | Falls back to BPR16 |
| Medium | Fallback line IDs collided for a reversal and its correction | A repeated CLP01 gets a `#n` stem |
| Low | N104 was stored as the NPI even when N103 was `FI` (tax ID) | Taken only for `XX`, else from REF*HPI |
| Low | SE01 was never checked | Counted and compared |
| Low | A BOM, `NaN`, duplicate modifiers, a control number over 9 digits, string tie-break | All fixed, with tests |
| Low | BPR06-15 not written for ACH; TRN03 not in "1"+EIN form | Not fixed: the parser does not read them, and the synthetic payers have no EIN |

After the fixes: 39 tests (including property tests), ruff and mypy `--strict` clean.

## Phase 2: synthetic data

Review checked labels against a fresh engine run, 835 balancing, citations against the
PDF text, injection and decoy records, and determinism across all four datasets: all held.
Findings: 1 high, 3 medium, 4 low.

| Severity | Finding | Resolution |
|---|---|---|
| High | `build()` never cleared old output: 54 orphan 835s reused line ids with conflicting payments | Output folders are emptied before each build |
| Medium | The holdout pack leaked development wording (negations, `Section N.` headings, decoys) | Pack B has its own headings (`Article N - ...`), negations and decoys |
| Medium | Every fragment of a wrapped clause carries the citation, including fragments with no value | Kept on purpose: truth cites the whole clause. The eval scores citation *precision* (cited spans fall inside the truth clause and contain the value), not recall over fragments |
| Medium | Modifier 50 landed on carve-out lines, where the contract never says whether modifiers apply | The generator no longer does this; the engine flags any modifier on a carve-out for review |
| Low | `denial_valid` forced variance to 0, breaking `variance = expected - payer` | Variance always follows the formula; a new `recoverable` column says what an appeal can claim |
| Low | `mpr_misapplied` was planted on contracts silent about multiple procedures | Every contract now states its multiple-procedure rule, including "will not be reduced" |
| Low | OCR noise can corrupt a cited value (`2061l`) | Kept: this is what scanned contracts look like; the extractor repairs digits and lowers confidence |
| Low | All-denied payments wrote `BPR*I*0` | Zero payments write `BPR*H` (non-payment) |
| Plausible | "usual and customary charge" is not clearly the billed charge | Reworded to "the charge billed on the claim" |

## Phase 3: ingest and audit

The review attacked the safety claims with hand-built contracts and remits. On the
generated datasets everything held. On adversarial inputs it did not, and the findings
below were the most useful of the project. Each one has a regression test in
`tests/test_review_regressions.py`.

| Severity | Finding | Resolution |
|---|---|---|
| High | Routing ignored the contract's own status: 24 findings on the full set from NEEDS_REVIEW contracts went out with no person | Actionable findings from any unverified contract route to review. Findings also cite `medicare_year` and the amendment's effective date, not just the term the trace named |
| High | Only CO-group denials counted; OA-18 and PI-29 zero-pays and a reversal were AUTO "underpaid" | A zero-paid line is a denial in any payer group. Reversals, predeterminations, secondary payments and negative payments are `skipped`, never priced |
| High | Grounding only checked the number was somewhere in the cited text: a two-column fee row gave 99213 the 99214 rate | Values bind to their own context: a fee must follow its code and come before the next code, a modifier's percentage must follow that modifier. Two codes on one fee row are not read |
| High | "Lab at 150%; all else 120%" read 150%; "modifier 25" in a Medicare clause became a modifier term | A clause stating several percentages is ambiguous (confidence 0.5, review). Modifier rules skip clauses about Medicare or multiple procedures |
| High | The Claude path trusted the model's own injection flags; duplicate keys priced one value and cited another | The injection screen runs on every document after any extractor. A key stated twice with different values is dropped entirely |
| High | The injection phrase list was easy to evade ("claims-processing model") | Wider list, whole-word matching on the OCR skeleton (the first attempt matched `llm` inside `limit`). Phrase lists stay evadable, so the real defence is the first row: suspicious documents never produce unreviewed findings |
| High | Amendment parsing: "from 135% to 120%" read 135%; a date and a change in one sentence kept only the date; an exhibit after an amendment was never read | `from X to Y` takes Y; dates no longer end the clause; an exhibit heading resets amendment context; an amendment with nothing readable raises a warning |
| Medium | A wrapped "Modifier 50 ...;" line swallowed the "Modifier 52" line after it | A line opening with "Modifier NN" or "CPT NNNNN" always starts a new clause, and every modifier in a clause is read against its own segment |
| Medium | "true"/"false"/"none" skipped grounding | They need supporting words in the cited text (billed/charge, multiple/procedure/reduction) |
| Medium | A $0.01 rounding difference was an AUTO underpayment | Differences up to the tolerance are correct (the generator's labels use the same rule). The minimum appeal amount is enforced by the appeal policy |
| Low | Claude extractor crashed the run on truncated output or a malformed key | Any model failure gives an empty extraction plus a warning; keys are checked against a pattern; warnings put the contract in review |
| Low | `mpr_exempt` defaulted to false whenever "not subject to" was missing | Emitted only when the clause says either way; otherwise assembly assumes exempt and flags it |
| Plausible | Appeal deadlines were not compared with today | Checked by the appeal policy before anything is sent (Phase 5) |

Cost of the fixes, measured by the eval: review rate on the full set went from 22% to 40%.
That is the price of never sending a finding from an unverified contract.

## Phase 4: evals and CI gate

| Severity | Finding | Resolution |
|---|---|---|
| High | Nothing gated routing: sending every finding AUTO "improved" `review_rate` | New zero-tolerance metric `auto_on_suspect` (unreviewed actionable findings from a contract with injected text). `review_rate` is reported, not gated, since it should move when routing rules change on purpose |
| High | The `bless-baseline` label skipped the gate, safety included, on every push while it stayed on | Blessing still fails on any safety metric, and the bless job removes the label after one use |
| High | Fork PRs checked out the wrong ref and the job ran PR code with write permissions | The gate job is read-only on the merge ref; bless and Claude jobs run only for same-repo PRs, the Claude job behind an approval environment |
| Medium | `line_accuracy` divided by findings, so dropping findings passed; duplicates inflated recall | Scored against truth lines; `duplicate_findings` and `missing_findings` are zero-tolerance metrics |
| Medium | A missing baseline compared the run with itself; a PR could lower its own baseline | A metric with no baseline fails the gate; CI gates against the base branch's `baseline.json` |
| Medium | Holdout gates little beyond safety (most of its metrics are 0) | Accepted and documented: holdout is there to measure the generalisation gap, and `value_recall` shows partial progress |
| Low | `git diff` missed untracked dataset files | `git add -N` before the diff |
| Low | No timely-filing denial sat on the limit, so an off-by-one would pass | The generator now plants denials at exactly the limit and one day past it |
| Low | No amendment uses `unset`; `citation_precision` scores every grounded value | Known gaps, left as they are |

## Phase 5: service, appeals, integrations

Tenant isolation held under attack, on SQLite filters and Postgres row-level security
both. The appeal flow did not: the first two findings broke the central promise.
Regression tests are at the end of `tests/test_service_e2e.py`.

| Severity | Finding | Resolution |
|---|---|---|
| Critical | A rejection could still end in a submission: the status check read the row, but the graph resumed past the interrupt and ran its pending submit | Sending is no longer a graph node. `decide_appeal` moves the row `awaiting_approval -> approved/rejected` in one guarded UPDATE; only the winner proceeds, and the graph only drafts and waits. A rejection racing an approval gets 409 |
| Critical | Concurrent drafts claimed the same finding and filed it 3 times | Findings are claimed with one guarded `UPDATE ... WHERE status IN ('open','approved')`; the row count must match |
| High | `finding_ids=[f, f, f]` tripled the amount | Repeated ids are rejected |
| High | Policy was only checked at draft time; approving 90 days late still sent | Approval re-runs the policy against today; a stale appeal becomes `blocked` and frees its findings |
| High | A failed submit trapped the money as "appealed" with no way out | `POST /appeals/{id}/retry` (same idempotency key, so a timeout that actually landed resolves to the same filing) and `/withdraw` |
| High | The webhook moved an `awaiting_approval` appeal to won, and won/lost could flip; a "nan" timestamp never expired | Only a `submitted` appeal can be decided, once; the event's payer id must match the one on file; non-finite timestamps fail verification |
| High | The same line on a reissued 835 created a second appealable finding | A line already found recoverable is stored as `duplicate`: visible, never counted or appealed twice |
| Medium | `approved_by` came from a free-text header | The person is bound to the API key. Keys without a person (services, the MCP server) can read and draft but get 403 on any decision |
| Medium | A lost checkpoint made an appeal undecidable (500) | Decisions live in the row, so a missing checkpoint only skips closing the graph. Default paths are absolute (`~/.recoup/`) so the API, worker and MCP server share one database |
| Medium | Rejecting a letter wrote the money off for good | Rejecting a letter frees the findings; writing a finding off is a review decision |
| Medium | POSTs without an idempotency key were retried on 5xx; a bad Retry-After crashed the caller | Only idempotent methods or keyed requests are retried (429 always); Retry-After accepts seconds or an HTTP date, anything else falls back to jitter; PMS notes carry a key |
| Low | Bad CSV, a broken PDF, `limit=-1`, a non-object webhook body all returned 500 | 422 for each, with a test |
| Low | A job whose worker died stayed `running` forever | Claiming sets a 15-minute lease; an expired lease is claimed again |
| Low | A tenant or key named `*` would get system access | Tenant ids are validated; binding a key to `*` refuses to start |

## Phase 6: operator UI

Types matched the API on every endpoint; the defects were in state handling and in what
the screens claimed.

| Severity | Finding | Resolution |
|---|---|---|
| High | Changing `?id=` kept the previous record on screen, so "Approve and send" could post for appeal B while showing appeal A | Results are tagged with the path they were fetched for; detail pages are keyed on the id and only act when the record's id matches the URL |
| Medium | Contract lines scrolled to the wrong place (offset counted twice) | Fixed; offsets are relative to the scroll box |
| Medium | "Approve and send" sat above the letter with no confirmation | The decision is below the letter, behind "I checked each amount against the quoted contract lines" |
| Medium | Review buttons could be double-clicked; errors appeared at the top of a long page | Per-card busy state and per-card errors |
| Medium | "Recoverable" mixed money past its deadline and money waiting on review | The API splits it into ready, in review, past deadline, appealed, won, lost |
| Medium | "Draft appeal" was offered on findings policy will always block | Hidden, with the reason shown, until review approves the finding |
| Low | Silent 500-row cap, no `duplicate` filter, no loading state; every error blamed the API being down; the approver name came from the browser; tables overflowed at 375px; missing labels and `aria-current` | All fixed. The approver name comes from `GET /me`, which reports the key's person |

## Phase 7: production setup

The review ran `tofu validate`, the full test suite, the eval gate and a production
`next build`. Docker is not installed on the build machine, so the images were reviewed
by reading, and nothing has been deployed: these fixes are verified as far as
`tofu validate`, the test suite and `next build` go.

| Severity | Finding | Resolution |
|---|---|---|
| Critical | psycopg was a dev dependency and the image installs `--no-dev`: every Postgres container would crash on import | Moved to runtime dependencies; `uv export --no-dev` lists it |
| Critical | `web/public` was empty, so git would drop it and the web image's `COPY` would fail | `.gitkeep`, plus `mkdir -p public` in the build stage |
| High | The mocks ignored the deployed webhook and PMS secrets, so every signature and token check would fail in AWS | `recoup mocks` reads the same env as the API; ECS passes it the same SSM secrets |
| High | The webhook callback missed the `/api` prefix, and the mock reported a 404 as delivered | Callback fixed; delivery requires a 2xx |
| High | An approve-capable key would be baked into public JavaScript (or an empty one, breaking every call) | Deployed builds bake no key; the UI asks each person for theirs and keeps it in sessionStorage. The README says to set a certificate first |
| High | ECS services could race the listener and capacity providers on first apply | `depends_on` both |
| Medium | Output said "secret", workflow read a variable; immutable tags broke re-runs; prod could never be seeded and had no eval report | Output text fixed; the deploy skips images already pushed; the bootstrap task seeds an empty database; the image builds `evals/latest.json` at build time |
| Medium | uv in the image (0.8) was older than the lockfile's writer (0.12) | Pinned to the same version |
| Medium | The $60 budget was below the $73 estimate; the tag filter matched nothing until the tag is activated | Default $90, account-wide, documented |
| Low | README test and metric counts were off; stopped cost left out the ALB's IPs; an existing GitHub OIDC provider would fail the apply; state encryption not set | Counts corrected (124 tests, 17 metrics); stopped cost about $38; `create_github_oidc_provider` toggle; `encrypt = true` in the backend example |
| Found while fixing | mypy failed whenever the optional `claude` extra was not installed, which is how CI installs | mypy override for the optional module; CI type-checks with `--all-extras` |
