"""The ingest graph: PDF -> spans -> extraction -> grounding -> consistency -> contract.

Fixed order, no model deciding the next step. LangGraph earns its place here by giving
every node a trace span and a typed state, and by sharing one runner with the audit and
appeal graphs (the appeal graph is the one that pauses for a human).
"""

from __future__ import annotations

import hashlib
import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from recoup.config import DEMO_TENANT, Tenant
from recoup.core.fees import FeeTable, default_fee_table
from recoup.ingest.assemble import IngestedContract, IngestStatus, assemble
from recoup.ingest.extractors.base import Extraction, Extractor
from recoup.ingest.extractors.rules import RuleExtractor
from recoup.ingest.guards import injection_spans
from recoup.ingest.spans import Span, spans_from_pdf
from recoup.ingest.validate import Evidence, check_consistency, ground
from recoup.obs import traced


def get_extractor(name: str | None = None) -> Extractor:
    """`rules` by default. `claude` only when asked for and a key is present: no surprise spend."""
    name = name or os.environ.get("RECOUP_EXTRACTOR", "rules")
    if name == "rules":
        return RuleExtractor()
    if name == "claude":
        from recoup.ingest.extractors.claude import ClaudeExtractor

        return ClaudeExtractor.from_env()
    raise ValueError(f"unknown extractor {name!r}")


class IngestState(TypedDict, total=False):
    pdf: bytes
    filename: str
    spans: list[Span]
    extraction: Extraction
    evidence: list[Evidence]
    result: IngestedContract


def build_ingest_graph(extractor: Extractor, tenant: Tenant, fees: FeeTable):  # type: ignore[no-untyped-def]
    @traced("ingest.load_spans")
    def load_spans(state: IngestState) -> IngestState:
        return {"spans": spans_from_pdf(state["pdf"])}

    @traced("ingest.extract")
    def extract(state: IngestState) -> IngestState:
        return {"extraction": extractor.extract(state["spans"])}

    @traced("ingest.ground")
    def ground_check(state: IngestState) -> IngestState:
        # The injection screen runs on every document, whichever extractor read it.
        extraction = state["extraction"]
        screened = list(dict.fromkeys([*extraction.injection_span_ids, *injection_spans(state["spans"])]))
        extraction = extraction.model_copy(update={"injection_span_ids": screened})
        return {"extraction": extraction, "evidence": ground(extraction, state["spans"])}

    @traced("ingest.consistency")
    def consistency(state: IngestState) -> IngestState:
        check_consistency(state["evidence"], fees)
        return {"evidence": state["evidence"]}

    @traced("ingest.assemble")
    def build(state: IngestState) -> IngestState:
        extraction = state["extraction"]
        contract, status, issues = assemble(state["evidence"], tenant)
        if extraction.warnings and status is IngestStatus.ACTIVE:
            status = IngestStatus.NEEDS_REVIEW
        if extraction.injection_span_ids:
            issues.append(
                f"text addressed to automated readers at {', '.join(extraction.injection_span_ids)}"
            )
            if status is IngestStatus.ACTIVE:
                status = IngestStatus.NEEDS_REVIEW
        result = IngestedContract(
            document_id=hashlib.sha256(state["pdf"]).hexdigest(),
            filename=state.get("filename", ""),
            extractor=extraction.extractor,
            status=status,
            contract=contract,
            evidence=state["evidence"],
            injection_span_ids=extraction.injection_span_ids,
            issues=[*extraction.warnings, *issues],
        )
        return {"result": result}

    graph = StateGraph(IngestState)
    for name, node in (("load_spans", load_spans), ("extract", extract), ("ground", ground_check),
                       ("consistency", consistency), ("assemble", build)):  # fmt: skip
        graph.add_node(name, node)
    graph.add_edge(START, "load_spans")
    graph.add_edge("load_spans", "extract")
    graph.add_edge("extract", "ground")
    graph.add_edge("ground", "consistency")
    graph.add_edge("consistency", "assemble")
    graph.add_edge("assemble", END)
    return graph.compile()


def ingest_pdf(pdf: bytes, filename: str = "", extractor: Extractor | None = None,
               tenant: Tenant = DEMO_TENANT, fees: FeeTable | None = None) -> IngestedContract:  # fmt: skip
    graph = build_ingest_graph(extractor or get_extractor(), tenant, fees or default_fee_table())
    state = graph.invoke({"pdf": pdf, "filename": filename})
    result: IngestedContract = state["result"]
    return result
