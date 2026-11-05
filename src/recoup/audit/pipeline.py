"""The audit graph: 835 text -> remittances -> findings -> summary."""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from recoup.audit.findings import Finding, Kind, Route, audit
from recoup.config import DEMO_TENANT, Tenant
from recoup.core.fees import FeeTable, default_fee_table
from recoup.core.models import Claim, Remittance, ServiceLine
from recoup.core.money import ZERO
from recoup.ingest.assemble import IngestedContract
from recoup.obs import traced
from recoup.x12.x835 import parse_835


def load_claims_csv(text: str) -> dict[str, Claim]:
    """The practice-management export: one row per service line, grouped back into claims."""
    rows: dict[str, list[dict[str, str]]] = {}
    for row in csv.DictReader(text.splitlines()):
        rows.setdefault(row["claim_id"], []).append(row)
    claims = {}
    for claim_id, lines in rows.items():
        first = lines[0]
        claims[claim_id] = Claim(
            claim_id=claim_id,
            payer_id=first["payer_id"],
            provider_npi=first["provider_npi"],
            submitted_date=date.fromisoformat(first["submitted_date"]),
            lines=tuple(
                ServiceLine(
                    line_id=r["line_id"],
                    code=r["code"],
                    modifiers=tuple(m for m in r["modifiers"].split(";") if m),
                    units=Decimal(r["units"]),
                    billed=Decimal(r["billed"]),
                    service_date=date.fromisoformat(r["service_date"]),
                )
                for r in lines
            ),
        )
    return claims


class AuditSummary(BaseModel):
    lines: int
    by_kind: dict[str, int]
    routed_to_review: int
    recoverable_auto: Decimal
    """Dollars on findings that can go straight to an appeal draft."""
    recoverable_review: Decimal
    """Dollars waiting on a person."""


def summarize(findings: Iterable[Finding]) -> AuditSummary:
    items = list(findings)
    kinds = Counter(f.kind.value for f in items)
    actionable = [f for f in items if f.kind in (Kind.UNDERPAID, Kind.DENIAL_INVALID)]
    return AuditSummary(
        lines=len(items),
        by_kind=dict(sorted(kinds.items())),
        routed_to_review=sum(f.route is Route.REVIEW for f in items),
        recoverable_auto=sum((f.recoverable for f in actionable if f.route is Route.AUTO), ZERO),
        recoverable_review=sum((f.recoverable for f in actionable if f.route is Route.REVIEW), ZERO),
    )


class AuditState(TypedDict, total=False):
    remit_texts: list[str]
    claims_csv: str
    remits: list[Remittance]
    claims: dict[str, Claim]
    findings: list[Finding]
    summary: AuditSummary


def build_audit_graph(contracts: list[IngestedContract], tenant: Tenant, fees: FeeTable):  # type: ignore[no-untyped-def]
    @traced("audit.parse_835")
    def parse(state: AuditState) -> AuditState:
        return {"remits": [r for text in state["remit_texts"] for r in parse_835(text)]}

    @traced("audit.load_claims")
    def claims(state: AuditState) -> AuditState:
        return {"claims": load_claims_csv(state["claims_csv"]) if state.get("claims_csv") else {}}

    @traced("audit.price_and_classify")
    def classify(state: AuditState) -> AuditState:
        return {"findings": audit(state["remits"], contracts, state["claims"], fees, tenant.policy)}

    @traced("audit.summarize")
    def summary(state: AuditState) -> AuditState:
        return {"summary": summarize(state["findings"])}

    graph = StateGraph(AuditState)
    for name, node in (
        ("parse_835", parse),
        ("load_claims", claims),
        ("classify", classify),
        ("summarize", summary),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "parse_835")
    graph.add_edge("parse_835", "load_claims")
    graph.add_edge("load_claims", "classify")
    graph.add_edge("classify", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()


def run_audit(remit_texts: list[str], claims_csv: str, contracts: list[IngestedContract],
              tenant: Tenant = DEMO_TENANT, fees: FeeTable | None = None) -> tuple[list[Finding], AuditSummary]:  # fmt: skip
    graph = build_audit_graph(contracts, tenant, fees or default_fee_table())
    state = graph.invoke({"remit_texts": remit_texts, "claims_csv": claims_csv})
    return state["findings"], state["summary"]


def audit_folder(folder: Path, contracts: list[IngestedContract], tenant: Tenant = DEMO_TENANT
                 ) -> tuple[list[Finding], AuditSummary]:  # fmt: skip
    """A dataset-shaped folder: remits/*.835 and claims.csv."""
    texts = [p.read_text() for p in sorted((folder / "remits").glob("*.835"))]
    claims_path = folder / "claims.csv"
    return run_audit(texts, claims_path.read_text() if claims_path.exists() else "", contracts, tenant)
