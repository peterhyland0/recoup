"""Background worker: drains the job queue. Run several; SKIP LOCKED keeps them out of each other's way."""

from __future__ import annotations

import base64
import time
from typing import Any

from recoup.db.queue import Handler, run_one
from recoup.integrations.clients import ClearinghouseClient
from recoup.service import Recoup


def handlers(app: Recoup, clearinghouse: ClearinghouseClient) -> dict[str, Handler]:
    def ingest_contract(tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        pdf = base64.b64decode(payload["pdf_b64"])
        return app.ingest_contract(tenant_id, pdf, payload["filename"], payload.get("actor", "worker"))

    def ingest_remit(tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return app.ingest_remit(tenant_id, payload["text"], payload.get("source", "queue"), "worker")

    def sync(tenant_id: str, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"files": app.sync_clearinghouse(tenant_id, clearinghouse, "worker")}

    return {"ingest_contract": ingest_contract, "ingest_remit": ingest_remit, "sync_clearinghouse": sync}


def run_forever(app: Recoup, clearinghouse: ClearinghouseClient, idle_sleep: float = 1.0) -> None:
    table = handlers(app, clearinghouse)
    while True:
        if not run_one(app.db, table):
            time.sleep(idle_sleep)
