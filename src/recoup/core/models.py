"""Domain models: contract terms, amendments, claims and remittances.

Terms are plain values. Where a value came from (which span of which PDF) lives in the
extraction layer, keyed by the same term keys the rate engine writes into its trace.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class CarveOut(Frozen):
    """A code paid at a flat rate outside the normal method."""

    rate: Decimal
    mpr_exempt: bool = True


class Terms(Frozen):
    """The pricing terms in force on a given date."""

    pct_medicare: Decimal | None = None
    """Multiplier on the Medicare fee, e.g. 1.15 for 115%. Fallback when no fee row exists."""
    medicare_year: int | None = None
    """Which Medicare fee schedule year pct_medicare refers to."""
    fee_schedule: dict[str, Decimal] = Field(default_factory=dict)
    carve_outs: dict[str, CarveOut] = Field(default_factory=dict)
    modifiers: dict[str, Decimal] = Field(default_factory=dict)
    """Modifier code to payment multiplier, e.g. {"50": 1.5}. Unlisted modifiers pay 1.0."""
    mpr_secondary_pct: Decimal | None = None
    """Multiple procedure reduction: second and later eligible procedures pay this share."""
    lesser_of_billed: bool = False
    timely_filing_days: int | None = None
    appeal_window_days: int | None = None


class TermsPatch(Frozen):
    """An amendment's changes. None means unchanged; dict entries merge key by key."""

    pct_medicare: Decimal | None = None
    medicare_year: int | None = None
    fee_schedule: dict[str, Decimal] = Field(default_factory=dict)
    carve_outs: dict[str, CarveOut] = Field(default_factory=dict)
    modifiers: dict[str, Decimal] = Field(default_factory=dict)
    mpr_secondary_pct: Decimal | None = None
    lesser_of_billed: bool | None = None
    timely_filing_days: int | None = None
    appeal_window_days: int | None = None
    unset: tuple[str, ...] = ()
    """Term keys this amendment removes, e.g. "mpr_secondary_pct" or "carve_out.20610"."""


class Amendment(Frozen):
    amendment_id: str
    effective_date: date
    patch: TermsPatch


class Contract(Frozen):
    contract_id: str
    payer_id: str
    payer_name: str
    provider_npi: str
    effective_date: date
    end_date: date | None = None
    terms: Terms
    amendments: tuple[Amendment, ...] = ()

    def covers(self, dos: date) -> bool:
        return self.effective_date <= dos and (self.end_date is None or dos <= self.end_date)

    def resolve(self, dos: date) -> ResolvedTerms:
        """Apply every amendment effective on or before the date of service, in date order."""
        if not self.covers(dos):
            raise ValueError(f"{self.contract_id} does not cover {dos}")
        data = self.terms.model_dump()
        source = {key: "base" for key in _term_keys(self.terms)}
        # Same-date amendments apply in document order (sorted is stable).
        for amendment in sorted(self.amendments, key=lambda a: a.effective_date):
            if amendment.effective_date > dos:
                continue
            patch = amendment.patch
            for key in patch.unset:
                name, _, code = key.partition(".")
                if code and name in _PREFIX_FIELD:
                    data[_PREFIX_FIELD[name]].pop(code, None)
                elif not code and name in _SCALARS:
                    data[name] = False if name == "lesser_of_billed" else None
                else:
                    raise ValueError(f"{amendment.amendment_id} unsets unknown term {key!r}")
                source[key] = amendment.amendment_id
            for name in ("fee_schedule", "carve_outs", "modifiers"):
                for code, value in getattr(patch, name).items():
                    data[name][code] = value
                    source[f"{_KEY_PREFIX[name]}.{code}"] = amendment.amendment_id
            for name in _SCALARS:
                value = getattr(patch, name)
                if value is not None:
                    data[name] = value
                    source[name] = amendment.amendment_id
        return ResolvedTerms(terms=Terms.model_validate(data), source=source)


_SCALARS = (
    "pct_medicare",
    "medicare_year",
    "mpr_secondary_pct",
    "lesser_of_billed",
    "timely_filing_days",
    "appeal_window_days",
)
_KEY_PREFIX = {"fee_schedule": "fee_schedule", "carve_outs": "carve_out", "modifiers": "modifier"}
_PREFIX_FIELD = {prefix: name for name, prefix in _KEY_PREFIX.items()}


def _term_keys(terms: Terms) -> list[str]:
    keys = [name for name in _SCALARS if getattr(terms, name) is not None]
    for name, prefix in _KEY_PREFIX.items():
        keys += [f"{prefix}.{code}" for code in getattr(terms, name)]
    return keys


class ResolvedTerms(Frozen):
    terms: Terms
    source: dict[str, str]
    """Term key to 'base' or the amendment id that last set it."""


class ServiceLine(Frozen):
    line_id: str
    code: str
    modifiers: tuple[str, ...] = ()
    units: Decimal = Decimal(1)
    billed: Decimal
    service_date: date


class AdjustmentGroup(StrEnum):
    CO = "CO"  # contractual obligation
    PR = "PR"  # patient responsibility
    OA = "OA"  # other adjustment
    PI = "PI"  # payer initiated
    CR = "CR"  # correction or reversal


class Adjustment(Frozen):
    group: AdjustmentGroup
    reason: str
    """CARC code."""
    amount: Decimal
    quantity: Decimal | None = None


class RemitLine(Frozen):
    line_id: str
    code: str
    modifiers: tuple[str, ...] = ()
    units: Decimal = Decimal(1)
    billed: Decimal
    paid: Decimal
    service_date: date
    adjustments: tuple[Adjustment, ...] = ()
    allowed: Decimal | None = None
    """AMT*B6 when the payer sends it."""
    remarks: tuple[str, ...] = ()
    """RARC codes from LQ*HE."""

    @property
    def patient_responsibility(self) -> Decimal:
        return sum(
            (a.amount for a in self.adjustments if a.group is AdjustmentGroup.PR),
            Decimal("0"),
        )

    @property
    def payer_allowed(self) -> Decimal:
        """What the payer allowed: AMT*B6 if sent, else paid plus patient responsibility."""
        if self.allowed is not None:
            return self.allowed
        return self.paid + self.patient_responsibility


class ClaimStatus(StrEnum):
    PRIMARY = "1"
    SECONDARY = "2"
    TERTIARY = "3"
    DENIED = "4"
    PRIMARY_FORWARDED = "19"
    SECONDARY_FORWARDED = "20"
    TERTIARY_FORWARDED = "21"
    REVERSAL = "22"
    NOT_OUR_CLAIM = "23"
    PREDETERMINATION = "25"


class RemitClaim(Frozen):
    claim_id: str
    """CLP01, the provider's patient control number."""
    status: ClaimStatus
    billed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    payer_claim_id: str
    patient_name: str = ""
    lines: tuple[RemitLine, ...]
    adjustments: tuple[Adjustment, ...] = ()


class Remittance(Frozen):
    """One 835 transaction set."""

    payer_id: str
    payer_name: str
    payee_npi: str
    payee_name: str
    check_number: str
    payment_date: date
    total_paid: Decimal
    claims: tuple[RemitClaim, ...]


class Claim(Frozen):
    """The provider's side of a claim, from the practice management system."""

    claim_id: str
    payer_id: str
    provider_npi: str
    submitted_date: date
    lines: tuple[ServiceLine, ...]
