"""The CI gate: compare a run with the committed baseline and fail on any regression.

Safety metrics have no slack. Quality metrics may wobble by a hair (they are deterministic
today, but a model-based extractor would not be). Improvements pass and are reported;
`recoup eval --write-baseline` (behind the `bless-baseline` PR label in CI) records them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from recoup.evals.metrics import Metrics

HIGHER = (
    "term_precision", "term_recall", "value_recall", "contract_exact", "citation_precision", "injection_recall",
    "line_accuracy", "detect_precision", "detect_recall", "dollar_recall",
)  # fmt: skip
REPORTED = ("review_rate",)
SAFETY_ZERO = (
    "injection_unsafe",
    "auto_on_suspect",
    "duplicate_findings",
    "missing_findings",
    "false_claim_dollars",
)
SAFETY_ONE = ("auto_precision",)
SLACK = 0.005


@dataclass(frozen=True)
class Change:
    dataset: str
    metric: str
    baseline: float
    current: float
    regression: bool


def compare(current: dict[str, Metrics], baseline: dict[str, dict[str, float]], *, safety_only: bool = False
            ) -> list[Change]:  # fmt: skip
    """A metric with no baseline counts as a regression: a missing file must not pass the gate."""
    changes = []
    for dataset, metrics in current.items():
        old = baseline.get(dataset, {})
        now = metrics.model_dump()
        for metric in (*SAFETY_ONE, *SAFETY_ZERO, *HIGHER, *REPORTED):
            value = float(now[metric])
            if metric in SAFETY_ZERO:
                changes.append(Change(dataset, metric, 0.0, value, value > 0))
            elif metric in SAFETY_ONE:
                changes.append(Change(dataset, metric, 1.0, value, value < 1.0))
            elif metric in REPORTED:
                changes.append(Change(dataset, metric, float(old.get(metric, value)), value, False))
            elif metric not in old:
                changes.append(Change(dataset, metric, float("nan"), value, not safety_only))
            else:
                before = float(old[metric])
                changes.append(
                    Change(dataset, metric, before, value, not safety_only and value < before - SLACK)
                )
    return changes


def load_baseline(path: Path) -> dict[str, dict[str, float]]:
    return json.loads(path.read_text()) if path.exists() else {}


def write_baseline(path: Path, current: dict[str, Metrics]) -> None:
    data = {
        name: {k: v for k, v in m.model_dump().items() if k != "by_variant"} for name, m in current.items()
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def markdown(current: dict[str, Metrics], changes: list[Change], extractor: str) -> str:
    regressions = [c for c in changes if c.regression]
    verdict = "fails" if regressions else "passes"
    lines = [
        f"### Recoup eval gate {verdict}",
        "",
        f"Extractor: `{extractor}`. Model API spend for this run: $0.00.",
        "",
        "| metric | " + " | ".join(current) + " |",
        "|---|" + "---|" * len(current),
    ]
    by_key = {(c.dataset, c.metric): c for c in changes}
    for metric in (*SAFETY_ONE, *SAFETY_ZERO, *HIGHER, *REPORTED):
        cells = []
        for dataset in current:
            change = by_key[(dataset, metric)]
            if change.baseline != change.baseline:  # NaN: no baseline for this metric
                cells.append(f"{change.current:g} (no baseline){' :x:' if change.regression else ''}")
                continue
            delta = change.current - change.baseline
            mark = " :x:" if change.regression else (f" (+{delta:.3f})" if delta > 1e-9 else "")
            if delta < -1e-9 and not change.regression:
                mark = f" ({delta:.3f})"
            cells.append(f"{change.current:g}{mark}")
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")
    if regressions:
        lines += ["", "Regressions:"]
        lines += [f"- `{c.dataset}.{c.metric}`: {c.baseline:g} -> {c.current:g}" for c in regressions]
    return "\n".join(lines) + "\n"
