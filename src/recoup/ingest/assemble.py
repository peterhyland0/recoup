"""Grounded evidence to a core Contract, plus the status that decides who trusts it."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from recoup.config import Tenant
from recoup.core.models import Amendment, CarveOut, Contract, Terms, TermsPatch
from recoup.ingest.validate import Evidence


class IngestStatus(StrEnum):
    ACTIVE = "active"
    """Every value grounded and confident. Findings can go straight out."""
    NEEDS_REVIEW = "needs_review"
    """Usable for audit, but a person should confirm the flagged values."""
    FAILED = "failed"
    """Could not build a contract. Nothing is priced against it."""


class IngestedContract(BaseModel):
    document_id: str
    filename: str
    extractor: str
    status: IngestStatus
    contract: Contract | None
    evidence: list[Evidence]
    injection_span_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    def evidence_for(self, term_key: str, source: str | None) -> Evidence | None:
        """Map an engine trace step (term key plus 'base' or an amendment id) to its evidence."""
        key = term_key if source in (None, "base") else f"amendment.{source}.{term_key}"
        for item in self.evidence:
            if item.key == key:
                return item
        return None


def _scalar(name: str, raw: str) -> object:
    if name in ("pct_medicare", "mpr_secondary_pct"):
        return None if raw == "none" else Decimal(raw)
    if name in ("medicare_year", "timely_filing_days", "appeal_window_days"):
        return int(raw)
    if name == "lesser_of_billed":
        return raw == "true"
    raise KeyError(name)


SCALARS = ("pct_medicare", "medicare_year", "mpr_secondary_pct", "lesser_of_billed", "timely_filing_days",
           "appeal_window_days")  # fmt: skip


HEADER_KEYS = {"contract_id", "payer_name", "provider_npi", "effective_date", "end_date"}


def _terms_fields(values: dict[str, str], issues: list[str], where: str) -> dict[str, object]:
    fields: dict[str, object] = {"fee_schedule": {}, "modifiers": {}, "carve_outs": {}}
    unset: list[str] = []
    for key, raw in values.items():
        family, _, code = key.partition(".")
        if family in SCALARS and not code:
            value = _scalar(family, raw)
            if value is None:
                unset.append(family)
            else:
                fields[family] = value
        elif family == "fee_schedule":
            fields["fee_schedule"][code] = Decimal(raw)  # type: ignore[index]
        elif family == "modifier":
            fields["modifiers"][code] = Decimal(raw)  # type: ignore[index]
        elif family == "carve_out" and code.endswith(".mpr_exempt"):
            continue
        elif family == "carve_out":
            exempt = values.get(f"carve_out.{code}.mpr_exempt")
            if exempt is None:
                issues.append(
                    f"{where}carve-out {code}: multiple-procedure treatment not found, assumed exempt"
                )
            fields["carve_outs"][code] = CarveOut(rate=Decimal(raw), mpr_exempt=exempt != "false")  # type: ignore[index]
        elif key not in HEADER_KEYS:
            issues.append(f"{where}{key}: not a term this system understands, ignored")
    fields["_unset"] = unset
    return fields


def assemble(evidence: list[Evidence], tenant: Tenant, threshold: float | None = None
             ) -> tuple[Contract | None, IngestStatus, list[str]]:  # fmt: skip
    threshold = tenant.policy.confidence_threshold if threshold is None else threshold
    issues: list[str] = [f"{e.key}: {issue}" for e in evidence for issue in e.issues]
    grounded = {e.key: e.value for e in evidence if e.grounded}

    base = {k: v for k, v in grounded.items() if not k.startswith("amendment.")}
    by_amendment: dict[str, dict[str, str]] = defaultdict(dict)
    for key, value in grounded.items():
        if key.startswith("amendment."):
            parts = key.split(".", 2)
            if len(parts) < 3:
                issues.append(f"{key}: malformed amendment key, ignored")
                continue
            by_amendment[parts[1]][parts[2]] = value

    missing = [k for k in ("contract_id", "effective_date", "provider_npi", "payer_name") if k not in base]
    if missing:
        return None, IngestStatus.FAILED, [*issues, f"missing {', '.join(missing)}"]
    payer = tenant.payer_for(base["payer_name"])
    if payer is None:
        return (
            None,
            IngestStatus.FAILED,
            [*issues, f"payer {base['payer_name']!r} is not configured for this tenant"],
        )

    fields = _terms_fields(base, issues, "")
    fields.pop("_unset")
    terms = Terms.model_validate(fields)
    if terms.pct_medicare is None and not terms.fee_schedule and not terms.carve_outs:
        return None, IngestStatus.FAILED, [*issues, "no pricing terms found"]

    amendments = []
    for amendment_id, values in sorted(by_amendment.items()):
        effective = values.pop("effective_date", None)
        if effective is None:
            issues.append(f"amendment {amendment_id}: no effective date, so it was not applied")
            continue
        if values.get("medicare_year") == base.get("medicare_year"):
            values.pop("medicare_year")  # restating the same schedule year is not a change
        if not values:
            issues.append(f"amendment {amendment_id}: no change could be read from it")
        patch_fields = _terms_fields(values, issues, f"amendment {amendment_id}: ")
        unset = patch_fields.pop("_unset")
        patch = TermsPatch.model_validate({**patch_fields, "unset": tuple(unset)})  # type: ignore[arg-type]
        amendments.append(Amendment(amendment_id=amendment_id, effective_date=date.fromisoformat(effective),
                                    patch=patch))  # fmt: skip

    contract = Contract(
        contract_id=base["contract_id"],
        payer_id=payer.payer_id,
        payer_name=base["payer_name"],
        provider_npi=base["provider_npi"],
        effective_date=date.fromisoformat(base["effective_date"]),
        end_date=date.fromisoformat(base["end_date"]) if "end_date" in base else None,
        terms=terms,
        amendments=tuple(amendments),
    )
    unsure = [e.key for e in evidence if e.confidence < threshold]
    if unsure:
        issues.append(f"low confidence: {', '.join(unsure)}")
    status = IngestStatus.NEEDS_REVIEW if issues else IngestStatus.ACTIVE
    return contract, status, issues
