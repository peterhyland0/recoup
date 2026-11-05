from datetime import date
from decimal import Decimal as D

from hypothesis import given
from hypothesis import strategies as st

from recoup.core.engine import price_claim
from recoup.core.fees import default_fee_table
from recoup.core.models import Amendment, CarveOut, Contract, ServiceLine, Terms, TermsPatch

FEES = default_fee_table()
DOS = date(2026, 3, 2)


def contract(**terms: object) -> Contract:
    base: dict[str, object] = {"pct_medicare": D("1.20"), "medicare_year": 2026}
    base.update(terms)
    return Contract(
        contract_id="C1",
        payer_id="P1",
        payer_name="Payer",
        provider_npi="1234567890",
        effective_date=date(2026, 1, 1),
        terms=Terms.model_validate(base),
    )


def line(
    code: str, line_id: str = "L1", modifiers: tuple[str, ...] = (), units: int = 1, billed: str = "999"
) -> ServiceLine:
    return ServiceLine(
        line_id=line_id, code=code, modifiers=modifiers, units=units, billed=D(billed), service_date=DOS
    )


def test_percent_of_medicare():
    [result] = price_claim([line("99213")], contract(), FEES)
    assert result.expected_allowed == D("110.76")  # 92.30 * 1.20
    assert [s.rule for s in result.trace] == ["base_rate", "round"]
    assert result.trace[0].term_key == "pct_medicare"


def test_fee_schedule_row_beats_percent_of_medicare():
    [result] = price_claim([line("99213")], contract(fee_schedule={"99213": D("101.00")}), FEES)
    assert result.expected_allowed == D("101.00")
    assert result.trace[0].term_key == "fee_schedule.99213"


def test_carve_out_beats_fee_schedule():
    terms = {"fee_schedule": {"20610": D("80")}, "carve_outs": {"20610": CarveOut(rate=D("95"))}}
    [result] = price_claim([line("20610")], contract(**terms), FEES)
    assert result.expected_allowed == D("95.00")


def test_unpriced_code_is_reported_not_guessed():
    [result] = price_claim([line("00000")], contract(), FEES)
    assert result.expected_allowed is None
    assert result.unpriced_reason and "00000" in result.unpriced_reason


def test_modifier_and_units():
    terms = {"fee_schedule": {"20610": D("80")}, "modifiers": {"50": D("1.5")}}
    [result] = price_claim([line("20610", modifiers=("50",), units=2)], contract(**terms), FEES)
    assert result.expected_allowed == D("240.00")
    assert [s.rule for s in result.trace] == ["base_rate", "modifier", "units", "round"]


def test_unlisted_modifier_pays_full():
    [result] = price_claim(
        [line("99213", modifiers=("25",))], contract(fee_schedule={"99213": D("100")}), FEES
    )
    assert result.expected_allowed == D("100.00")


def test_mpr_ranks_by_amount_and_skips_ineligible_codes():
    terms = {"mpr_secondary_pct": D("0.5")}
    lines = [line("20610", "A"), line("29881", "B"), line("11042", "C"), line("99213", "D")]
    results = {r.line_id: r for r in price_claim(lines, contract(**terms), FEES)}
    assert results["B"].expected_allowed == D("629.76")  # 524.80 * 1.2, primary
    assert results["A"].expected_allowed == D("38.58")  # 64.30 * 1.2 * 0.5
    assert results["C"].expected_allowed == D("72.78")  # 121.30 * 1.2 * 0.5
    assert results["D"].expected_allowed == D("110.76")  # E/M, not MPR eligible


def test_mpr_exempt_carve_out():
    terms = {"mpr_secondary_pct": D("0.5"), "carve_outs": {"20610": CarveOut(rate=D("70"))}}
    results = {
        r.line_id: r for r in price_claim([line("29881", "A"), line("20610", "B")], contract(**terms), FEES)
    }
    assert results["B"].expected_allowed == D("70.00")


def test_lesser_of_billed():
    [result] = price_claim([line("99213", billed="50")], contract(lesser_of_billed=True), FEES)
    assert result.expected_allowed == D("50.00")
    assert result.trace[-2].rule == "lesser_of"


def test_amendment_applies_only_from_its_date():
    amended = contract().model_copy(
        update={
            "amendments": (
                Amendment(
                    amendment_id="A1",
                    effective_date=date(2026, 3, 1),
                    patch=TermsPatch(pct_medicare=D("1.30"), fee_schedule={"99214": D("150")}),
                ),
            )
        }
    )
    before = ServiceLine(line_id="1", code="99213", billed=D("999"), service_date=date(2026, 2, 27))
    after = ServiceLine(line_id="2", code="99213", billed=D("999"), service_date=date(2026, 3, 1))
    early, late = price_claim([before, after], amended, FEES)
    assert early.expected_allowed == D("110.76")
    assert late.expected_allowed == D("119.99")  # 92.30 * 1.30
    assert late.trace[0].source == "A1"
    assert early.trace[0].source == "base"


