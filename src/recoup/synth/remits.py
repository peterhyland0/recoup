"""Claims, payer behaviour and 835 remittances, with an exact label for every line.

Payer mistakes are planted by pricing the claim against a *mutated* copy of the contract
(the terms the payer actually used). Labels are then computed from the resulting numbers,
not from the intent: a planted error that happens not to change a line's amount is
labelled correct.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from recoup.core.engine import LineExpectation, price_claim
from recoup.core.fees import FeeTable
from recoup.core.models import (
    Adjustment,
    AdjustmentGroup,
    Claim,
    ClaimStatus,
    Contract,
    RemitClaim,
    RemitLine,
    Remittance,
    ServiceLine,
    Terms,
    TermsPatch,
)
from recoup.core.money import ZERO, to_money
from recoup.synth.contracts import random_service_date
from recoup.synth.parties import Payer, Practice

TOLERANCE = Decimal("0.01")


class Label(StrEnum):
    CORRECT = "correct"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    DENIAL_INVALID = "denial_invalid"
    DENIAL_VALID = "denial_valid"


class PlantedError(StrEnum):
    NONE = "none"
    STALE_RATE = "stale_rate"
    WRONG_PCT = "wrong_pct"
    FEE_ROW_IGNORED = "fee_row_ignored"
    MPR_MISAPPLIED = "mpr_misapplied"
    MODIFIER_IGNORED = "modifier_ignored"
    INVALID_TIMELY_DENIAL = "invalid_timely_denial"
    VALID_TIMELY_DENIAL = "valid_timely_denial"
    OVERPAID = "overpaid"


ERROR_WEIGHTS = {
    PlantedError.NONE: 50,
    PlantedError.STALE_RATE: 8,
    PlantedError.WRONG_PCT: 8,
    PlantedError.FEE_ROW_IGNORED: 7,
    PlantedError.MPR_MISAPPLIED: 7,
    PlantedError.MODIFIER_IGNORED: 6,
    PlantedError.INVALID_TIMELY_DENIAL: 5,
    PlantedError.VALID_TIMELY_DENIAL: 4,
    PlantedError.OVERPAID: 5,
}

EM_CODES = ["99203", "99204", "99212", "99213", "99213", "99214", "99214", "99215"]
ANCILLARY = ["96372", "93000", "71046", "73030"]


@dataclass(frozen=True)
class LineTruth:
    line_id: str
    claim_id: str
    contract_id: str
    code: str
    service_date: date
    payment_date: date
    label: Label
    planted_error: PlantedError
    expected_allowed: Decimal | None
    payer_allowed: Decimal
    variance: Decimal
    """expected_allowed minus payer_allowed; positive means the payer paid less than the contract."""
    recoverable: Decimal
    """What an appeal can claim back: the shortfall, or the full expected amount for an invalid denial."""


@dataclass
class ClaimBatch:
    claims: list[Claim]
    remits: list[Remittance]
    truths: list[LineTruth]


def flatten(contract: Contract, dos: date, terms: Terms | None = None) -> Contract:
    """The contract as a payer would apply it on one date, optionally with altered terms."""
    resolved = terms or contract.resolve(dos).terms
    return contract.model_copy(update={"terms": resolved, "amendments": ()})


class ClaimFactory:
    def __init__(
        self, rng: random.Random, contract: Contract, payer: Payer, practice: Practice, fees: FeeTable
    ):
        self.rng = rng
        self.contract = contract
        self.payer = payer
        self.practice = practice
        self.fees = fees
        self.procedures = [c for c in fees.codes() if fees.mpr_eligible(c)]

    # claim shape

    def _codes_for(self, error: PlantedError, dos: date) -> list[tuple[str, tuple[str, ...], int]]:
        rng, terms = self.rng, self.contract.resolve(dos).terms
        procedural = (
            error in (PlantedError.MPR_MISAPPLIED, PlantedError.MODIFIER_IGNORED) or rng.random() < 0.55
        )
        lines: list[tuple[str, tuple[str, ...], int]] = []
        if not procedural:
            lines.append((rng.choice(EM_CODES), (), 1))
            if rng.random() < 0.4:
                lines.append((rng.choice(ANCILLARY), (), 1))
        else:
            if rng.random() < 0.6:
                lines.append((rng.choice(["99213", "99214"]), ("25",), 1))
            count = 2 if error is PlantedError.MPR_MISAPPLIED else rng.choice([1, 1, 2, 3])
            pool = list(self.procedures)
            carve = [c for c in terms.carve_outs if c in pool]
            if carve and rng.random() < 0.5:
                lines.append((rng.choice(carve), (), 1))
                count -= 1
            for code in rng.sample([c for c in pool if c not in {x[0] for x in lines}], max(count, 0)):
                units = 2 if code in ("17000", "11102") and rng.random() < 0.4 else 1
                lines.append((code, (), units))
            # Modifier 50 only on non-carve-out procedures: the contracts never say whether a
            # flat carve-out rate is adjusted by modifiers, so the truth would be a guess.
            bilateral = [i for i, x in enumerate(lines) if x[0] in pool and x[0] not in terms.carve_outs]
            if bilateral and (error is PlantedError.MODIFIER_IGNORED or rng.random() < 0.15):
                code, mods, units = lines[bilateral[0]]
                lines[bilateral[0]] = (code, (*mods, "50"), units)
            elif error is PlantedError.MODIFIER_IGNORED:
                code = rng.choice([c for c in pool if c not in terms.carve_outs])
                lines.append((code, ("50",), 1))
        if error is PlantedError.FEE_ROW_IGNORED and terms.fee_schedule:
            lines.append((rng.choice(sorted(terms.fee_schedule)), (), 1))
        return lines

    def _affected_line(self, patch: TermsPatch, dos: date) -> tuple[str, tuple[str, ...], int]:
        """A line whose price the amendment changed, so ignoring it shows up in the money."""
        if patch.fee_schedule:
            return (self.rng.choice(sorted(patch.fee_schedule)), (), 1)
        if patch.modifiers:
            return (self.rng.choice(self.procedures), ("50",), 1)
        terms = self.contract.resolve(dos).terms
        unlisted = [c for c in EM_CODES if c not in terms.fee_schedule] or EM_CODES
        return (self.rng.choice(unlisted), (), 1)

    def _payer_contract(self, error: PlantedError, dos: date, codes: set[str]) -> Contract:
        c = self.contract
        terms = c.resolve(dos).terms
        if error is PlantedError.STALE_RATE:
            return c.model_copy(update={"amendments": ()})
        if error is PlantedError.WRONG_PCT and terms.pct_medicare is not None:
            return flatten(
                c, dos, terms.model_copy(update={"pct_medicare": terms.pct_medicare - Decimal("0.10")})
            )
        if error is PlantedError.FEE_ROW_IGNORED:
            rows = {k: v for k, v in terms.fee_schedule.items() if k not in codes}
            return flatten(c, dos, terms.model_copy(update={"fee_schedule": rows}))
        if error is PlantedError.MPR_MISAPPLIED:
            if terms.mpr_secondary_pct not in (None, Decimal("0.50")):
                return flatten(c, dos, terms.model_copy(update={"mpr_secondary_pct": Decimal("0.50")}))
            carve_outs = {k: v.model_copy(update={"mpr_exempt": False}) for k, v in terms.carve_outs.items()}
            update = {"mpr_secondary_pct": Decimal("0.50"), "carve_outs": carve_outs}
            if terms.mpr_secondary_pct is None:
                return flatten(c, dos, terms.model_copy(update=update))
            return flatten(c, dos, terms.model_copy(update={"carve_outs": carve_outs}))
        if error is PlantedError.MODIFIER_IGNORED:
            return flatten(
                c, dos, terms.model_copy(update={"modifiers": {**terms.modifiers, "50": Decimal(1)}})
            )
        if error is PlantedError.OVERPAID and terms.pct_medicare is not None:
            rows = {k: to_money(v * Decimal("1.08")) for k, v in terms.fee_schedule.items()}
            pct = terms.pct_medicare + Decimal("0.05")
            return flatten(c, dos, terms.model_copy(update={"pct_medicare": pct, "fee_schedule": rows}))
        return c

    def _billed(self, code: str, modifiers: tuple[str, ...], units: int) -> Decimal:
        medicare = self.fees.fee(code, 2026) or Decimal(100)
        multiplier = Decimal("1.5") if "50" in modifiers else Decimal(1)
        return to_money(Decimal(round(medicare * Decimal(self.rng.uniform(2.2, 3.2)))) * multiplier * units)

    # one claim

    def make(self, number: int) -> tuple[Claim, RemitClaim, list[LineTruth], date]:
        rng, contract = self.rng, self.contract
        error = rng.choices(list(ERROR_WEIGHTS), weights=list(ERROR_WEIGHTS.values()))[0]
        dos = random_service_date(rng, contract)
        if error is PlantedError.STALE_RATE and not contract.amendments:
            error = PlantedError.WRONG_PCT
        spec = self._codes_for(error, dos)
        if error is PlantedError.STALE_RATE:
            amendment = rng.choice(contract.amendments)
            dos = max(dos, amendment.effective_date + timedelta(days=rng.randint(0, 20)))
            spec.append(self._affected_line(amendment.patch, dos))
        filing = contract.resolve(dos).terms.timely_filing_days or 180
        if error is PlantedError.VALID_TIMELY_DENIAL:
            # One day past the limit is the case an off-by-one would get wrong, so it is common here.
            submitted = dos + timedelta(days=filing + rng.choice([1, 1, rng.randint(5, 40)]))
        elif error is PlantedError.INVALID_TIMELY_DENIAL:
            submitted = dos + timedelta(
                days=rng.choice([filing, filing - 1, rng.randint(5, max(6, filing - 5))])
            )
        else:
            submitted = dos + timedelta(days=rng.randint(2, min(30, filing - 1)))
        payment = submitted + timedelta(days=rng.randint(10, 25))
        payment += timedelta(days=(4 - payment.weekday()) % 7)  # payers pay on Fridays

        claim_id = f"{contract.contract_id.replace('-', '')}-{number:03d}"
        draft = [
            ServiceLine(line_id=f"{claim_id}-{i}", code=code, modifiers=mods, units=Decimal(units),
                        billed=self._billed(code, mods, units), service_date=dos)
            for i, (code, mods, units) in enumerate(spec, start=1)
        ]  # fmt: skip
        payer_contract = self._payer_contract(error, dos, {code for code, _, _ in spec})
        truth = price_claim(draft, contract, self.fees)
        paid_as = price_claim(draft, payer_contract, self.fees)

        lines: list[ServiceLine] = []
        low_billed = contract.resolve(dos).terms.lesser_of_billed and rng.random() < 0.08
        for line, expected, payer in zip(draft, truth, paid_as, strict=True):
            ceiling = max(expected.expected_allowed or ZERO, payer.expected_allowed or ZERO)
            billed = line.billed if line.billed > ceiling else to_money(ceiling * Decimal("1.6"))
            if low_billed and expected.expected_allowed:
                billed = to_money(expected.expected_allowed * Decimal(rng.uniform(0.7, 0.95)))
            lines.append(line.model_copy(update={"billed": billed}))
        if low_billed:
            truth = price_claim(lines, contract, self.fees)
            paid_as = price_claim(lines, payer_contract, self.fees)

        payer_terms = payer_contract.resolve(dos).terms
        no_mpr = flatten(payer_contract, dos, payer_terms.model_copy(update={"mpr_secondary_pct": None}))
        unreduced = price_claim(lines, no_mpr, self.fees)

        denied = error in (PlantedError.INVALID_TIMELY_DENIAL, PlantedError.VALID_TIMELY_DENIAL)
        remit_lines, truths = [], []
        copay_left = Decimal(rng.choice([20, 25, 30, 40]))
        for line, expected, payer, full in zip(lines, truth, paid_as, unreduced, strict=True):
            reduction = max((full.expected_allowed or ZERO) - (payer.expected_allowed or ZERO), ZERO)
            remit_line, allowed = self._remit_line(line, payer, denied, copay_left, reduction)
            copay_left -= remit_line.patient_responsibility if line.code in EM_CODES else ZERO
            copay_left = max(copay_left, ZERO)
            remit_lines.append(remit_line)
            truths.append(self._label(line, claim_id, expected, allowed, error, payment))

        claim = Claim(
            claim_id=claim_id,
            payer_id=contract.payer_id,
            provider_npi=contract.provider_npi,
            submitted_date=submitted,
            lines=tuple(lines),
        )
        remit_claim = RemitClaim(
            claim_id=claim_id,
            status=ClaimStatus.DENIED if denied else ClaimStatus.PRIMARY,
            billed=sum((ln.billed for ln in lines), ZERO),
            paid=sum((rl.paid for rl in remit_lines), ZERO),
            patient_responsibility=sum((rl.patient_responsibility for rl in remit_lines), ZERO),
            payer_claim_id=f"{self.payer.prefix}{rng.randrange(10**9, 10**10)}",
            patient_name=f"PATIENT{rng.randrange(1000, 9999)}, TEST",
            lines=tuple(remit_lines),
        )
        return claim, remit_claim, truths, payment

    def _remit_line(self, line: ServiceLine, payer: LineExpectation, denied: bool, copay_left: Decimal,
                    reduction: Decimal) -> tuple[RemitLine, Decimal]:  # fmt: skip
        base = {"line_id": line.line_id, "code": line.code, "modifiers": line.modifiers, "units": line.units,
                "billed": line.billed, "service_date": line.service_date}  # fmt: skip
        if denied or payer.expected_allowed is None:
            reason = "29" if denied else "96"
            adjustment = Adjustment(group=AdjustmentGroup.CO, reason=reason, amount=line.billed)
            return RemitLine(**base, paid=ZERO, adjustments=(adjustment,)), ZERO
        allowed = payer.expected_allowed
        patient = ZERO
        if line.code in EM_CODES and copay_left > 0:
            patient = min(copay_left, allowed)
            pr = [Adjustment(group=AdjustmentGroup.PR, reason="3", amount=patient)] if patient else []
        elif self.rng.random() < 0.3:
            patient = to_money(allowed * Decimal("0.20"))
            pr = [Adjustment(group=AdjustmentGroup.PR, reason="2", amount=patient)]
        else:
            pr = []
        co: list[Adjustment] = []
        contractual = line.billed - allowed - reduction
        if reduction and contractual >= 0:
            co.append(Adjustment(group=AdjustmentGroup.CO, reason="59", amount=reduction))
        else:
            contractual = line.billed - allowed
        if contractual:
            co.insert(0, Adjustment(group=AdjustmentGroup.CO, reason="45", amount=contractual))
        remit = RemitLine(
            **base,
            paid=allowed - patient,
            adjustments=(*co, *pr),
            allowed=allowed if self.payer.sends_allowed else None,
        )
        return remit, allowed

    def _label(self, line: ServiceLine, claim_id: str, expected: LineExpectation, payer_allowed: Decimal,
               error: PlantedError, payment: date) -> LineTruth:  # fmt: skip
        variance = (expected.expected_allowed or ZERO) - payer_allowed
        if error is PlantedError.INVALID_TIMELY_DENIAL:
            label = Label.DENIAL_INVALID
        elif error is PlantedError.VALID_TIMELY_DENIAL:
            label = Label.DENIAL_VALID
        elif abs(variance) <= TOLERANCE:
            label, variance = Label.CORRECT, ZERO
        else:
            label = Label.UNDERPAID if variance > 0 else Label.OVERPAID
        effective = error if label is not Label.CORRECT else PlantedError.NONE
        recoverable = variance if label in (Label.UNDERPAID, Label.DENIAL_INVALID) else ZERO
        return LineTruth(
            line_id=line.line_id, claim_id=claim_id, contract_id=self.contract.contract_id, code=line.code,
            service_date=line.service_date, payment_date=payment, label=label, planted_error=effective,
            expected_allowed=expected.expected_allowed, payer_allowed=payer_allowed, variance=variance,
            recoverable=recoverable,
        )  # fmt: skip


def make_batch(rng: random.Random, contract: Contract, payer: Payer, practice: Practice, fees: FeeTable,
               n_claims: int) -> ClaimBatch:  # fmt: skip
    factory = ClaimFactory(rng, contract, payer, practice, fees)
    claims, truths = [], []
    by_payment: dict[date, list[RemitClaim]] = defaultdict(list)
    for number in range(1, n_claims + 1):
        claim, remit_claim, line_truths, payment = factory.make(number)
        claims.append(claim)
        truths += line_truths
        by_payment[payment].append(remit_claim)
    remits = [
        Remittance(
            payer_id=payer.payer_id,
            payer_name=payer.name.upper(),
            payee_npi=practice.npi,
            payee_name=practice.name.upper(),
            check_number=f"EFT{contract.contract_id[-3:]}{payment:%Y%m%d}",
            payment_date=payment,
            total_paid=sum((c.paid for c in group), ZERO),
            claims=tuple(group),
        )
        for payment, group in sorted(by_payment.items())
    ]
    return ClaimBatch(claims=claims, remits=remits, truths=truths)
