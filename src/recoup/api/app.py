"""HTTP API. Thin: authentication, input shapes and status codes; the work is in `recoup.service`."""

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pypdf.errors import PdfReadError

from recoup.db.session import Database
from recoup.integrations.clients import ClearinghouseClient, verify_signature
from recoup.service import Conflict, NotFound, Recoup
from recoup.x12.x835 import X12ParseError


class Actor(BaseModel):
    tenant_id: str
    user: str
    person: bool
    """True for a key issued to a named person. Service keys can read and draft, never decide."""


class ReviewDecision(BaseModel):
    approve: bool
    note: str = ""


class AppealRequest(BaseModel):
    finding_ids: list[str]


class AppealDecision(BaseModel):
    approve: bool


def create_app(app_service: Recoup, api_keys: dict[str, tuple[str, str | None]], webhook_secret: str,
               clearinghouse: ClearinghouseClient | None = None, eval_report: Path | None = None,
               cors_origins: list[str] | None = None) -> FastAPI:  # fmt: skip
    """`api_keys` maps a key to (tenant, person). The person comes from the key, never from a header,
    so `approved_by` names whoever holds the credential. A real deployment puts SSO here."""
    for tenant, _ in api_keys.values():
        if tenant == Database.SYSTEM:
            raise ValueError("no API key may be bound to the system tenant")
    api = FastAPI(
        title="Recoup", version="0.1.0", description="Payer underpayment audits with cited contract terms."
    )
    api.add_middleware(CORSMiddleware, allow_origins=cors_origins or ["http://localhost:3000"],
                       allow_methods=["*"], allow_headers=["*"])  # fmt: skip

    def actor(x_api_key: Annotated[str | None, Header()] = None) -> Actor:
        entry = api_keys.get(x_api_key or "")
        if entry is None:
            raise HTTPException(401, "missing or unknown X-API-Key")
        tenant, person = entry
        return Actor(
            tenant_id=tenant, user=person or f"service:{(x_api_key or '')[:4]}", person=person is not None
        )

    def person(who: Annotated[Actor, Depends(actor)]) -> Actor:
        if not who.person:
            raise HTTPException(403, "decisions need a key issued to a named person")
        return who

    Who = Annotated[Actor, Depends(actor)]
    Person = Annotated[Actor, Depends(person)]

    @api.exception_handler(NotFound)
    def not_found(_request: Request, error: NotFound) -> Response:
        return Response(json.dumps({"detail": f"not found: {error}"}), 404, media_type="application/json")

    @api.exception_handler(Conflict)
    def conflict(_request: Request, error: Conflict) -> Response:
        return Response(json.dumps({"detail": str(error)}), 409, media_type="application/json")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/me")
    def me(who: Who) -> dict[str, Any]:
        """Who this key belongs to, so the UI names the person the server will record."""
        return {"tenant_id": who.tenant_id, "user": who.user, "can_decide": who.person}

    @api.get("/summary")
    def summary(who: Who) -> dict[str, Any]:
        return app_service.summary(who.tenant_id)

    @api.post("/contracts", status_code=201)
    async def upload_contract(who: Who, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        data = await file.read()
        if not data.startswith(b"%PDF"):
            raise HTTPException(422, "expected a PDF")
        try:
            return app_service.ingest_contract(who.tenant_id, data, file.filename or "contract.pdf", who.user)
        except PdfReadError as error:
            raise HTTPException(422, f"unreadable PDF: {error}") from None

    @api.get("/contracts")
    def contracts(who: Who) -> list[dict[str, Any]]:
        return app_service.list_documents(who.tenant_id)

    @api.get("/contracts/{document_id}")
    def contract(who: Who, document_id: str) -> dict[str, Any]:
        return app_service.get_document(who.tenant_id, document_id)

    @api.get("/contracts/{document_id}/pdf")
    def contract_pdf(who: Who, document_id: str) -> Response:
        return Response(app_service.document_pdf(who.tenant_id, document_id), media_type="application/pdf")

    @api.post("/claims", status_code=201)
    async def upload_claims(who: Who, file: Annotated[UploadFile, File()]) -> dict[str, int]:
        try:
            text = (await file.read()).decode("utf-8")
            return {"claims": app_service.load_claims(who.tenant_id, text, who.user)}
        except (UnicodeDecodeError, KeyError, ValueError) as error:
            raise HTTPException(422, f"claims CSV not understood: {error}") from None

    @api.post("/remits", status_code=201)
    async def upload_remit(who: Who, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        text = (await file.read()).decode("utf-8", errors="replace")
        try:
            return app_service.ingest_remit(who.tenant_id, text, f"upload:{file.filename}", who.user)
        except X12ParseError as error:
            raise HTTPException(422, f"not a valid 835: {error}") from None

    @api.post("/sync/clearinghouse")
    def sync(who: Who) -> list[dict[str, Any]]:
        if clearinghouse is None:
            raise HTTPException(503, "no clearinghouse configured")
        return app_service.sync_clearinghouse(who.tenant_id, clearinghouse, who.user)

    @api.get("/findings")
    def findings(who: Who, kind: str | None = None, route: str | None = None, status: str | None = None,
                 limit: Annotated[int, Query(ge=1, le=2000)] = 500) -> list[dict[str, Any]]:  # fmt: skip
        return app_service.list_findings(who.tenant_id, kind, route, status, limit)

    @api.get("/findings/{finding_id}")
    def finding(who: Who, finding_id: str) -> dict[str, Any]:
        return app_service.get_finding(who.tenant_id, finding_id)

    @api.get("/review-tasks")
    def review_tasks(who: Who, status: str = "open") -> list[dict[str, Any]]:
        return app_service.list_review_tasks(who.tenant_id, status)

    @api.post("/review-tasks/{task_id}/decision")
    def decide_review(who: Person, task_id: str, body: ReviewDecision) -> dict[str, Any]:
        return app_service.decide_review(who.tenant_id, task_id, body.approve, who.user, body.note)

    @api.post("/appeals", status_code=201)
    def start_appeal(who: Who, body: AppealRequest) -> dict[str, Any]:
        return app_service.start_appeal(who.tenant_id, body.finding_ids, who.user)

    @api.get("/appeals")
    def appeals(who: Who) -> list[dict[str, Any]]:
        return app_service.list_appeals(who.tenant_id)

    @api.get("/appeals/{appeal_id}")
    def appeal(who: Who, appeal_id: str) -> dict[str, Any]:
        return app_service.get_appeal(who.tenant_id, appeal_id)

    @api.post("/appeals/{appeal_id}/decision")
    def decide_appeal(who: Person, appeal_id: str, body: AppealDecision) -> dict[str, Any]:
        return app_service.decide_appeal(who.tenant_id, appeal_id, body.approve, who.user)

    @api.post("/appeals/{appeal_id}/retry")
    def retry_appeal(who: Person, appeal_id: str) -> dict[str, Any]:
        """Re-send an approved appeal whose submission failed. The payer dedupes on the appeal id."""
        return app_service.retry_submission(who.tenant_id, appeal_id, who.user)

    @api.post("/appeals/{appeal_id}/withdraw")
    def withdraw_appeal(who: Person, appeal_id: str) -> dict[str, Any]:
        return app_service.withdraw_appeal(who.tenant_id, appeal_id, who.user)

    @api.post("/webhooks/payer")
    async def payer_webhook(request: Request, x_recoup_timestamp: Annotated[str, Header()],
                            x_recoup_signature: Annotated[str, Header()]) -> dict[str, Any]:  # fmt: skip
        body = await request.body()
        if not verify_signature(webhook_secret, body, x_recoup_timestamp, x_recoup_signature):
            raise HTTPException(401, "bad signature")
        try:
            event = json.loads(body)
        except ValueError:
            raise HTTPException(422, "body is not JSON") from None
        return app_service.record_payer_event(event)

    @api.get("/evals/latest")
    def latest_eval(who: Who) -> dict[str, Any]:
        if eval_report is None or not eval_report.exists():
            raise HTTPException(404, "no eval report; run `recoup eval --report evals/latest.json`")
        data: dict[str, Any] = json.loads(eval_report.read_text())
        return data

    return api
