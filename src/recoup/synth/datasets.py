"""Named, seeded datasets. Same name, same bytes."""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from recoup.core.fees import FeeTable, default_fee_table
from recoup.synth.contracts import make_contract
from recoup.synth.parties import PAYERS, make_practice
from recoup.synth.pdf import to_pdf
from recoup.synth.remits import LineTruth, make_batch
from recoup.synth.render import LineKind, Pack, Variant, render_contract
from recoup.x12.x835 import write_835

GENERATOR_VERSION = "1"


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    seed: int
    variants: tuple[tuple[Variant, int], ...]
    pack: Pack
    claims_per_contract: int
    purpose: str


SPECS = {
    spec.name: spec
    for spec in (
        DatasetSpec("smoke", 1, ((Variant.CLEAN, 2), (Variant.MESSY, 1), (Variant.SCANNED, 1)), Pack.A, 8,
                    "fast CI check on every push"),
        DatasetSpec("full", 2, ((Variant.CLEAN, 6), (Variant.MESSY, 6), (Variant.SCANNED, 4),
                                (Variant.ADVERSARIAL, 4)), Pack.A, 14, "the main development set"),
        DatasetSpec("adversarial", 3, ((Variant.ADVERSARIAL, 8),), Pack.A, 8,
                    "prompt injection with poisoned numbers"),
        DatasetSpec("holdout", 4, ((Variant.CLEAN, 3), (Variant.MESSY, 4), (Variant.SCANNED, 3)), Pack.B, 10,
                    "wording the extractor was never tuned on"),
    )
}  # fmt: skip


def _truth_row(truth: LineTruth) -> dict[str, str]:
    row = asdict(truth)
    return {key: "" if value is None else str(value) for key, value in row.items()}


def build(spec: DatasetSpec, out: Path, fees: FeeTable | None = None) -> dict[str, object]:
    fees = fees or default_fee_table()
    for folder in ("contracts", "remits"):
        if (out / folder).exists():
            shutil.rmtree(out / folder)  # stale files from an older run would poison the eval
        (out / folder).mkdir(parents=True)
    contracts: dict[str, object] = {}
    lines: dict[str, dict[str, str]] = {}
    claim_rows: list[dict[str, str]] = []
    index = 0
    for variant, count in spec.variants:
        for _ in range(count):
            index += 1
            rng = random.Random(f"{spec.seed}:{spec.name}:{index}")
            payer = PAYERS[(index - 1) % len(PAYERS)]
            practice = make_practice(rng)
            contract = make_contract(rng, fees, payer, practice, index)
            doc = render_contract(contract, practice.name, fees, variant, spec.pack, rng)
            pdf_path = f"contracts/{contract.contract_id}.pdf"
            (out / pdf_path).write_bytes(to_pdf(doc, contract.contract_id))
            batch = make_batch(rng, contract, payer, practice, fees, spec.claims_per_contract)
            remit_paths = []
            for number, remit in enumerate(batch.remits, start=1):
                path = f"remits/{contract.contract_id}-{remit.payment_date:%Y%m%d}.835"
                (out / path).write_text(write_835(remit, control=index * 1000 + number))
                remit_paths.append(path)
            for claim in batch.claims:
                for line in claim.lines:
                    claim_rows.append({
                        "claim_id": claim.claim_id, "payer_id": claim.payer_id,
                        "provider_npi": claim.provider_npi, "submitted_date": claim.submitted_date.isoformat(),
                        "line_id": line.line_id, "code": line.code, "modifiers": ";".join(line.modifiers),
                        "units": str(line.units), "billed": str(line.billed),
                        "service_date": line.service_date.isoformat(),
                    })  # fmt: skip
            for line_truth in batch.truths:
                lines[line_truth.line_id] = _truth_row(line_truth)
            contracts[contract.contract_id] = {
                "variant": variant.value,
                "pack": spec.pack.value,
                "practice": practice.name,
                "pdf": pdf_path,
                "remits": remit_paths,
                "contract": contract.model_dump(mode="json"),
                "citations": {k: [list(x) for x in v] for k, v in sorted(doc.citations().items())},
                "injection_lines": [list(x) for x in doc.lines_of_kind(LineKind.INJECTION)],
                "decoy_lines": [list(x) for x in doc.lines_of_kind(LineKind.DECOY)],
            }
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(claim_rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(claim_rows)
    (out / "claims.csv").write_text(buffer.getvalue())
    labels: dict[str, int] = {}
    for row in lines.values():
        labels[row["label"]] = labels.get(row["label"], 0) + 1
    manifest = {
        "dataset": spec.name,
        "seed": spec.seed,
        "generator_version": GENERATOR_VERSION,
        "purpose": spec.purpose,
        "contracts": len(contracts),
        "claim_lines": len(lines),
        "labels": dict(sorted(labels.items())),
    }
    truth = {"manifest": manifest, "contracts": contracts, "lines": lines}
    (out / "truth.json").write_text(json.dumps(truth, indent=2, sort_keys=True) + "\n")
    return manifest
