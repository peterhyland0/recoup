import json
from decimal import Decimal as D
from pathlib import Path

import pytest
from pypdf import PdfReader

from recoup.core.engine import price_claim
from recoup.core.fees import default_fee_table
from recoup.core.models import Contract
from recoup.synth.datasets import SPECS, build
from recoup.synth.parties import npi_with_check_digit
from recoup.synth.render import number_words
from recoup.x12.x835 import parse_835


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("data") / "smoke"
    build(SPECS["smoke"], out)
    return out


def test_same_seed_same_bytes(smoke: Path, tmp_path: Path):
    again = tmp_path / "smoke"
    build(SPECS["smoke"], again)
    for path in sorted(smoke.rglob("*")):
        if path.is_file():
            assert path.read_bytes() == (again / path.relative_to(smoke)).read_bytes(), path.name


def test_remits_balance(smoke: Path):
    for path in (smoke / "remits").glob("*.835"):
        for remit in parse_835(path.read_text()):
            assert remit.total_paid == sum(c.paid for c in remit.claims)
            for claim in remit.claims:
                for line in claim.lines:
                    assert line.billed - sum(a.amount for a in line.adjustments) == line.paid
                    assert line.paid >= 0


def test_citations_point_at_lines_that_state_the_value(smoke: Path):
    truth = json.loads((smoke / "truth.json").read_text())
    for meta in truth["contracts"].values():
        if meta["variant"] != "clean":
            continue
        pages = [
            [ln for ln in p.extract_text().split("\n") if ln.strip()]
            for p in PdfReader(smoke / meta["pdf"]).pages
        ]
        terms = meta["contract"]["terms"]
        for code, rate in terms["fee_schedule"].items():
            [(page, line)] = meta["citations"][f"fee_schedule.{code}"]
            text = pages[page - 1][line - 1]
            assert code in text and rate.rstrip("0").rstrip(".") in text


def test_labels_match_the_engine(smoke: Path):
    """Ground truth expected amounts are exactly what the rate engine says for the true contract."""
    truth = json.loads((smoke / "truth.json").read_text())
    contracts = {cid: Contract.model_validate(m["contract"]) for cid, m in truth["contracts"].items()}
    fees = default_fee_table()
    for path in (smoke / "remits").glob("*.835"):
        for remit in parse_835(path.read_text()):
            for claim in remit.claims:
                row = truth["lines"][claim.lines[0].line_id]
                results = price_claim(claim.lines, contracts[row["contract_id"]], fees)
                for line, result in zip(claim.lines, results, strict=True):
                    expected = truth["lines"][line.line_id]["expected_allowed"]
                    assert str(result.expected_allowed) == expected


def test_label_signs(smoke: Path):
    for row in json.loads((smoke / "truth.json").read_text())["lines"].values():
        variance = D(row["variance"])
        assert {"underpaid": variance > 0, "overpaid": variance < 0, "correct": variance == 0}.get(
            row["label"], True
        )


def test_full_set_covers_every_error_type(tmp_path: Path):
    build(SPECS["full"], tmp_path / "full")
    rows = json.loads((tmp_path / "full" / "truth.json").read_text())["lines"].values()
    seen = {row["planted_error"] for row in rows}
    assert seen >= {"stale_rate", "wrong_pct", "fee_row_ignored", "mpr_misapplied", "modifier_ignored",
                    "invalid_timely_denial", "valid_timely_denial", "overpaid", "none"}  # fmt: skip


def test_npi_check_digit():
    assert npi_with_check_digit("123456789") == "1234567893"  # the CMS worked example


def test_number_words():
    assert number_words(115) == "one hundred fifteen"
    assert number_words(90) == "ninety"
    assert number_words(365) == "three hundred sixty-five"
