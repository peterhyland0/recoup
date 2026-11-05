"""Appeal drafting and the wait for a person.

draft (letter + policy check) -+-> blocked -> finalize
                               +-> hold -> approve (interrupt: waits for a human) -> finalize

The graph owns the slow, resumable part: it checkpoints after every node, so an approval
that arrives days later, in another process, resumes where it stopped. The irreversible
part, sending to the payer, is deliberately not a graph node. It is `Recoup.submit`, run
only after `Recoup.decide_appeal` has moved the appeal row from awaiting_approval to
approved in one guarded UPDATE. A second decision, a late rejection, or a replay after a
crash finds the row already moved and stops. The appeal id is the idempotency key, so a
retry after a lost response cannot file twice.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from recoup.audit.findings import ACTIONABLE, Finding
from recoup.obs import traced

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from recoup.service import Recoup


class AppealState(TypedDict, total=False):
    appeal_id: str
    tenant_id: str
    status: str
    letter: str
    amount: str
    issues: list[str]


def policy_issues(findings: list[Finding], approved_reviews: set[str], today: date, min_amount: Decimal,
                  threshold: float) -> list[str]:  # fmt: skip
    """Why this appeal must not go out. Empty means it may be sent once a person approves it."""
    issues = []
    total = sum((f.recoverable for f in findings), Decimal(0))
    if total < min_amount:
        issues.append(f"total ${total:.2f} is below the ${min_amount:.2f} minimum worth filing")
    for f in findings:
        if f.kind not in ACTIONABLE:
            issues.append(f"{f.line_id}: a {f.kind.value} finding is not appealable")
        if f.appeal_deadline is None:
            issues.append(f"{f.line_id}: the contract states no appeal window")
        elif f.appeal_deadline < today:
            issues.append(f"{f.line_id}: appeal window closed on {f.appeal_deadline}")
        if f.review_reasons and f.finding_id not in approved_reviews:
            issues.append(f"{f.line_id}: needs a reviewer's approval first ({f.review_reasons[0]})")
        weak = [e.key for e in f.evidence if not e.grounded or e.confidence < threshold]
        if weak and f.finding_id not in approved_reviews:
            issues.append(f"{f.line_id}: rests on unverified terms {', '.join(weak)}")
    if len({f.claim_id for f in findings}) > 1:
        issues.append("one appeal covers one claim")
    return issues


def build_appeal_graph(app: Recoup, checkpointer: BaseCheckpointSaver[Any], today: Callable[[], date]):  # type: ignore[no-untyped-def]
    @traced("appeal.draft_and_policy")
    def draft(state: AppealState) -> AppealState:
        letter, issues, amount = app.draft_and_check(state["tenant_id"], state["appeal_id"])
        return {"letter": letter, "issues": issues, "amount": str(amount),
                "status": "blocked" if issues else "awaiting_approval"}  # fmt: skip

    @traced("appeal.hold")
    def hold(state: AppealState) -> AppealState:
        app.save_appeal(state["tenant_id"], state["appeal_id"], status="awaiting_approval", letter=state["letter"],
                        issues=state["issues"])  # fmt: skip
        return {}

    def approve(state: AppealState) -> AppealState:
        # The resume payload is only a wake-up. What was decided, and by whom, is read from the
        # appeal row, where `Recoup.decide_appeal` wrote it under a guarded transition.
        interrupt({"appeal_id": state["appeal_id"], "amount": state["amount"], "letter": state["letter"]})
        return {"status": app.get_appeal(state["tenant_id"], state["appeal_id"])["status"]}

    @traced("appeal.finalize")
    def finalize(state: AppealState) -> AppealState:
        if state["status"] == "blocked":
            app.save_appeal(state["tenant_id"], state["appeal_id"], status="blocked", letter=state["letter"],
                            issues=state["issues"])  # fmt: skip
        return {}

    graph = StateGraph(AppealState)
    for name, node in (("draft", draft), ("hold", hold), ("approve", approve), ("finalize", finalize)):
        graph.add_node(name, node)
    graph.add_edge(START, "draft")
    graph.add_conditional_edges("draft", lambda s: "finalize" if s["status"] == "blocked" else "hold")
    graph.add_edge("hold", "approve")
    graph.add_edge("approve", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