def test_line_outside_contract_window_is_unpriced_not_fatal():
    expired = contract().model_copy(update={"end_date": date(2026, 3, 1)})
    inside = ServiceLine(line_id="1", code="99213", billed=D("999"), service_date=date(2026, 2, 28))
    outside = ServiceLine(line_id="2", code="99213", billed=D("999"), service_date=date(2026, 3, 2))
    first, second = price_claim([inside, outside], expired, FEES)
    assert first.expected_allowed == D("110.76")
    assert second.expected_allowed is None
    assert second.unpriced_reason and "outside" in second.unpriced_reason


def test_mpr_reduces_extra_units_of_a_lone_procedure():
    [result] = price_claim([line("17000", units=3)], contract(mpr_secondary_pct=D("0.5")), FEES)
    assert result.expected_allowed == D("158.16")  # 65.90 * 1.2 * (1 + 0.5 + 0.5)


def test_mpr_ranks_by_per_unit_amount():
    lines = [line("17000", "A", units=3), line("11042", "B")]
    results = {r.line_id: r for r in price_claim(lines, contract(mpr_secondary_pct=D("0.5")), FEES)}
    assert results["B"].expected_allowed == D("145.56")  # 121.30 * 1.2, primary by unit value
    assert results["A"].expected_allowed == D("118.62")  # 65.90 * 1.2 * 0.5 * 3


def test_mpr_groups_by_date_of_service():
    a = ServiceLine(line_id="A", code="29881", billed=D("9999"), service_date=date(2026, 3, 2))
    b = ServiceLine(line_id="B", code="20610", billed=D("9999"), service_date=date(2026, 3, 3))
    results = price_claim([a, b], contract(mpr_secondary_pct=D("0.5")), FEES)
    assert [r.expected_allowed for r in results] == [D("629.76"), D("77.16")]


def test_unpriced_line_flags_its_mpr_group_for_review():
    results = price_claim(
        [line("29999", "A"), line("20610", "B")], contract(mpr_secondary_pct=D("0.5")), FEES
    )
    assert results[0].expected_allowed is None
    assert results[1].review_reason and "29999" in results[1].review_reason


def test_fractional_units():
    [result] = price_claim([line("96372", units=D("0.5"))], contract(), FEES)
    assert result.expected_allowed == D("9.48")  # 15.80 * 1.2 * 0.5


def test_duplicate_modifier_applies_once():
    terms = {"fee_schedule": {"20610": D("80")}, "modifiers": {"50": D("1.5")}}
    [result] = price_claim([line("20610", modifiers=("50", "50"))], contract(**terms), FEES)
    assert result.expected_allowed == D("120.00")


def test_same_date_amendments_apply_in_document_order_and_can_unset():
    a10 = Amendment(
        amendment_id="AMD-10",
        effective_date=date(2026, 2, 1),
        patch=TermsPatch(mpr_secondary_pct=D("0.6"), carve_outs={"20610": CarveOut(rate=D("70"))}),
    )
    a2 = Amendment(
        amendment_id="AMD-2",
        effective_date=date(2026, 2, 1),
        patch=TermsPatch(unset=("mpr_secondary_pct", "carve_out.20610")),
    )
    resolved = contract(mpr_secondary_pct=D("0.5")).model_copy(update={"amendments": (a10, a2)}).resolve(DOS)
    assert resolved.terms.mpr_secondary_pct is None
    assert "20610" not in resolved.terms.carve_outs
    assert resolved.source["mpr_secondary_pct"] == "AMD-2"


amounts = st.decimals(min_value=D("0.01"), max_value=D("5000"), places=2)
codes = st.sampled_from(FEES.codes())
lines_strategy = st.lists(
    st.tuples(codes, st.integers(1, 4), amounts, st.sampled_from([(), ("50",), ("52",), ("25",)])),
    min_size=1,
    max_size=6,
)


@given(
    lines_strategy,
    st.decimals(min_value=D("0.5"), max_value=D("2.5"), places=2),
    st.one_of(st.none(), st.decimals(min_value=D("0.25"), max_value=D("1"), places=2)),
    st.booleans(),
)
def test_properties(spec, pct, mpr, lesser_of):
    terms = {
        "pct_medicare": pct,
        "mpr_secondary_pct": mpr,
        "lesser_of_billed": lesser_of,
        "modifiers": {"50": D("1.5"), "52": D("0.5")},
    }
    lines = [
        line(code, f"L{i}", mods, units, str(billed)) for i, (code, units, billed, mods) in enumerate(spec)
    ]
    results = price_claim(lines, contract(**terms), FEES)
    assert len(results) == len(lines)
    for result, source in zip(results, lines, strict=True):
        assert result.expected_allowed is not None
        assert result.expected_allowed >= 0
        assert result.expected_allowed == result.expected_allowed.quantize(D("0.01"))
        if lesser_of:
            assert result.expected_allowed <= source.billed
        assert result.trace[-1].amount == result.expected_allowed
