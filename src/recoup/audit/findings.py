"""Expected versus paid, per service line, with an explanation and a routing decision.

The audit never asks a model anything. It matches each remit line to a contract, prices
the claim with the rate engine, compares, and writes an explanation from the trace. A
finding goes to a person when it is actionable and rests on anything uncertain.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from recoup.config import Policy
from recoup.core.carc import DENIAL_REASONS, describe_carc
from recoup.core.engine import LineExpectation, TraceStep, price_claim
from recoup.core.fees import FeeTable
from recoup.core.models import AdjustmentGroup, Claim, ClaimStatus, RemitClaim, RemitLine, Remittance
from recoup.core.money import ZERO
from recoup.ingest.assemble import IngestedContract, IngestStatus
from recoup.ingest.validate import Evidence


class Kind(StrEnum):
    CORRECT = "correct"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    DENIAL_INVALID = "denial_invalid"
    DENIAL_VALID = "denial_valid"
    DENIAL_OTHER = "denial_other"
    UNPRICED = "unpriced"
    NO_CONTRACT = "no_contract"
    SKIPPED = "skipped"
    """Reversals, predeterminations, secondary payments: not priced against the primary contract."""


ACTIONABLE = {Kind.UNDERPAID, Kind.DENIAL_INVALID}
ALWAYS_REVIEW = {Kind.OVERPAID, Kind.DENIAL_OTHER, Kind.UNPRICED, Kind.NO_CONTRACT}


class Route(StrEnum):
    AUTO = "auto"
    REVIEW = "review"


class Step(BaseModel):
    rule: str
    detail: str
    amount: str
    term_key: str | None = None
    source: str | None = None
    span_ids: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str
    line_id: str
    claim_id: str
    payer_claim_id: str
    check_number: str
    payment_date: date
    service_date: date
    code: str
    modifiers: list[str]
    units: Decimal
    billed: Decimal
    contract_id: str | None
    document_id: str | None
    kind: Kind
    expected_allowed: Decimal | None
    payer_allowed: Decimal
    variance: Decimal
    recoverable: Decimal
    carcs: list[str]
    trace: list[Step]
    evidence: list[Evidence]
    explanation: str
    appeal_deadline: date | None
    route: Route
    review_reasons: list[str]


def _contract_for(remit: Remittance, dos: date, contracts: list[IngestedContract]
                  ) -> tuple[IngestedContract | None, str | None]:  # fmt: skip
    usable = [
        c for c in contracts
        if c.contract is not None and c.status is not IngestStatus.FAILED
        and c.contract.payer_id == remit.payer_id and c.contract.provider_npi == remit.payee_npi
        and c.contract.covers(dos)
    ]  # fmt: skip
    if not usable:
        return None, None
    usable.sort(key=lambda c: c.contract.effective_date if c.contract else date.min, reverse=True)
    note = None
    if len(usable) > 1:
        note = f"{len(usable)} contracts cover this date; used the latest, {usable[0].contract.contract_id}"  # type: ignore[union-attr]
    return usable[0], note


SKIP_STATUSES = {
    ClaimStatus.REVERSAL: "a reversal of an earlier payment",
    ClaimStatus.PREDETERMINATION: "a predetermination, not a payment",
    ClaimStatus.NOT_OUR_CLAIM: "forwarded as not this payer's claim",
    ClaimStatus.SECONDARY: "a secondary payment (coordination of benefits is not modelled)",
    ClaimStatus.SECONDARY_FORWARDED: "a secondary payment (coordination of benefits is not modelled)",
    ClaimStatus.TERTIARY: "a tertiary payment (coordination of benefits is not modelled)",
    ClaimStatus.TERTIARY_FORWARDED: "a tertiary payment (coordination of benefits is not modelled)",
}


def _denial_reason(line: RemitLine) -> str | None:
    """A zero-paid line is a denial whatever adjustment group the payer used (CO, OA or PI)."""
    if line.paid != 0:
        return None
    payer_side = [a for a in line.adjustments if a.group is not AdjustmentGroup.PR]
    for adjustment in payer_side:
        if adjustment.reason in DENIAL_REASONS:
            return adjustment.reason
    if payer_side and line.billed > 0 and line.patient_responsibility < line.billed:
        return payer_side[0].reason or "unknown"
    return None


def _steps(expectation: LineExpectation, ingested: IngestedContract) -> tuple[list[Step], list[Evidence]]:
    steps: list[Step] = []
    used: list[Evidence] = []

    def use(evidence: Evidence | None) -> None:
        if evidence is not None and evidence not in used:
            used.append(evidence)

    for step in expectation.trace:
        evidence = ingested.evidence_for(step.term_key, step.source) if step.term_key else None
        use(evidence)
        if step.term_key == "pct_medicare":
            # The percentage means nothing without the schedule year it applies to.
            use(
                ingested.evidence_for("medicare_year", step.source)
                or ingested.evidence_for("medicare_year", "base")
            )
        if step.source not in (None, "base"):
            use(ingested.evidence_for("effective_date", step.source))
        steps.append(_step(step, evidence))
    return steps, used


def _skipped(remit: Remittance, claim: RemitClaim, line: RemitLine, why: str) -> Finding:
    return Finding(
        finding_id=f"F-{line.line_id}",
        line_id=line.line_id,
        claim_id=claim.claim_id,
        payer_claim_id=claim.payer_claim_id,
        check_number=remit.check_number,
        payment_date=remit.payment_date,
        service_date=line.service_date,
        code=line.code,
        modifiers=list(line.modifiers),
        units=line.units,
        billed=line.billed,
        contract_id=None,
        document_id=None,
        kind=Kind.SKIPPED,
        expected_allowed=None,
        payer_allowed=line.payer_allowed,
        variance=ZERO,
        recoverable=ZERO,
        carcs=[a.reason for a in line.adjustments if a.group is not AdjustmentGroup.PR],
        trace=[],
        evidence=[],
        explanation=f"Not audited: this claim is {why}.",
        appeal_deadline=None,
        route=Route.AUTO,
        review_reasons=[],
    )


def _step(step: TraceStep, evidence: Evidence | None) -> Step:
    return Step(rule=step.rule, detail=step.detail, amount=f"{step.amount:.2f}", term_key=step.term_key,
                source=step.source, span_ids=evidence.span_ids if evidence else [])  # fmt: skip


def _where(evidence: list[Evidence]) -> str:
    spans = sorted({sid for e in evidence for sid in e.span_ids})
    return f" (cited at {', '.join(spans)})" if spans else ""


def explain(kind: Kind, contract_id: str | None, expected: Decimal | None, allowed: Decimal,
            evidence: list[Evidence], steps: list[Step], carcs: list[str], extra: str = "") -> str:  # fmt: skip
    codes = ", ".join(f"CARC {c} ({describe_carc(c)})" for c in carcs) or "no adjustment codes"
    if kind is Kind.NO_CONTRACT:
        return "No active contract covers this payer, provider and date of service."
    if kind is Kind.UNPRICED:
        return f"Contract {contract_id} gives no way to price this line: {extra}."
    if kind is Kind.DENIAL_OTHER:
        return f"Denied with {codes}. Recoup does not judge this denial reason; a person should."
    basis = "; ".join(s.detail for s in steps if s.rule != "round")
    if kind is Kind.DENIAL_INVALID:
        return (f"Denied with {codes}, but {extra}. Under contract {contract_id}{_where(evidence)} the line "
                f"should have allowed ${expected:.2f} ({basis}).")  # fmt: skip
    if kind is Kind.DENIAL_VALID:
        return f"Denied with {codes}, and {extra}, so the denial stands."
    assert expected is not None
    gap = expected - allowed
    if kind is Kind.CORRECT:
        return f"Paid as contracted: ${allowed:.2f} allowed under {contract_id} ({basis})."
    direction = "short" if gap > 0 else "over"
    return (f"Contract {contract_id}{_where(evidence)} sets the allowed amount at ${expected:.2f} ({basis}). "
            f"The payer allowed ${allowed:.2f}, ${abs(gap):.2f} {direction}. Payer codes: {codes}.")  # fmt: skip


def audit_claim(remit: Remittance, claim: RemitClaim, contracts: list[IngestedContract], claims: dict[str, Claim],
                fees: FeeTable, policy: Policy) -> list[Finding]:  # fmt: skip
    if not claim.lines:
        return []
    skip = SKIP_STATUSES.get(claim.status)
    if skip is None and any(line.paid < 0 for line in claim.lines):
        skip = "a negative payment (a recoupment or reversal)"
    if skip is not None:
        return [_skipped(remit, claim, line, skip) for line in claim.lines]
    dos = claim.lines[0].service_date
    ingested, note = _contract_for(remit, dos, contracts)
    priced: dict[str, LineExpectation] = {}
    if ingested is not None and ingested.contract is not None:
        priced = {e.line_id: e for e in price_claim(claim.lines, ingested.contract, fees)}
    source_claim = claims.get(claim.claim_id)
    findings = []
    for line in claim.lines:
        carcs = [a.reason for a in line.adjustments if a.group is not AdjustmentGroup.PR]
        reasons: list[str] = [note] if note else []
        expectation = priced.get(line.line_id)
        steps, used = _steps(expectation, ingested) if expectation and ingested else ([], [])
        expected = expectation.expected_allowed if expectation else None
        allowed = line.payer_allowed
        extra = ""
        denial = _denial_reason(line)
        contract = ingested.contract if ingested is not None else None
        covered = contract is not None and contract.covers(line.service_date)
        terms = contract.resolve(line.service_date).terms if covered and contract else None

        if ingested is None or ingested.contract is None:
            kind = Kind.NO_CONTRACT
        elif denial == "29":
            filing = terms.timely_filing_days if terms else None
            source = contract.resolve(line.service_date).source if covered and contract else {}
            filing_evidence = ingested.evidence_for("timely_filing_days", source.get("timely_filing_days"))
            if source_claim is None or filing is None:
                kind = Kind.DENIAL_OTHER
                reasons.append("timely-filing denial, but the submission date or filing limit is unknown")
            else:
                days = (source_claim.submitted_date - line.service_date).days
                if filing_evidence is not None:
                    used.append(filing_evidence)
                if days <= filing:
                    kind = Kind.DENIAL_INVALID
                    extra = f"the claim was submitted {days} days after the date of service, within the {filing}-day limit"
                else:
                    kind = Kind.DENIAL_VALID
                    extra = f"the claim was submitted {days} days after the date of service, past the {filing}-day limit"
        elif denial is not None:
            kind = Kind.DENIAL_OTHER
        elif expected is None:
            kind = Kind.UNPRICED
            extra = expectation.unpriced_reason if expectation and expectation.unpriced_reason else "no rate"
        elif abs(expected - allowed) <= policy.tolerance:
            kind = Kind.CORRECT
        else:
            kind = Kind.UNDERPAID if expected > allowed else Kind.OVERPAID

        variance = (expected or ZERO) - allowed
        if kind is Kind.UNDERPAID:
            recoverable = variance
        elif kind is Kind.DENIAL_INVALID:
            recoverable = variance if expected is not None else ZERO
        else:
            recoverable = ZERO

        if expectation and expectation.review_reason:
            reasons.append(expectation.review_reason)
        if kind in ALWAYS_REVIEW:
            reasons.append(f"{kind.value} findings are always checked by a person")
        if kind in ACTIONABLE:
            weak = [e.key for e in used if not e.grounded or e.confidence < policy.confidence_threshold]
            if weak:
                reasons.append(f"rests on low-confidence terms: {', '.join(weak)}")
            if ingested is not None and ingested.injection_span_ids:
                reasons.append("contract contains text addressed to automated readers")
            elif ingested is not None and ingested.status is IngestStatus.NEEDS_REVIEW:
                first = ingested.issues[0] if ingested.issues else "flagged at ingest"
                reasons.append(f"contract has not been verified: {first}")
            if kind is Kind.DENIAL_INVALID and expected is None:
                reasons.append("denial looks invalid but the line could not be priced")
        window = terms.appeal_window_days if terms else None
        deadline = remit.payment_date + timedelta(days=window) if window else None

        findings.append(Finding(
            finding_id=f"F-{line.line_id}",
            line_id=line.line_id, claim_id=claim.claim_id, payer_claim_id=claim.payer_claim_id,
            check_number=remit.check_number, payment_date=remit.payment_date, service_date=line.service_date,
            code=line.code, modifiers=list(line.modifiers), units=line.units, billed=line.billed,
            contract_id=ingested.contract.contract_id if ingested and ingested.contract else None,
            document_id=ingested.document_id if ingested else None,
            kind=kind, expected_allowed=expected, payer_allowed=allowed, variance=variance, recoverable=recoverable,
            carcs=carcs, trace=steps, evidence=used,
            explanation=explain(kind, ingested.contract.contract_id if ingested and ingested.contract else None,
                                expected, allowed, used, steps, carcs, extra),
            appeal_deadline=deadline,
            route=Route.REVIEW if reasons else Route.AUTO,
            review_reasons=reasons,
        ))  # fmt: skip
    return findings


def audit(remits: Iterable[Remittance], contracts: list[IngestedContract], claims: dict[str, Claim],
          fees: FeeTable, policy: Policy) -> list[Finding]:  # fmt: skip
    findings = []
    for remit in remits:
        for claim in remit.claims:
            findings += audit_claim(remit, claim, contracts, claims, fees, policy)
    return findings
