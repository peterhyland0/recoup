"""Mock clearinghouse, payer portal and PMS in one FastAPI app (`recoup mocks`, port 8001).

Each one misbehaves the way production systems do, deterministically, so the client code
that copes with it is tested rather than hoped for:
  clearinghouse   every 3rd request gets 429 with Retry-After; every 5th gets 503
  payer portal    requires Idempotency-Key; a replayed key returns the first response;
                  decisions arrive later as HMAC-signed webhooks
  PMS             OAuth2 client credentials; tokens expire; a bad token is a 401
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request, Response

from recoup.integrations.clients import sign


class MockState:
    def __init__(self, era_dir: Path | None = None, webhook_secret: str = "whsec-dev",
                 pms_client: tuple[str, str] = ("recoup", "pms-secret"), token_ttl: int = 3600,
                 flaky: bool = True, callback_url: str | None = None):  # fmt: skip
        self.era_dir = era_dir
        self.webhook_secret = webhook_secret
        self.pms_client = pms_client
        self.token_ttl = token_ttl
        self.flaky = flaky
        self.callback_url = callback_url
        self.requests = 0
        self.appeals: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, float] = {}
        self.notes: dict[str, list[str]] = {}

    def eras(self) -> list[Path]:
        return sorted(self.era_dir.glob("*.835")) if self.era_dir else []


def create_mock_app(state: MockState | None = None) -> FastAPI:
    state = state or MockState()
    app = FastAPI(title="Recoup mock external systems")
    app.state.mock = state

    def chaos() -> None:
        state.requests += 1
        if not state.flaky:
            return
        if state.requests % 3 == 0:
            raise HTTPException(429, "slow down", headers={"Retry-After": "0"})
        if state.requests % 5 == 0:
            raise HTTPException(503, "clearinghouse maintenance window")

    @app.get("/clearinghouse/{account}/eras")
    def list_eras(account: str, since: str | None = None) -> dict[str, Any]:
        chaos()
        eras = [
            {"era_id": p.stem, "size": p.stat().st_size} for p in state.eras() if not since or p.stem > since
        ]
        return {"account": account, "eras": eras}

    @app.get("/clearinghouse/{account}/eras/{era_id}")
    def get_era(account: str, era_id: str) -> Response:
        chaos()
        match = [p for p in state.eras() if p.stem == era_id]
        if not match:
            raise HTTPException(404, "no such ERA")
        return Response(match[0].read_text(), media_type="application/edi-x12")

    @app.post("/payer/appeals")
    async def submit_appeal(request: Request, idempotency_key: str | None = Header(None)) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        if idempotency_key in state.by_key:
            return state.by_key[idempotency_key]
        body = await request.json()
        for field in ("claim_id", "amount", "letter"):
            if field not in body:
                raise HTTPException(422, f"missing {field}")
        appeal_id = f"APL-{len(state.appeals) + 1:05d}"
        record = {"appeal_id": appeal_id, "status": "received", "claim_id": body["claim_id"],
                  "amount": body["amount"], "external_ref": body.get("external_ref")}  # fmt: skip
        state.appeals[appeal_id] = record
        state.by_key[idempotency_key] = record
        return record

    @app.post("/payer/_simulate/{appeal_id}/decide")
    def decide(appeal_id: str, outcome: str = "won") -> dict[str, Any]:
        """Test hook: decide an appeal and emit the signed webhook the real portal would send."""
        record = state.appeals.get(appeal_id)
        if record is None:
            raise HTTPException(404, "no such appeal")
        record["status"] = outcome
        body = json.dumps({"event": "appeal.decided", "appeal_id": appeal_id, "outcome": outcome,
                           "external_ref": record.get("external_ref")}).encode()  # fmt: skip
        timestamp = str(int(time.time()))
        signature = sign(state.webhook_secret, body, timestamp)
        delivered = False
        if state.callback_url:
            try:
                response = httpx.post(state.callback_url, content=body, timeout=5,
                                      headers={"Content-Type": "application/json", "X-Recoup-Timestamp": timestamp,
                                               "X-Recoup-Signature": signature})  # fmt: skip
                delivered = response.is_success
            except httpx.HTTPError:
                delivered = False
        return {"body": body.decode(), "timestamp": timestamp, "signature": signature, "delivered": delivered}

    @app.post("/pms/oauth/token")
    def token(grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(...)
              ) -> dict[str, Any]:  # fmt: skip
        if grant_type != "client_credentials" or (client_id, client_secret) != state.pms_client:
            raise HTTPException(401, "invalid client")
        value = secrets.token_urlsafe(24)
        state.tokens[value] = time.time() + state.token_ttl
        return {"access_token": value, "token_type": "bearer", "expires_in": state.token_ttl}

    @app.post("/pms/claims/{claim_id}/notes")
    async def add_note(claim_id: str, request: Request, authorization: str | None = Header(None),
                       idempotency_key: str | None = Header(None)) -> dict[str, Any]:  # fmt: skip
        bearer = (authorization or "").removeprefix("Bearer ")
        if state.tokens.get(bearer, 0) < time.time():
            raise HTTPException(401, "invalid or expired token")
        text = (await request.json())["text"]
        if idempotency_key and idempotency_key in state.by_key:
            return state.by_key[idempotency_key]
        state.notes.setdefault(claim_id, []).append(text)
        if idempotency_key:
            state.by_key[idempotency_key] = {"claim_id": claim_id, "notes": len(state.notes[claim_id])}
        return {"claim_id": claim_id, "notes": len(state.notes[claim_id])}

    return app
