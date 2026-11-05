"""Wiring from environment variables. Everything has a local default so `recoup serve` just works.

DATABASE_URL           sqlite:///~/.recoup/recoup.db (or postgresql+psycopg://...). Absolute by default,
                       so the API, worker and MCP server agree whatever directory they start in.
RECOUP_MOCKS_URL       http://127.0.0.1:8001 (the mock clearinghouse, payer portal and PMS)
RECOUP_API_KEYS        dev-key:demo:demo-biller,svc-key:demo  (key:tenant[:person]; only keys with a
                       person may approve or reject)
RECOUP_WEBHOOK_SECRET  whsec-dev
RECOUP_PMS_CLIENT      recoup:pms-secret
RECOUP_CHECKPOINTS     ~/.recoup/checkpoints.db (where paused appeal graphs wait for approval)
RECOUP_TODAY           unset = the real date; set an ISO date to replay a scenario
RECOUP_TENANT_FILE     unset = the built-in demo tenant
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver

from recoup import obs
from recoup.config import DEMO_TENANT, Tenant, load_tenant
from recoup.db.session import Database
from recoup.integrations.clients import ClearinghouseClient, PayerPortalClient, PMSClient
from recoup.integrations.http import ResilientClient
from recoup.service import Recoup


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def api_keys() -> dict[str, tuple[str, str | None]]:
    """key:tenant[:person], comma-separated. A key with a person can decide; one without cannot."""
    keys: dict[str, tuple[str, str | None]] = {}
    for item in env("RECOUP_API_KEYS", "dev-key:demo:demo-biller,svc-key:demo").split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) >= 2:
            keys[parts[0]] = (parts[1], parts[2] if len(parts) > 2 and parts[2] else None)
    return keys


def today() -> date:
    fixed = os.environ.get("RECOUP_TODAY")
    return date.fromisoformat(fixed) if fixed else date.today()


def tenants() -> dict[str, Tenant]:
    path = os.environ.get("RECOUP_TENANT_FILE")
    tenant = load_tenant(path) if path else DEMO_TENANT
    return {tenant.tenant_id: tenant}


def build(
    http: httpx.Client | None = None, database_url: str | None = None
) -> tuple[Recoup, ClearinghouseClient]:
    client = ResilientClient(http or httpx.Client(base_url=env("RECOUP_MOCKS_URL", "http://127.0.0.1:8001"),
                                                  timeout=10))  # fmt: skip
    pms_id, _, pms_secret = env("RECOUP_PMS_CLIENT", "recoup:pms-secret").partition(":")
    obs.configure()  # exports only when OTEL_EXPORTER_OTLP_ENDPOINT is set
    home = Path(env("RECOUP_HOME", str(Path.home() / ".recoup")))
    home.mkdir(parents=True, exist_ok=True)
    db = Database(database_url or os.environ.get("DATABASE_URL") or f"sqlite:///{home / 'recoup.db'}")
    if not db.is_postgres:
        db.create_all()
    checkpoints = sqlite3.connect(
        env("RECOUP_CHECKPOINTS", str(home / "checkpoints.db")), check_same_thread=False
    )
    service = Recoup(db, tenants(), PayerPortalClient(client), PMSClient(client, pms_id, pms_secret),
                     checkpointer=SqliteSaver(checkpoints), today=today)  # fmt: skip
    return service, ClearinghouseClient(client, account="demo")


def eval_report() -> Path:
    return Path(env("RECOUP_EVAL_REPORT", "evals/latest.json"))
