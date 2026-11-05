"""Random but plausible contract terms, with amendments."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from recoup.core.fees import FeeTable
from recoup.core.models import Amendment, CarveOut, Contract, Terms, TermsPatch
from recoup.core.money import to_money
from recoup.synth.parties import Payer, Practice

PCTS = [Decimal(x) for x in ("1.05", "1.10", "1.15", "1.20", "1.25", "1.30", "1.35", "1.40")]
MPR_PCTS = [Decimal(x) for x in ("0.50", "0.60", "0.75")]
BILATERAL = [Decimal(x) for x in ("1.50", "1.50", "1.25", "1.75")]
FILING_DAYS = [90, 120, 180, 365]
APPEAL_DAYS = [60, 90, 120, 180]
YEAR = 2026


def _pick_codes(rng: random.Random, fees: FeeTable, count: int) -> list[str]:
    return sorted(rng.sample(fees.codes(), count))


def make_terms(rng: random.Random, fees: FeeTable) -> Terms:
    fee_codes = _pick_codes(rng, fees, rng.randint(3, 8))
    fee_schedule = {}
    for code in fee_codes:
        medicare = fees.fee(code, YEAR)
        assert medicare is not None
        fee_schedule[code] = to_money(medicare * Decimal(rng.randint(100, 160)) / 100)

    procedure_codes = [c for c in fees.codes() if fees.mpr_eligible(c) and c not in fee_schedule]
    carve_outs = {}
    for code in rng.sample(procedure_codes, rng.choice([0, 0, 1, 2])):
        medicare = fees.fee(code, YEAR)
        assert medicare is not None
        carve_outs[code] = CarveOut(
            rate=to_money(medicare * Decimal(rng.randint(110, 150)) / 100),
            mpr_exempt=rng.random() < 0.8,
        )

    modifiers = {"50": rng.choice(BILATERAL)}
    if rng.random() < 0.6:
        modifiers["52"] = Decimal("0.50")
    if rng.random() < 0.3:
        modifiers["80"] = Decimal("0.16")

    return Terms(
        pct_medicare=rng.choice(PCTS),
        medicare_year=YEAR,
        fee_schedule=fee_schedule,
        carve_outs=carve_outs,
        modifiers=modifiers,
        mpr_secondary_pct=rng.choice(MPR_PCTS) if rng.random() < 0.8 else None,
        lesser_of_billed=rng.random() < 0.7,
        timely_filing_days=rng.choice(FILING_DAYS),
        appeal_window_days=rng.choice(APPEAL_DAYS),
    )


def make_amendments(rng: random.Random, terms: Terms, fees: FeeTable) -> tuple[Amendment, ...]:
    count = rng.choice([0, 1, 1, 2])
    months = sorted(rng.sample(range(3, 10), count))
    amendments = []
    for number, month in enumerate(months, start=1):
        kind = rng.choice(["pct", "fee_rows", "modifier"])
        if kind == "pct" and terms.pct_medicare is not None:
            patch = TermsPatch(pct_medicare=terms.pct_medicare + Decimal(rng.choice([5, 10, -5])) / 100)
        elif kind == "modifier":
            patch = TermsPatch(
                modifiers={"50": rng.choice([b for b in BILATERAL if b != terms.modifiers["50"]])}
            )
        else:
            changed = rng.sample(sorted(terms.fee_schedule), min(2, len(terms.fee_schedule)))
            new_code = rng.choice([c for c in fees.codes() if c not in terms.fee_schedule])
            rows = {code: to_money(terms.fee_schedule[code] * Decimal("1.06")) for code in changed}
            medicare = fees.fee(new_code, YEAR)
            assert medicare is not None
            rows[new_code] = to_money(medicare * Decimal("1.45"))
            patch = TermsPatch(fee_schedule=rows)
        amendments.append(
            Amendment(amendment_id=f"A{number}", effective_date=date(YEAR, month, 1), patch=patch)
        )
    return tuple(amendments)


def make_contract(rng: random.Random, fees: FeeTable, payer: Payer, practice: Practice, seq: int) -> Contract:
    terms = make_terms(rng, fees)
    return Contract(
        contract_id=f"{payer.prefix}-{YEAR}-{seq:03d}",
        payer_id=payer.payer_id,
        payer_name=payer.name,
        provider_npi=practice.npi,
        effective_date=date(YEAR, 1, 1),
        end_date=date(YEAR, 12, 31),
        terms=terms,
        amendments=make_amendments(rng, terms, fees),
    )


def random_service_date(rng: random.Random, contract: Contract) -> date:
    end = contract.end_date or contract.effective_date + timedelta(days=365)
    span = (end - contract.effective_date).days - 45
    return contract.effective_date + timedelta(days=rng.randint(4, span))
