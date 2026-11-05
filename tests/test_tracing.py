from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from recoup import obs
from recoup.audit.pipeline import audit_folder
from recoup.ingest.pipeline import ingest_pdf
from recoup.synth.datasets import SPECS, build


def test_every_graph_node_is_a_span(tmp_path: Path):
    exporter = InMemorySpanExporter()
    obs.configure(exporter)
    build(SPECS["smoke"], tmp_path / "smoke")
    contracts = [
        ingest_pdf(p.read_bytes(), p.name) for p in sorted((tmp_path / "smoke" / "contracts").glob("*.pdf"))
    ]
    audit_folder(tmp_path / "smoke", contracts)
    names = {span.name for span in exporter.get_finished_spans()}
    assert {"ingest.load_spans", "ingest.extract", "ingest.ground", "ingest.consistency", "ingest.assemble",
            "audit.parse_835", "audit.price_and_classify"} <= names  # fmt: skip
    costs = {span.attributes.get("recoup.llm.cost_usd") for span in exporter.get_finished_spans()}
    assert costs == {0.0}  # the default extractor never calls a paid model
