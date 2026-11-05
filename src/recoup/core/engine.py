"""The rate engine: what the contract says a payer should have allowed.

No model ever produces a dollar figure. Each line gets a trace of the steps that built
its amount, and each step names the contract term key it used, so the UI can show the
clause behind every number.

Order of operations (ADR-002):
  1. base rate per unit: carve-out, else fee schedule row, else percent of Medicare
  2. modifier multipliers, each distinct modifier once
  3. multiple procedure reduction, per date of service: every unit of every eligible line
     is a slot; slots rank by per-unit amount, the first pays 100%, the rest pay the
     secondary share (so units 2+ of a lone procedure are reduced too, as CMS does)
  4. units, for lines the reduction did not touch
  5. lesser of billed charge, if the contract has that clause
  6. round to cents, half up

A line the engine cannot price is never guessed. If such a line could have been part of a
reduction group, the other lines in that group are flagged for review, because the
ranking may be wrong.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from recoup.core.fees import FeeTable
from recoup.core.models import Contract, ResolvedTerms
from recoup.core.money import to_money


class PricingLine(Protocol):
    @property
    def line_id(self) -> str: ...
    @property
    def code(self) -> str: ...
    @property
    def modifiers(self) -> tuple[str, ...]: ...
    @property
    def units(self) -> Decimal: ...
    @property
    def billed(self) -> Decimal: ...
    @property
    def service_date(self) -> date: ...


@dataclass(frozen=True)
class TraceStep:
    rule: str
    detail: str
    amount: Decimal
    """Running amount after this step, unrounded until the final step."""
    term_key: str | None = None
    source: str | None = None
    """'base' or the amendment id that set the term."""


@dataclass(frozen=True)
class LineExpectation:
    line_id: str
    code: str
    expected_allowed: Decimal | None
    """None when the contract has no way to price this code."""
    trace: tuple[TraceStep, ...]
    unpriced_reason: str | None = None
    review_reason: str | None = None
    """Set when the amount was computed but a human should confirm it."""

    @property
    def priced(self) -> bool:
        return self.expected_allowed is not None


@dataclass
class _Work:
    index: int
    line: PricingLine
    resolved: ResolvedTerms | None
    amount: Decimal | None = None
    """Per-unit until units are applied, then the line total."""
    units_applied: bool = False
    steps: list[TraceStep] = field(default_factory=list)
    unpriced_reason: str | None = None
    review_reason: str | None = None

    @property
    def terms_source(self) -> dict[str, str]:
        return self.resolved.source if self.resolved else {}

    def step(self, rule: str, detail: str, term_key: str | None = None) -> None:
        assert self.amount is not None
        source = self.terms_source.get(term_key) if term_key else None
        self.steps.append(TraceStep(rule, detail, self.amount, term_key, source))


def price_claim(lines: Sequence[PricingLine], contract: Contract, fees: FeeTable) -> list[LineExpectation]:
    """Price every line of one claim. Lines may span several dates of service."""
    work = []
    for index, line in enumerate(lines):
        resolved = contract.resolve(line.service_date) if contract.covers(line.service_date) else None
        item = _Work(index, line, resolved)
        if resolved is None:
            item.unpriced_reason = f"date of service {line.service_date} is outside {contract.contract_id}"
        work.append(item)
    for item in work:
        if item.resolved is None:
            continue
        _base_rate(item, item.resolved, fees)
        if item.amount is not None:
            _modifiers(item, item.resolved)
    _multiple_procedure_reduction(work, fees)
    for item in work:
        if item.amount is None:
            continue
        if not item.units_applied:
            _units(item)
        _lesser_of(item)
        item.amount = to_money(item.amount)
        item.step("round", "rounded to cents, half up")
    return [
        LineExpectation(
            line_id=item.line.line_id,
            code=item.line.code,
            expected_allowed=item.amount,
            trace=tuple(item.steps),
            unpriced_reason=item.unpriced_reason,
            review_reason=item.review_reason,
        )
        for item in work
    ]


def _base_rate(item: _Work, resolved: ResolvedTerms, fees: FeeTable) -> None:
    terms = resolved.terms
    code = item.line.code
    if code in terms.carve_outs:
        item.amount = terms.carve_outs[code].rate
        item.step("base_rate", f"carve-out flat rate for {code}", f"carve_out.{code}")
    elif code in terms.fee_schedule:
        item.amount = terms.fee_schedule[code]
        item.step("base_rate", f"contract fee schedule rate for {code}", f"fee_schedule.{code}")
    elif terms.pct_medicare is not None and terms.medicare_year is not None:
        medicare = fees.fee(code, terms.medicare_year)
        if medicare is None:
            item.unpriced_reason = f"no {terms.medicare_year} Medicare fee for {code}"
            return
        item.amount = medicare * terms.pct_medicare
        item.step(
            "base_rate",
            f"{terms.pct_medicare * 100:.1f}% of {terms.medicare_year} Medicare fee ${medicare} for {code}",
            "pct_medicare",
        )
    else:
        item.unpriced_reason = f"contract has no rate for {code}"


def _modifiers(item: _Work, resolved: ResolvedTerms) -> None:
    assert item.amount is not None
    for modifier in dict.fromkeys(item.line.modifiers):
        multiplier = resolved.terms.modifiers.get(modifier)
        if multiplier is None or multiplier == 1:
            continue
        item.amount *= multiplier
        item.step("modifier", f"modifier {modifier} pays {multiplier * 100:.1f}%", f"modifier.{modifier}")
        if item.line.code in resolved.terms.carve_outs:
            item.review_reason = (
                f"modifier {modifier} applied to a carve-out rate; confirm the contract adjusts carve-outs"
            )


def _units(item: _Work) -> None:
    assert item.amount is not None
    item.units_applied = True
    if item.line.units != 1:
        item.amount *= item.line.units
        item.step("units", f"{item.line.units} units")


def _mpr_candidate(item: _Work, fees: FeeTable) -> bool:
    if item.resolved is None or item.resolved.terms.mpr_secondary_pct is None:
        return False
    if not fees.mpr_eligible(item.line.code):
        return False
    carve_out = item.resolved.terms.carve_outs.get(item.line.code)
    return carve_out is None or not carve_out.mpr_exempt


def _multiple_procedure_reduction(work: list[_Work], fees: FeeTable) -> None:
    groups: dict[date, list[_Work]] = defaultdict(list)
    blind_spots: dict[date, list[str]] = defaultdict(list)
    for item in work:
        if item.amount is None:
            if fees.mpr_eligible(item.line.code) or fees.row(item.line.code) is None:
                blind_spots[item.line.service_date].append(f"{item.line.line_id} ({item.line.code})")
        elif _mpr_candidate(item, fees):
            groups[item.line.service_date].append(item)

    for day, group in groups.items():
        slots: list[tuple[Decimal, int, _Work, Decimal]] = []
        for item in group:
            assert item.amount is not None
            whole, fraction = divmod(item.line.units, 1)
            quantities = [Decimal(1)] * int(whole) + ([fraction] if fraction else [])
            slots += [(item.amount, item.index, item, qty) for qty in quantities]
        if len(slots) < 2 and not blind_spots.get(day):
            continue
        slots.sort(key=lambda s: (-s[0], s[1]))
        full: dict[int, Decimal] = defaultdict(Decimal)
        reduced: dict[int, Decimal] = defaultdict(Decimal)
        for position, (_, index, _, qty) in enumerate(slots):
            (full if position == 0 else reduced)[index] += qty
        for item in group:
            pct = item.resolved.terms.mpr_secondary_pct if item.resolved else None
            assert item.amount is not None and pct is not None
            at_full, at_reduced = full[item.index], reduced[item.index]
            item.amount = item.amount * at_full + item.amount * at_reduced * pct
            item.units_applied = True
            parts = []
            if at_full:
                parts.append(f"{at_full} unit at 100%")
            if at_reduced:
                parts.append(f"{at_reduced} at {pct * 100:.1f}%")
            item.step("mpr", "multiple procedure ranking: " + ", ".join(parts), "mpr_secondary_pct")
            if blind_spots.get(day):
                item.review_reason = f"could not price {', '.join(blind_spots[day])} on {day}, so the procedure ranking may be wrong"


def _lesser_of(item: _Work) -> None:
    assert item.amount is not None and item.resolved is not None
    if item.resolved.terms.lesser_of_billed and item.line.billed < item.amount:
        item.amount = item.line.billed
        item.step("lesser_of", f"billed charge ${item.line.billed} is lower", "lesser_of_billed")
