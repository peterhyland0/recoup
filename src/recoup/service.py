"""Application services. The API, the worker and the MCP server all call these, never the tables directly.

Every method takes a tenant_id and filters on it. Every state change writes an audit_log row.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from recoup.appeals.graph import build_appeal_graph, policy_issues
from recoup.appeals.letter import draft_letter
from recoup.audit.findings import ACTIONABLE, Finding, Route, audit
from recoup.audit.pipeline import load_claims_csv
from recoup.config import Tenant
from recoup.core.fees import FeeTable, default_fee_table
from recoup.core.models import Claim
from recoup.db.models import (
    AppealRecord,
    AuditLog,
    ClaimRecord,
    Document,
    FindingRecord,
    RemitFile,
    ReviewTask,
    now,
)
from recoup.db.session import Database
from recoup.ingest.assemble import IngestedContract
from recoup.ingest.extractors.base import Extractor
from recoup.ingest.pipeline import get_extractor, ingest_pdf
from recoup.ingest.spans import Span, spans_from_pdf
from recoup.integrations.clients import ClearinghouseClient, PayerPortalClient, PMSClient
from recoup.x12.x835 import parse_835


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


@dataclass
class AppealBundle:
    findings: list[Finding]
    quotes: dict[str, str]
    approved_reviews: set[str]
    payer_name: str
    provider_name: str
    npi: str
    window: int | None


def _rows(result: Any) -> int:
    """Rows touched by an UPDATE (a CursorResult; typed loosely by SQLAlchemy's stubs)."""
    return int(result.rowcount)


def _finding(record: FindingRecord) -> Finding:
    return Finding.model_validate(record.data)


class Recoup:
    def __init__(self, db: Database, tenants: dict[str, Tenant], portal: PayerPortalClient, pms: PMSClient,
                 *, checkpointer: BaseCheckpointSaver[Any] | None = None, today: Callable[[], date] = date.today,
                 extractor: Extractor | None = None, fees: FeeTable | None = None):  # fmt: skip
        self.db = db
        self.tenants = tenants
        self.portal = portal
        self.pms = pms
        self.today = today
        self.extractor = extractor or get_extractor()
        self.fees = fees or default_fee_table()
        self.appeals = build_appeal_graph(self, checkpointer or InMemorySaver(), today)

    def tenant(self, tenant_id: str) -> Tenant:
        if tenant_id not in self.tenants:
            raise NotFound(f"tenant {tenant_id}")
        return self.tenants[tenant_id]

    @staticmethod
    def _log(session: Session, tenant_id: str, actor: str, action: str, subject: str, **detail: Any) -> None:
        session.add(AuditLog(tenant_id=tenant_id, actor=actor, action=action, subject=subject,
                             detail=json.loads(json.dumps(detail, default=str))))  # fmt: skip

    # contracts

    def ingest_contract(self, tenant_id: str, pdf: bytes, filename: str, actor: str) -> dict[str, Any]:
        digest = hashlib.sha256(pdf).hexdigest()
        with self.db.session(tenant_id) as session:
            existing = session.scalars(select(Document).where(Document.tenant_id == tenant_id,
                                                              Document.sha256 == digest)).first()  # fmt: skip
            if existing is not None:
                return self._document_summary(existing)
        result = ingest_pdf(
            pdf, filename, extractor=self.extractor, tenant=self.tenant(tenant_id), fees=self.fees
        )
        spans = spans_from_pdf(pdf)
        with self.db.session(tenant_id) as session:
            document = Document(
                tenant_id=tenant_id, filename=filename, sha256=digest, status=result.status.value,
                contract_id=result.contract.contract_id if result.contract else None,
                payer_id=result.contract.payer_id if result.contract else None, extractor=result.extractor,
                ingested=result.model_dump(mode="json"), spans=[s.model_dump() for s in spans], pdf=pdf,
            )  # fmt: skip
            session.add(document)
            session.flush()
            self._log(session, tenant_id, actor, "contract.ingested", document.id, status=result.status.value,
                      issues=result.issues[:10])  # fmt: skip
            return self._document_summary(document)

    @staticmethod
    def _document_summary(document: Document) -> dict[str, Any]:
        ingested = document.ingested
        return {"id": document.id, "filename": document.filename, "status": document.status,
                "contract_id": document.contract_id, "payer_id": document.payer_id, "extractor": document.extractor,
                "issues": ingested.get("issues", []), "injection_span_ids": ingested.get("injection_span_ids", []),
                "created_at": document.created_at.isoformat()}  # fmt: skip

    def list_documents(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.db.session(tenant_id) as session:
            rows = session.scalars(select(Document).where(Document.tenant_id == tenant_id)
                                   .order_by(Document.contract_id)).all()  # fmt: skip
            return [self._document_summary(d) for d in rows]

    def get_document(self, tenant_id: str, document_id: str) -> dict[str, Any]:
        with self.db.session(tenant_id) as session:
            document = self._document(session, tenant_id, document_id)
            return {
                **self._document_summary(document),
                "ingested": document.ingested,
                "spans": document.spans,
            }

    def document_pdf(self, tenant_id: str, document_id: str) -> bytes:
        with self.db.session(tenant_id) as session:
            return self._document(session, tenant_id, document_id).pdf

    @staticmethod
    def _document(session: Session, tenant_id: str, document_id: str) -> Document:
        document = session.get(Document, document_id)
        if document is None or document.tenant_id != tenant_id:
            raise NotFound(f"document {document_id}")
        return document

    def _contracts(self, session: Session, tenant_id: str) -> list[IngestedContract]:
        rows = session.scalars(select(Document).where(Document.tenant_id == tenant_id)).all()
        out = []
        for row in rows:
            ingested = IngestedContract.model_validate(row.ingested)
            out.append(ingested.model_copy(update={"document_id": row.id}))
        return out

    # claims and remits

    def load_claims(self, tenant_id: str, csv_text: str, actor: str) -> int:
        claims = load_claims_csv(csv_text)
        with self.db.session(tenant_id) as session:
            for claim in claims.values():
                session.merge(ClaimRecord(tenant_id=tenant_id, claim_id=claim.claim_id,
                                          data=claim.model_dump(mode="json")))  # fmt: skip
            self._log(session, tenant_id, actor, "claims.loaded", "claims", count=len(claims))
        return len(claims)

    def ingest_remit(self, tenant_id: str, text: str, source: str, actor: str) -> dict[str, Any]:
        """Parse, audit and store one 835 file. Re-sending the same file is a no-op."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        remits = parse_835(text)
        with self.db.session(tenant_id) as session:
            existing = session.scalars(select(RemitFile).where(RemitFile.tenant_id == tenant_id,
                                                               RemitFile.sha256 == digest)).first()  # fmt: skip
            if existing is not None:
                count = session.scalar(select(func.count()).select_from(FindingRecord)
                                       .where(FindingRecord.remit_file_id == existing.id)) or 0  # fmt: skip
                return {"remit_file_id": existing.id, "findings": count, "duplicate": True}
            contracts = self._contracts(session, tenant_id)
            claim_ids = {c.claim_id for r in remits for c in r.claims}
            claims = {
                row.claim_id: Claim.model_validate(row.data)
                for row in session.scalars(select(ClaimRecord).where(ClaimRecord.tenant_id == tenant_id,
                                                                     ClaimRecord.claim_id.in_(claim_ids)))
            }  # fmt: skip
            findings = audit(remits, contracts, claims, self.fees, self.tenant(tenant_id).policy)
            remit_file = RemitFile(tenant_id=tenant_id, sha256=digest, source=source, text=text)
            session.add(remit_file)
            session.flush()
            stored = 0
            for finding in findings:
                duplicate = session.scalars(select(FindingRecord).where(
                    FindingRecord.tenant_id == tenant_id, FindingRecord.check_number == finding.check_number,
                    FindingRecord.line_id == finding.line_id)).first()  # fmt: skip
                if duplicate is not None:
                    continue
                earlier = session.scalars(select(FindingRecord).where(
                    FindingRecord.tenant_id == tenant_id, FindingRecord.line_id == finding.line_id,
                    FindingRecord.kind.in_([k.value for k in ACTIONABLE]))).first()  # fmt: skip
                record = FindingRecord(
                    tenant_id=tenant_id, remit_file_id=remit_file.id, check_number=finding.check_number,
                    line_id=finding.line_id, claim_id=finding.claim_id, payer_id=remits[0].payer_id,
                    contract_id=finding.contract_id, kind=finding.kind.value, route=finding.route.value,
                    recoverable=finding.recoverable, appeal_deadline=finding.appeal_deadline, data={},
                    # A line already found recoverable on an earlier payment is not money twice.
                    status="duplicate" if earlier is not None and finding.kind in ACTIONABLE else "open",
                )  # fmt: skip
                session.add(record)
                session.flush()
                record.data = finding.model_copy(update={"finding_id": record.id}).model_dump(mode="json")
                if finding.route is Route.REVIEW and record.status != "duplicate":
                    session.add(
                        ReviewTask(tenant_id=tenant_id, finding_id=record.id, reasons=finding.review_reasons)
                    )
                stored += 1
            self._log(
                session, tenant_id, actor, "remit.audited", remit_file.id, source=source, findings=stored
            )
            return {"remit_file_id": remit_file.id, "findings": stored, "duplicate": False}

    def sync_clearinghouse(
        self, tenant_id: str, client: ClearinghouseClient, actor: str
    ) -> list[dict[str, Any]]:
        return [self.ingest_remit(tenant_id, client.download(era["era_id"]), f"clearinghouse:{era['era_id']}", actor)
                for era in client.list_eras()]  # fmt: skip

    # findings and review

    def list_findings(self, tenant_id: str, kind: str | None = None, route: str | None = None,
                      status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:  # fmt: skip
        with self.db.session(tenant_id) as session:
            query = select(FindingRecord).where(FindingRecord.tenant_id == tenant_id)
            if kind:
                query = query.where(FindingRecord.kind == kind)
            if route:
                query = query.where(FindingRecord.route == route)
            if status:
                query = query.where(FindingRecord.status == status)
            rows = session.scalars(query.order_by(FindingRecord.recoverable.desc()).limit(limit)).all()
            return [{**r.data, "status": r.status} for r in rows]

    def get_finding(self, tenant_id: str, finding_id: str) -> dict[str, Any]:
        with self.db.session(tenant_id) as session:
            record = self._finding_record(session, tenant_id, finding_id)
            task = session.scalars(select(ReviewTask).where(ReviewTask.finding_id == finding_id)).first()
            return {**record.data, "status": record.status,
                    "review": None if task is None else {"id": task.id, "status": task.status, "note": task.note,
                                                         "decided_by": task.decided_by}}  # fmt: skip

    @staticmethod
    def _finding_record(session: Session, tenant_id: str, finding_id: str) -> FindingRecord:
        record = session.get(FindingRecord, finding_id)
        if record is None or record.tenant_id != tenant_id:
            raise NotFound(f"finding {finding_id}")
        return record

    def list_review_tasks(self, tenant_id: str, status: str = "open") -> list[dict[str, Any]]:
        with self.db.session(tenant_id) as session:
            rows = session.execute(
                select(ReviewTask, FindingRecord).join(FindingRecord, ReviewTask.finding_id == FindingRecord.id)
                .where(ReviewTask.tenant_id == tenant_id, ReviewTask.status == status)
                .order_by(FindingRecord.recoverable.desc())
            ).all()  # fmt: skip
            return [{"id": task.id, "status": task.status, "reasons": task.reasons, "finding": {**f.data, "status": f.status}}
                    for task, f in rows]  # fmt: skip

    def decide_review(
        self, tenant_id: str, task_id: str, approve: bool, actor: str, note: str = ""
    ) -> dict[str, Any]:
        with self.db.session(tenant_id) as session:
            task = session.get(ReviewTask, task_id)
            if task is None or task.tenant_id != tenant_id:
                raise NotFound(f"review task {task_id}")
            if task.status != "open":
                raise Conflict(f"review task {task_id} is already {task.status}")
            task.status = "approved" if approve else "rejected"
            task.decided_by, task.note, task.decided_at = actor, note, now()
            record = self._finding_record(session, tenant_id, task.finding_id)
            record.status = task.status
            self._log(session, tenant_id, actor, f"review.{task.status}", task.finding_id, note=note)
            return {"id": task.id, "status": task.status, "finding_id": task.finding_id}

    # appeals

    def start_appeal(self, tenant_id: str, finding_ids: list[str], actor: str) -> dict[str, Any]:
        if not finding_ids:
            raise Conflict("an appeal needs at least one finding")
        if len(set(finding_ids)) != len(finding_ids):
            raise Conflict("a finding is listed more than once")
        with self.db.session(tenant_id) as session:
            records = [self._finding_record(session, tenant_id, fid) for fid in finding_ids]
            for record in records:
                if record.kind not in {k.value for k in ACTIONABLE}:
                    raise Conflict(f"{record.id} is {record.kind}, not appealable")
            if len({r.claim_id for r in records}) != 1:
                raise Conflict("one appeal covers one claim")
            # Claim the findings in one guarded UPDATE: of two concurrent drafts, one gets them.
            claimed = _rows(session.execute(
                update(FindingRecord)
                .where(FindingRecord.tenant_id == tenant_id, FindingRecord.id.in_(finding_ids),
                       FindingRecord.status.in_(["open", "approved"]))
                .values(status="appealed")
                .execution_options(synchronize_session=False)
            ))  # fmt: skip
            if claimed != len(finding_ids):
                raise Conflict("a finding is already in an appeal or closed")
            appeal = AppealRecord(tenant_id=tenant_id, claim_id=records[0].claim_id, payer_id=records[0].payer_id,
                                  finding_ids=finding_ids, amount=sum((r.recoverable for r in records), Decimal(0)),
                                  status="drafting")  # fmt: skip
            session.add(appeal)
            session.flush()
            self._log(session, tenant_id, actor, "appeal.started", appeal.id, findings=finding_ids)
            appeal_id = appeal.id
        self.appeals.invoke({"appeal_id": appeal_id, "tenant_id": tenant_id}, self._thread(appeal_id))
        return self.get_appeal(tenant_id, appeal_id)

    def draft_and_check(self, tenant_id: str, appeal_id: str) -> tuple[str, list[str], Decimal]:
        bundle = self.appeal_bundle(tenant_id, appeal_id)
        findings = bundle.findings
        deadline = min((f.appeal_deadline for f in findings if f.appeal_deadline), default=None)
        letter = draft_letter(findings, bundle.quotes, today=self.today(), payer_name=bundle.payer_name,
                              provider_name=bundle.provider_name, npi=bundle.npi, window=bundle.window,
                              deadline=deadline)  # fmt: skip
        policy = self.tenant(tenant_id).policy
        issues = policy_issues(findings, bundle.approved_reviews, self.today(), policy.min_appeal_amount,
                               policy.confidence_threshold)  # fmt: skip
        return letter, issues, sum((f.recoverable for f in findings), Decimal(0))

    def _transition(
        self, tenant_id: str, appeal_id: str, frm: tuple[str, ...], to: str, **values: Any
    ) -> bool:
        """Move an appeal between states in one guarded UPDATE. False if someone else moved it first."""
        with self.db.session(tenant_id) as session:
            moved = _rows(session.execute(
                update(AppealRecord)
                .where(AppealRecord.id == appeal_id, AppealRecord.tenant_id == tenant_id, AppealRecord.status.in_(frm))
                .values(status=to, updated_at=now(), **values)
                .execution_options(synchronize_session=False)
            ))  # fmt: skip
            return bool(moved == 1)

    def _release_findings(self, session: Session, tenant_id: str, appeal: AppealRecord) -> None:
        """Hand an appeal's findings back so they can be appealed again (or rejected in review)."""
        for fid in appeal.finding_ids:
            record = self._finding_record(session, tenant_id, fid)
            if record.status == "appealed":
                record.status = "open"

    def decide_appeal(self, tenant_id: str, appeal_id: str, approve: bool, actor: str) -> dict[str, Any]:
        """A person's decision. Approval re-runs the policy (deadlines move), then sends exactly once."""
        current = self.get_appeal(tenant_id, appeal_id)
        if current["status"] != "awaiting_approval":
            raise Conflict(f"appeal {appeal_id} is {current['status']}, not awaiting approval")
        if approve:
            _, issues, _ = self.draft_and_check(tenant_id, appeal_id)
            if issues:
                if self._transition(
                    tenant_id, appeal_id, ("awaiting_approval",), "blocked", policy_issues=issues
                ):
                    with self.db.session(tenant_id) as session:
                        self._release_findings(
                            session, tenant_id, self._appeal(session, tenant_id, appeal_id)
                        )
                        self._log(
                            session, tenant_id, actor, "appeal.blocked_at_approval", appeal_id, issues=issues
                        )
                raise Conflict(f"appeal {appeal_id} can no longer be sent: {'; '.join(issues)}")
        to = "approved" if approve else "rejected"
        if not self._transition(tenant_id, appeal_id, ("awaiting_approval",), to, approved_by=actor):
            raise Conflict(f"appeal {appeal_id} was decided by someone else first")
        with self.db.session(tenant_id) as session:
            appeal = self._appeal(session, tenant_id, appeal_id)
            if not approve:
                self._release_findings(session, tenant_id, appeal)
            self._log(session, tenant_id, actor, f"appeal.{to}", appeal_id)
        self._close_thread(appeal_id)
        if approve:
            self.submit(tenant_id, appeal_id)
        return self.get_appeal(tenant_id, appeal_id)

    def _close_thread(self, appeal_id: str) -> None:
        """Let the paused graph run to its end. Missing checkpoints are fine: the row is the record."""
        config = self._thread(appeal_id)
        try:
            if "approve" in (self.appeals.get_state(config).next or ()):
                self.appeals.invoke(Command(resume={}), config)
        except Exception:  # a lost or foreign checkpoint must not block the decision already recorded
            return

    def submit(self, tenant_id: str, appeal_id: str) -> dict[str, Any]:
        """Send an approved appeal. Safe to repeat: the portal dedupes on the appeal id."""
        if not self._transition(
            tenant_id, appeal_id, ("approved", "submit_failed", "submitting"), "submitting"
        ):
            raise Conflict(f"appeal {appeal_id} is not approved")
        appeal = self.get_appeal(tenant_id, appeal_id)
        bundle = self.appeal_bundle(tenant_id, appeal_id)
        first = bundle.findings[0]
        payload = {"claim_id": first.claim_id, "payer_claim_id": first.payer_claim_id, "amount": appeal["amount"],
                   "letter": appeal["letter"], "external_ref": appeal_id, "lines": [f.line_id for f in bundle.findings]}  # fmt: skip
        try:
            portal = self.portal.submit_appeal(payload, idempotency_key=f"appeal-{appeal_id}")
        except Exception as error:  # the portal, the network, or a bad response: retryable by a person
            issues = [*appeal["policy_issues"], f"submission failed: {error}"]
            self._transition(tenant_id, appeal_id, ("submitting",), "submit_failed", policy_issues=issues)
            return self.get_appeal(tenant_id, appeal_id)
        settled = [i for i in appeal["policy_issues"] if not i.startswith("submission failed")]
        self._transition(tenant_id, appeal_id, ("submitting",), "submitted", portal_appeal_id=portal.get("appeal_id"),
                         policy_issues=settled)  # fmt: skip
        note = (f"Recoup appeal {appeal_id} filed as {portal.get('appeal_id')} for ${appeal['amount']}, "
                f"approved by {appeal['approved_by']}.")  # fmt: skip
        try:
            self.pms.add_claim_note(first.claim_id, note, idempotency_key=f"note-{appeal_id}")
        except Exception as error:  # the appeal is filed; a missing note is recorded, not fatal
            with self.db.session(tenant_id) as session:
                record = self._appeal(session, tenant_id, appeal_id)
                record.policy_issues = [*record.policy_issues, f"PMS note not written: {error}"]
        with self.db.session(tenant_id) as session:
            self._log(
                session, tenant_id, "system", "appeal.submitted", appeal_id, portal=portal.get("appeal_id")
            )
        return self.get_appeal(tenant_id, appeal_id)

    def retry_submission(self, tenant_id: str, appeal_id: str, actor: str) -> dict[str, Any]:
        with self.db.session(tenant_id) as session:
            self._log(session, tenant_id, actor, "appeal.retry", appeal_id)
        return self.submit(tenant_id, appeal_id)

    def withdraw_appeal(self, tenant_id: str, appeal_id: str, actor: str) -> dict[str, Any]:
        """Give up on an appeal that never reached the payer and free its findings."""
        if not self._transition(
            tenant_id, appeal_id, ("blocked", "submit_failed", "awaiting_approval"), "withdrawn"
        ):
            raise Conflict(f"appeal {appeal_id} cannot be withdrawn now")
        with self.db.session(tenant_id) as session:
            self._release_findings(session, tenant_id, self._appeal(session, tenant_id, appeal_id))
            self._log(session, tenant_id, actor, "appeal.withdrawn", appeal_id)
        self._close_thread(appeal_id)
        return self.get_appeal(tenant_id, appeal_id)

    @staticmethod
    def _thread(appeal_id: str) -> Any:
        return {"configurable": {"thread_id": appeal_id}}

    def get_appeal(self, tenant_id: str, appeal_id: str) -> dict[str, Any]:
        with self.db.session(tenant_id) as session:
            appeal = self._appeal(session, tenant_id, appeal_id)
            return self._appeal_dict(appeal)

    def list_appeals(self, tenant_id: str) -> list[dict[str, Any]]:
        with self.db.session(tenant_id) as session:
            rows = session.scalars(select(AppealRecord).where(AppealRecord.tenant_id == tenant_id)
                                   .order_by(AppealRecord.created_at.desc())).all()  # fmt: skip
            return [self._appeal_dict(a) for a in rows]

    @staticmethod
    def _appeal(session: Session, tenant_id: str, appeal_id: str) -> AppealRecord:
        appeal = session.get(AppealRecord, appeal_id)
        if appeal is None or appeal.tenant_id != tenant_id:
            raise NotFound(f"appeal {appeal_id}")
        return appeal

    @staticmethod
    def _appeal_dict(appeal: AppealRecord) -> dict[str, Any]:
        return {"id": appeal.id, "claim_id": appeal.claim_id, "payer_id": appeal.payer_id,
                "finding_ids": appeal.finding_ids, "amount": str(appeal.amount), "status": appeal.status,
                "letter": appeal.letter, "policy_issues": appeal.policy_issues,
                "portal_appeal_id": appeal.portal_appeal_id, "approved_by": appeal.approved_by,
                "created_at": appeal.created_at.isoformat(), "updated_at": appeal.updated_at.isoformat()}  # fmt: skip

    def appeal_bundle(self, tenant_id: str, appeal_id: str) -> AppealBundle:
        with self.db.session(tenant_id) as session:
            appeal = self._appeal(session, tenant_id, appeal_id)
            records = [self._finding_record(session, tenant_id, fid) for fid in appeal.finding_ids]
            findings = [_finding(r) for r in records]
            approved = {r.id for r in records if session.scalars(select(ReviewTask).where(
                ReviewTask.finding_id == r.id, ReviewTask.status == "approved")).first()}  # fmt: skip
            quotes: dict[str, str] = {}
            ingested: IngestedContract | None = None
            if findings[0].document_id:
                document = session.get(Document, findings[0].document_id)
                if document is not None:
                    wanted = {sid for f in findings for e in f.evidence for sid in e.span_ids}
                    quotes = {s["span_id"]: s["text"] for s in document.spans if s["span_id"] in wanted}
                    ingested = IngestedContract.model_validate(document.ingested)
            contract = ingested.contract if ingested else None
            window = None
            if contract is not None:
                window = contract.resolve(findings[0].service_date).terms.appeal_window_days
            return AppealBundle(
                findings=findings, quotes=quotes, approved_reviews=approved,
                payer_name=contract.payer_name if contract else appeal.payer_id,
                provider_name=self.tenant(tenant_id).name, npi=contract.provider_npi if contract else "",
                window=window,
            )  # fmt: skip

    def save_appeal(
        self, tenant_id: str, appeal_id: str, *, status: str, letter: str, issues: list[str]
    ) -> None:
        """Written by the drafting graph only: drafting -> awaiting_approval or blocked."""
        with self.db.session(tenant_id) as session:
            appeal = self._appeal(session, tenant_id, appeal_id)
            if appeal.status not in ("drafting", status):
                return  # a person already acted on it; the graph does not overrule them
            appeal.status, appeal.letter, appeal.policy_issues = status, letter, issues
            if status == "blocked":
                self._release_findings(session, tenant_id, appeal)
            self._log(session, tenant_id, "system", f"appeal.{status}", appeal_id, issues=issues[:5])

    def record_payer_event(self, event: Any) -> dict[str, Any]:
        """A verified webhook from the payer portal. Only a submitted appeal can be decided, once."""
        if not isinstance(event, dict):
            raise Conflict("payer event must be a JSON object")
        appeal_id, outcome, portal_id = (
            event.get("external_ref"),
            event.get("outcome"),
            event.get("appeal_id"),
        )
        if outcome not in ("won", "lost") or not isinstance(appeal_id, str):
            raise Conflict("unrecognised payer event")
        with self.db.session(
            self.db.SYSTEM
        ) as session:  # the signature is verified; the event names no tenant
            appeal = session.get(AppealRecord, appeal_id)
            if appeal is None:
                raise NotFound(f"appeal {appeal_id}")
            if portal_id != appeal.portal_appeal_id:
                raise Conflict("event does not match the payer reference on file")
            if appeal.status == outcome:
                return {"id": appeal.id, "status": appeal.status, "duplicate": True}
            if appeal.status != "submitted":
                raise Conflict(
                    f"appeal {appeal_id} is {appeal.status}; only a submitted appeal can be decided"
                )
            appeal.status = outcome
            for fid in appeal.finding_ids:
                record = session.get(FindingRecord, fid)
                if record is not None:
                    record.status = outcome
            self._log(session, appeal.tenant_id, "payer-webhook", f"appeal.{outcome}", appeal.id)
            return {"id": appeal.id, "status": outcome, "duplicate": False}

    # reporting

    def summary(self, tenant_id: str) -> dict[str, Any]:
        """Dashboard numbers. Recoverable money is split by what stands between it and an appeal."""
        today = self.today()
        actionable = [k.value for k in ACTIONABLE]
        with self.db.session(tenant_id) as session:
            kinds = session.execute(
                select(FindingRecord.kind, func.count()).where(FindingRecord.tenant_id == tenant_id)
                .group_by(FindingRecord.kind)
            ).all()  # fmt: skip
            money_rows = session.execute(
                select(FindingRecord.id, FindingRecord.status, FindingRecord.route, FindingRecord.recoverable,
                       FindingRecord.appeal_deadline)
                .where(FindingRecord.tenant_id == tenant_id, FindingRecord.kind.in_(actionable))
            ).all()  # fmt: skip
            documents = session.execute(select(Document.status, func.count()).where(Document.tenant_id == tenant_id)
                                        .group_by(Document.status)).all()  # fmt: skip
            open_reviews = session.scalar(select(func.count()).select_from(ReviewTask).where(
                ReviewTask.tenant_id == tenant_id, ReviewTask.status == "open")) or 0  # fmt: skip
        buckets = {k: Decimal(0) for k in ("ready", "in_review", "past_deadline", "appealed", "won", "lost")}
        for _id, status, route, amount, deadline in money_rows:
            value = Decimal(str(amount))
            if status in ("appealed", "won", "lost"):
                buckets[status] += value
            elif status not in ("open", "approved"):
                continue  # rejected in review, or a duplicate of a line already counted
            elif deadline is None or deadline < today:
                buckets["past_deadline"] += value
            elif route == "review" and status != "approved":
                buckets["in_review"] += value
            else:
                buckets["ready"] += value
        return {
            "lines": sum(count for _, count in kinds),
            "by_kind": {kind: count for kind, count in kinds},
            "open_reviews": open_reviews,
            "recoverable": {k: str(v) for k, v in buckets.items()},
            "contracts": {status: count for status, count in documents},
            "today": today.isoformat(),
        }


def span_list(document: dict[str, Any]) -> list[Span]:
    return [Span.model_validate(s) for s in document["spans"]]
