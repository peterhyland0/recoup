from pathlib import Path

import pytest

from recoup.evals.gate import compare, load_baseline, markdown
from recoup.evals.metrics import evaluate
from recoup.ingest.extractors.base import Extraction
from recoup.ingest.extractors.rules import RuleExtractor
from recoup.ingest.spans import Span
from recoup.synth.datasets import SPECS, build

BASELINE = Path(__file__).parents[1] / "evals" / "baseline.json"


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("d") / "smoke"
    build(SPECS["smoke"], out)
    return out


class DropsFeeRows:
    """A plausible regression: someone 'simplifies' the table parser and loses Exhibit A."""

    name = "rules-broken"

    def extract(self, spans: list[Span]) -> Extraction:
        result = RuleExtractor().extract(spans)
        result.values = [v for v in result.values if not v.key.startswith("fee_schedule.")]
        return result


class TrustsInjections:
    """A worse one: the extractor takes numbers from text that tells it what to do."""

    name = "rules-gullible"

    def extract(self, spans: list[Span]) -> Extraction:
        result = RuleExtractor().extract(spans)
        result.injection_span_ids = []
        return result


def test_current_extractor_passes_the_committed_baseline(smoke: Path):
    current = {"smoke": evaluate(smoke, RuleExtractor())}
    changes = compare(current, load_baseline(BASELINE))
    assert not [c for c in changes if c.regression]
    assert "passes" in markdown(current, changes, "rules")


def test_dropping_fee_rows_fails_the_gate(smoke: Path):
    current = {"smoke": evaluate(smoke, DropsFeeRows())}
    regressions = {c.metric for c in compare(current, load_baseline(BASELINE)) if c.regression}
    assert {"term_recall", "contract_exact", "line_accuracy"} <= regressions
    table = markdown(current, compare(current, load_baseline(BASELINE)), "rules-broken")
    assert "fails" in table and ":x:" in table


def test_losing_every_injection_defence_trips_a_safety_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """With both the extractor's flags and the pipeline screen gone, poisoned contracts look ACTIVE
    and their findings go out unreviewed. The routing safety metric must catch that."""
    from recoup.ingest import pipeline

    monkeypatch.setattr(pipeline, "injection_spans", lambda spans: [])
    build(SPECS["adversarial"], tmp_path / "adversarial")
    current = {"adversarial": evaluate(tmp_path / "adversarial", TrustsInjections())}
    regressions = {c.metric for c in compare(current, load_baseline(BASELINE)) if c.regression}
    assert "injection_recall" in regressions


def test_sending_everything_without_review_trips_a_safety_metric(
    smoke: Path, monkeypatch: pytest.MonkeyPatch
):
    from recoup.audit import findings as audit_module

    original = audit_module.audit_claim

    def no_review(*args, **kwargs):  # type: ignore[no-untyped-def]
        return [f.model_copy(update={"route": audit_module.Route.AUTO, "review_reasons": []})
                for f in original(*args, **kwargs)]  # fmt: skip

    monkeypatch.setattr(audit_module, "audit_claim", no_review)
    build(SPECS["adversarial"], smoke.parent / "adv")
    current = {"adversarial": evaluate(smoke.parent / "adv", RuleExtractor())}
    regressions = {c.metric for c in compare(current, load_baseline(BASELINE)) if c.regression}
    assert "auto_on_suspect" in regressions


def test_a_missing_baseline_does_not_pass(smoke: Path, tmp_path: Path):
    current = {"smoke": evaluate(smoke, RuleExtractor())}
    assert any(c.regression for c in compare(current, load_baseline(tmp_path / "nope.json")))
    assert not any(c.regression for c in compare(current, {}, safety_only=True))


def test_duplicate_or_missing_findings_are_safety_failures(smoke: Path):
    import json

    from recoup.audit.pipeline import audit_folder
    from recoup.evals.metrics import score
    from recoup.ingest.pipeline import ingest_pdf

    truth = json.loads((smoke / "truth.json").read_text())
    contracts = [ingest_pdf(p.read_bytes(), p.name) for p in sorted((smoke / "contracts").glob("*.pdf"))]
    findings, _ = audit_folder(smoke, contracts)
    doubled = score(truth, contracts, findings + findings[:3])
    assert doubled.duplicate_findings == 3 and doubled.detect_recall <= 1
    dropped = score(truth, contracts, findings[5:])
    assert dropped.missing_findings == 5 and dropped.line_accuracy < 1
