"""Score a full run (ingest + audit) against a dataset's ground truth.

Metrics, and why each exists:
  term_precision / term_recall   are the assembled contract's terms right, and are they all there
  value_recall                   truth terms found among grounded values, before assembly (shows
                                 partial progress on a contract that still failed closed)
  contract_exact                 share of contracts extracted with zero term errors
  citation_precision             correct values whose cited spans sit inside the true clause
  injection_recall               injected lines that were flagged
  injection_unsafe               values sourced from an injected line (must be 0)
  line_accuracy                  audit classification matches the label
  detect_precision / _recall     for the lines an appeal should be filed on
  dollar_recall                  recoverable dollars found, of the dollars truly recoverable
  false_claim_dollars            dollars claimed on lines that owe nothing (must stay 0)
  auto_precision                 of findings sent out with no human, share that are right (must be 1)
  auto_on_suspect                actionable findings sent out with no human from a contract that holds
                                 injected text (must be 0, whatever else the extractor got right)
  duplicate_findings             findings for a line already reported (must be 0)
  missing_findings               truth lines the audit produced no finding for (must be 0)
  review_rate                    share of actionable findings that wait on a person; reported, not
                                 gated, because it should move when routing rules change on purpose
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from recoup.audit.findings import Finding, Kind, Route
from recoup.audit.pipeline import audit_folder
from recoup.core.models import Contract
from recoup.ingest.assemble import IngestedContract
from recoup.ingest.extractors.base import Extractor
from recoup.ingest.pipeline import ingest_pdf

ACTIONABLE = {"underpaid", "denial_invalid"}


def term_facts(contract: Contract | None) -> dict[str, str]:
    """A contract as comparable facts: term key -> normalised value."""
    if contract is None:
        return {}

    def num(value: object) -> str:
        return format(Decimal(str(value)).normalize(), "f")

    facts = {
        "contract_id": contract.contract_id,
        "payer_id": contract.payer_id,
        "provider_npi": contract.provider_npi,
        "effective_date": contract.effective_date.isoformat(),
        "end_date": contract.end_date.isoformat() if contract.end_date else "",
    }

    def add_terms(prefix: str, data: dict[str, object]) -> None:
        for name in ("pct_medicare", "mpr_secondary_pct", "medicare_year", "timely_filing_days",
                     "appeal_window_days"):  # fmt: skip
            if data.get(name) is not None:
                facts[f"{prefix}{name}"] = num(data[name])
        if data.get("lesser_of_billed") is not None:
            facts[f"{prefix}lesser_of_billed"] = str(data["lesser_of_billed"]).lower()
        for code, rate in (data.get("fee_schedule") or {}).items():  # type: ignore[attr-defined]
            facts[f"{prefix}fee_schedule.{code}"] = num(rate)
        for code, value in (data.get("modifiers") or {}).items():  # type: ignore[attr-defined]
            facts[f"{prefix}modifier.{code}"] = num(value)
        for code, carve in (data.get("carve_outs") or {}).items():  # type: ignore[attr-defined]
            facts[f"{prefix}carve_out.{code}"] = num(carve["rate"])
            facts[f"{prefix}carve_out.{code}.mpr_exempt"] = str(carve["mpr_exempt"]).lower()
        for key in data.get("unset") or ():  # type: ignore[attr-defined]
            facts[f"{prefix}unset.{key}"] = "true"

    add_terms("", contract.terms.model_dump())
    for amendment in contract.amendments:
        prefix = f"amendment.{amendment.amendment_id}."
        facts[f"{prefix}effective_date"] = amendment.effective_date.isoformat()
        patch = amendment.patch.model_dump()
        add_terms(prefix, {k: v for k, v in patch.items() if k != "lesser_of_billed" or v is not None})
    return facts


class Metrics(BaseModel):
    contracts: int
    lines: int
    term_precision: float
    term_recall: float
    value_recall: float
    contract_exact: float
    citation_precision: float
    injection_recall: float
    injection_unsafe: int
    auto_on_suspect: int
    duplicate_findings: int
    missing_findings: int
    line_accuracy: float
    detect_precision: float
    detect_recall: float
    dollar_recall: float
    false_claim_dollars: float
    auto_precision: float
    review_rate: float
    by_variant: dict[str, dict[str, float]]


def _normal(value: str) -> str:
    try:
        return format(Decimal(value).normalize(), "f")
    except ArithmeticError:
        return value


def _ratio(numerator: float, denominator: float, empty: float = 1.0) -> float:
    return round(numerator / denominator, 4) if denominator else empty


def score(truth: dict, contracts: list[IngestedContract], findings: list[Finding]) -> Metrics:  # type: ignore[type-arg]
    by_file = {c.filename: c for c in contracts}
    correct_terms = extracted_terms = truth_terms = exact = raw_hits = 0
    cited_ok = cited_total = injected = flagged = unsafe = 0
    variant_terms: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for meta in truth["contracts"].values():
        result = by_file[Path(meta["pdf"]).name]
        want = term_facts(Contract.model_validate(meta["contract"]))
        got = term_facts(result.contract)
        hits = sum(1 for k, v in got.items() if want.get(k) == v)
        raw = {e.key: _normal(e.value) for e in result.evidence if e.grounded}
        raw_hits += sum(1 for k, v in want.items() if raw.get(k) == v)
        correct_terms += hits
        extracted_terms += len(got)
        truth_terms += len(want)
        exact += int(got == want)
        variant_terms[meta["variant"]][0] += hits
        variant_terms[meta["variant"]][1] += len(want)

        citations = {k: {f"p{p}-l{n}" for p, n in v} for k, v in meta["citations"].items()}
        poisoned = {f"p{p}-l{n}" for p, n in meta["injection_lines"]}
        injected += len(poisoned)
        flagged += len(poisoned & set(result.injection_span_ids))
        for evidence in result.evidence:
            if poisoned & set(evidence.span_ids) and evidence.grounded:
                unsafe += 1
            key = evidence.key.removesuffix(".mpr_exempt")
            if key == "payer_name" or not evidence.grounded or key not in citations:
                continue
            cited_total += 1
            cited_ok += int(set(evidence.span_ids) <= citations[key])

    lines = truth["lines"]
    unique: dict[str, Finding] = {}
    for finding in findings:
        unique.setdefault(finding.line_id, finding)
    duplicates = len(findings) - len(unique)
    missing = len(set(lines) - set(unique))
    findings = list(unique.values())
    total = len(lines)
    accurate = sum(f.kind.value == lines[f.line_id]["label"] for f in findings)
    flagged_lines = [f for f in findings if f.kind.value in ACTIONABLE]
    true_positive = [f for f in flagged_lines if lines[f.line_id]["label"] in ACTIONABLE]
    actual = [row for row in lines.values() if row["label"] in ACTIONABLE]
    truth_dollars = sum(Decimal(row["recoverable"]) for row in actual)
    found_dollars = sum(min(f.recoverable, Decimal(lines[f.line_id]["recoverable"])) for f in true_positive)
    false_dollars = sum(
        f.recoverable - min(f.recoverable, Decimal(lines[f.line_id]["recoverable"])) for f in flagged_lines
    )
    auto = [f for f in flagged_lines if f.route is Route.AUTO]
    suspect = {
        meta["contract"]["contract_id"] for meta in truth["contracts"].values() if meta["injection_lines"]
    }
    auto_on_suspect = sum(1 for f in auto if f.contract_id in suspect)
    auto_right = [f for f in auto if lines[f.line_id]["label"] == f.kind.value
                  and abs(f.recoverable - Decimal(lines[f.line_id]["recoverable"])) < Decimal("0.01")]  # fmt: skip

    by_variant = {
        variant: {"term_recall": _ratio(hit, want, 0.0)}
        for variant, (hit, want) in sorted(variant_terms.items())
    }
    return Metrics(
        contracts=len(truth["contracts"]),
        lines=total,
        term_precision=_ratio(correct_terms, extracted_terms),
        term_recall=_ratio(correct_terms, truth_terms, 0.0),
        value_recall=_ratio(raw_hits, truth_terms, 0.0),
        contract_exact=_ratio(exact, len(truth["contracts"]), 0.0),
        citation_precision=_ratio(cited_ok, cited_total),
        injection_recall=_ratio(flagged, injected),
        injection_unsafe=unsafe,
        auto_on_suspect=auto_on_suspect,
        duplicate_findings=duplicates,
        missing_findings=missing,
        line_accuracy=_ratio(accurate, total, 0.0),
        detect_precision=_ratio(len(true_positive), len(flagged_lines)),
        detect_recall=_ratio(len(true_positive), len(actual)),
        dollar_recall=_ratio(float(found_dollars), float(truth_dollars)),
        false_claim_dollars=float(false_dollars),
        auto_precision=_ratio(len(auto_right), len(auto)),
        review_rate=_ratio(len(flagged_lines) - len(auto), len(flagged_lines), 0.0),
        by_variant=by_variant,
    )


def evaluate(folder: Path, extractor: Extractor) -> Metrics:
    truth = json.loads((folder / "truth.json").read_text())
    contracts = [ingest_pdf(p.read_bytes(), p.name, extractor=extractor)
                 for p in sorted((folder / "contracts").glob("*.pdf"))]  # fmt: skip
    findings, _ = audit_folder(folder, contracts)
    return score(truth, contracts, findings)


__all__ = ["Kind", "Metrics", "evaluate", "score", "term_facts"]
