# ADR-002: Rate engine order of operations

Status: accepted (revised after the Phase 1 review)

## Decision

For each claim, per line:

1. Base rate per unit: a carve-out if the code has one, else the contract fee schedule
   row, else the contract's percentage of the stated Medicare year's fee. No rate means
   the line is unpriced, never guessed.
2. Modifier multipliers, each distinct modifier once.
3. Multiple procedure reduction, per date of service. Every unit of every eligible line
   is a slot. Slots rank by per-unit amount; the first pays 100%, the rest pay the
   contract's secondary share. Carve-outs marked exempt are left out.
4. Units, for lines the reduction did not already account for.
5. Lesser of billed charge, if the contract says so.
6. Round to cents, half up, once, at the end.

## Why this order

It is the order the CMS physician fee schedule applies these adjustments in, and the
order most commercial contracts inherit when they say "percent of Medicare". The first
version ranked by line total and never reduced extra units of a single procedure; the
Phase 1 review showed that overstates expected payment for multi-unit procedures, so
ranking moved to per-unit slots.

## Guard rails

- A line the engine cannot price inside a reduction group puts every other line in that
  group into review, because the ranking may be wrong.
- A modifier applied to a carve-out rate is flagged: contracts rarely say whether a flat
  rate is adjusted.
- Property tests: amounts are never negative, always whole cents, never above billed
  under lesser-of, and the last trace step always equals the result.
