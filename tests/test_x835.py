from datetime import date
from decimal import Decimal as D

import pytest

from recoup.core.models import (
    Adjustment,
    AdjustmentGroup,
    ClaimStatus,
    RemitClaim,
    RemitLine,
    Remittance,
)
from recoup.x12.x835 import X12ParseError, detect_delimiters, parse_835, write_835

REMIT = Remittance(
    payer_id="60054",
    payer_name="NORTHWIND HEALTH PLAN",
    payee_npi="1234567893",
    payee_name="HUDSON ORTHO PC",
    check_number="EFT000123",
    payment_date=date(2026, 3, 20),
    total_paid=D("183.45"),
    claims=(
        RemitClaim(
            claim_id="CLM-1001",
            status=ClaimStatus.PRIMARY,
            billed=D("650"),
            paid=D("183.45"),
            patient_responsibility=D("20"),
            payer_claim_id="PCN998877",
            patient_name="DOE, JANE",
            lines=(
                RemitLine(
                    line_id="CLM-1001-1",
                    code="99213",
                    modifiers=("25",),
                    billed=D("150"),
                    paid=D("90.76"),
                    service_date=date(2026, 3, 2),
                    adjustments=(
                        Adjustment(group=AdjustmentGroup.CO, reason="45", amount=D("39.24")),
                        Adjustment(group=AdjustmentGroup.PR, reason="3", amount=D("20")),
                    ),
                    allowed=D("110.76"),
                ),
                RemitLine(
                    line_id="CLM-1001-2",
                    code="20610",
                    modifiers=("50", "RT"),
                    units=2,
                    billed=D("500"),
                    paid=D("92.69"),
                    service_date=date(2026, 3, 2),
                    adjustments=(
                        Adjustment(group=AdjustmentGroup.CO, reason="45", amount=D("400.31")),
                        Adjustment(group=AdjustmentGroup.CO, reason="59", amount=D("7"), quantity=D("1")),
                    ),
                    remarks=("N130",),
                ),
            ),
        ),
    ),
)


def test_round_trip():
    text = write_835(REMIT)
    [parsed] = parse_835(text)
    assert parsed == REMIT


def test_delimiters_come_from_isa():
    text = write_835(REMIT).replace("*", "|").replace(":", ">")
    assert detect_delimiters(text).element == "|"
    [parsed] = parse_835(text)
    assert parsed.claims[0].lines[1].modifiers == ("50", "RT")


def test_allowed_falls_back_to_paid_plus_patient_share():
    line = REMIT.claims[0].lines[1]
    assert line.payer_allowed == D("92.69")
    assert REMIT.claims[0].lines[0].payer_allowed == D("110.76")


def test_cas_keeps_quantity_slots_between_triples():
    text = write_835(REMIT)
    assert "CAS*CO*45*400.31**59*7*1" in text


def test_single_line_file_without_newlines():
    text = write_835(REMIT).replace("\n", "")
    assert parse_835(text)[0] == REMIT


@pytest.mark.parametrize(
    ("needle", "replacement", "message"),
    [
        ("ST*835", "ST*837", "not an 835"),
        ("SVC*HC:99213:25*150", "SVC*HC:99213:25*1x0", "bad amount"),
        ("DTM*405*20260320", "DTM*405*2026-03-20", "bad date"),
    ],
)
def test_errors_name_the_segment(needle, replacement, message):
    text = write_835(REMIT).replace(needle, replacement)
    with pytest.raises(X12ParseError, match=message):
        parse_835(text)


def test_rejects_non_x12():
    with pytest.raises(X12ParseError):
        parse_835("hello")


def two_sets(text: str) -> str:
    """Duplicate the ST..SE body so one interchange carries two transaction sets."""
    start, end = text.index("ST*835"), text.index("GE*")
    return text[:end] + text[start:end].replace("EFT000123", "EFT000124") + text[end:]


def test_multiple_transaction_sets():
    remits = parse_835(two_sets(write_835(REMIT)))
    assert [r.check_number for r in remits] == ["EFT000123", "EFT000124"]


def test_set_without_se_is_an_error():
    text = two_sets(write_835(REMIT))
    last_se = text.rindex("SE*")
    text = text[:last_se] + text[text.index("~", last_se) + 2 :]
    with pytest.raises(X12ParseError, match="no SE"):
        parse_835(text)


def test_segment_count_is_checked():
    with pytest.raises(X12ParseError, match="SE01"):
        parse_835(write_835(REMIT).replace("SE*22*", "SE*21*"))


def test_claim_level_cas_and_forwarded_status():
    claim = REMIT.claims[0].model_copy(
        update={
            "status": ClaimStatus.PRIMARY_FORWARDED,
            "adjustments": (Adjustment(group=AdjustmentGroup.OA, reason="253", amount=D("1.20")),),
        }
    )
    remit = REMIT.model_copy(update={"claims": (claim,)})
    [parsed] = parse_835(write_835(remit))
    assert parsed.claims[0].status is ClaimStatus.PRIMARY_FORWARDED
    assert parsed.claims[0].adjustments == claim.adjustments
    assert parsed.claims[0].lines[0].adjustments == claim.lines[0].adjustments


def test_fractional_units_survive():
    line = REMIT.claims[0].lines[0].model_copy(update={"units": D("0.5")})
    claim = REMIT.claims[0].model_copy(update={"lines": (line,)})
    [parsed] = parse_835(write_835(REMIT.model_copy(update={"claims": (claim,)})))
    assert parsed.claims[0].lines[0].units == D("0.5")


def test_payment_date_falls_back_to_bpr16():
    text = write_835(REMIT).replace("DTM*405*20260320~\n", "")
    text = text.replace("SE*22*", "SE*21*")
    assert parse_835(text)[0].payment_date == date(2026, 3, 20)


def test_line_service_period_start_when_no_472():
    text = write_835(REMIT).replace("DTM*472*20260302", "DTM*150*20260301", 1)
    assert parse_835(text)[0].claims[0].lines[0].service_date == date(2026, 3, 1)


def test_reversal_and_correction_get_distinct_fallback_line_ids():
    text = write_835(REMIT).replace("REF*6R*CLM-1001-1~\n", "").replace("REF*6R*CLM-1001-2~\n", "")
    start = text.index("CLP*")
    end = text.index("SE*")
    reversal = text[start:end].replace("CLP*CLM-1001*1*", "CLP*CLM-1001*22*")
    text = text[:start] + reversal + text[start:]
    segments = text[text.index("ST*") : text.index("SE*")].count("~")
    text = text.replace("SE*22*", f"SE*{segments + 1}*")
    claims = parse_835(text)[0].claims
    ids = [line.line_id for claim in claims for line in claim.lines]
    assert len(ids) == len(set(ids)) == 4


def test_payee_tax_id_is_not_taken_as_npi():
    text = write_835(REMIT).replace(
        "N1*PE*HUDSON ORTHO PC*XX*1234567893", "N1*PE*HUDSON ORTHO PC*FI*123456789"
    )
    with pytest.raises(X12ParseError, match="payee_npi"):
        parse_835(text)


def test_writer_rejects_delimiters_in_text():
    with pytest.raises(ValueError, match="delimiter"):
        write_835(REMIT.model_copy(update={"payee_name": "SMITH*JONES MD"}))


def test_bom_and_nan():
    assert parse_835("﻿" + write_835(REMIT))[0] == REMIT
    with pytest.raises(X12ParseError, match="bad amount"):
        parse_835(write_835(REMIT).replace("SVC*HC:99213:25*150", "SVC*HC:99213:25*NaN"))
