import json
from collections import Counter
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from recoup.audit.findings import Kind, Route, audit
from recoup.audit.pipeline import audit_folder, load_claims_csv
from recoup.config import Policy
from recoup.core.fees import default_fee_table
from recoup.core.models import (
    Adjustment,
    AdjustmentGroup,
    ClaimStatus,
    Contract,
    RemitClaim,
    RemitLine,
    Remittance,
    Terms,
)
from recoup.ingest.assemble import IngestedContract, IngestStatus
from recoup.ingest.pipeline import ingest_pdf
from recoup.synth.datasets import SPECS, build


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("d") / "smoke"
    build(SPECS["smoke"], out)
    return out


def test_audit_matches_ground_truth_on_smoke(smoke: Path):
    truth = json.loads((smoke / "truth.json").read_text())["lines"]
    contracts = [ingest_pdf(p.read_bytes(), p.name) for p in sorted((smoke / "contracts").glob("*.pdf"))]
    findings, summary = audit_folder(smoke, contracts)
    assert len(findings) == len(truth)
    mismatches = [(f.line_id, truth[f.line_id]["label"], f.kind.value) for f in findings
                  if truth[f.line_id]["label"] != f.kind.value]  # fmt: skip
    assert mismatches == []
    expected_recovery = sum(D(r["recoverable"]) for r in truth.values())
    assert summary.recoverable_auto + summary.recoverable_review == expected_recovery
    underpaid = next(f for f in findings if f.kind is Kind.UNDERPAID)
    assert underpaid.evidence and all(e.span_ids for e in underpaid.evidence)
    assert "short" in underpaid.explanation and underpaid.appeal_deadline is not None


def contract(npi: str = "1234567893") -> IngestedContract:
    terms = Terms(pct_medicare=D("1.2"), medicare_year=2026, timely_filing_days=90, appeal_window_days=60)
    model = Contract(contract_id="NW-2026-001", payer_id="60054", payer_name="Northwind Health Plan",
                     provider_npi=npi, effective_date=date(2026, 1, 1), end_date=date(2026, 12, 31), terms=terms)  # fmt: skip
    return IngestedContract(document_id="doc", filename="c.pdf", extractor="test", status=IngestStatus.ACTIVE,
                            contract=model, evidence=[])  # fmt: skip


def remit(*lines: RemitLine) -> Remittance:
    claim = RemitClaim(claim_id="C1", status=ClaimStatus.PRIMARY, billed=sum(ln.billed for ln in lines),
                       paid=sum(ln.paid for ln in lines), patient_responsibility=D(0), payer_claim_id="P1",
                       lines=lines)  # fmt: skip
    return Remittance(payer_id="60054", payer_name="NW", payee_npi="1234567893", payee_name="X", check_number="1",
                      payment_date=date(2026, 3, 20), total_paid=claim.paid, claims=(claim,))  # fmt: skip


def line(paid: str, *adjustments: Adjustment, code: str = "99213") -> RemitLine:
    return RemitLine(line_id="L1", code=code, billed=D("300"), paid=D(paid), service_date=date(2026, 3, 2),
                     adjustments=adjustments)  # fmt: skip


def test_no_contract_goes_to_review():
    [finding] = audit([remit(line("100"))], [contract(npi="9999999999")], {}, default_fee_table(), Policy())
    assert finding.kind is Kind.NO_CONTRACT and finding.route is Route.REVIEW


def test_timely_filing_denial_without_claim_data_is_not_judged():
    denied = line("0", Adjustment(group=AdjustmentGroup.CO, reason="29", amount=D("300")))
    [finding] = audit([remit(denied)], [contract()], {}, default_fee_table(), Policy())
    assert finding.kind is Kind.DENIAL_OTHER and finding.route is Route.REVIEW


def test_timely_filing_denial_inside_the_window_is_invalid():
    denied = line("0", Adjustment(group=AdjustmentGroup.CO, reason="29", amount=D("300")))
    claims = load_claims_csv(
        "claim_id,payer_id,provider_npi,submitted_date,line_id,code,modifiers,units,billed,service_date\n"
        "C1,60054,1234567893,2026-04-01,L1,99213,,1,300,2026-03-02\n"
    )
    [finding] = audit([remit(denied)], [contract()], claims, default_fee_table(), Policy())
    assert finding.kind is Kind.DENIAL_INVALID
    assert finding.recoverable == D("110.76")
    assert "30 days" in finding.explanation


def test_other_denials_go_to_a_person():
    denied = line("0", Adjustment(group=AdjustmentGroup.CO, reason="197", amount=D("300")))
    [finding] = audit([remit(denied)], [contract()], {}, default_fee_table(), Policy())
    assert finding.kind is Kind.DENIAL_OTHER and finding.route is Route.REVIEW


def test_tolerance_absorbs_rounding():
    paid = line("110.76", Adjustment(group=AdjustmentGroup.CO, reason="45", amount=D("189.24")))
    [finding] = audit([remit(paid)], [contract()], {}, default_fee_table(), Policy())
    assert finding.kind is Kind.CORRECT and finding.route is Route.AUTO


def test_low_confidence_term_routes_underpayment_to_review(smoke: Path):
    truth = json.loads((smoke / "truth.json").read_text())["lines"]
    contracts = [ingest_pdf(p.read_bytes(), p.name) for p in sorted((smoke / "contracts").glob("*.pdf"))]
    for item in contracts:
        for evidence in item.evidence:
            evidence.confidence = 0.5
    findings, _ = audit_folder(smoke, contracts)
    actionable = [f for f in findings if f.kind in (Kind.UNDERPAID, Kind.DENIAL_INVALID)]
    assert actionable and all(f.route is Route.REVIEW for f in actionable)
    assert Counter(f.kind.value for f in findings) == Counter(r["label"] for r in truth.values())
