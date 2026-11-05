import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from recoup.api.app import create_app
from recoup.config import DEMO_TENANT
from recoup.db.queue import enqueue, run_one
from recoup.db.session import Database
from recoup.integrations.clients import ClearinghouseClient, PayerPortalClient, PMSClient, sign
from recoup.integrations.http import ResilientClient, UpstreamError
from recoup.mcp_server import create_mcp
from recoup.mocks.app import MockState, create_mock_app
from recoup.service import Recoup
from recoup.synth.datasets import SPECS, build
from recoup.worker import handlers

SECRET = "whsec-test"
KEY = {"X-API-Key": "k1"}
SERVICE_KEY = {"X-API-Key": "svc"}
KEYS = {"k1": ("demo", "biller@practice"), "k2": ("other", "other-biller"), "svc": ("demo", None)}


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("d") / "smoke"
    build(SPECS["smoke"], out)
    return out


class World:
    def __init__(self, root: Path, dataset: Path, today: date):
        self.mock_state = MockState(era_dir=dataset / "remits", webhook_secret=SECRET)
        self.mock_http = TestClient(create_mock_app(self.mock_state))
        self.http = ResilientClient(self.mock_http, sleep=lambda _s: None)
        self.db_url = f"sqlite:///{root / 'recoup.db'}"
        self.checkpoints = root / "checkpoints.db"
        self.today = today
        self.dataset = dataset
        self.start()

    def start(self) -> None:
        """(Re)build the app over the same database and checkpoint file, as after a restart."""
        db = Database(self.db_url)
        db.create_all()
        saver = SqliteSaver(sqlite3.connect(self.checkpoints, check_same_thread=False))
        other = DEMO_TENANT.model_copy(update={"tenant_id": "other", "name": "Other practice"})
        self.service = Recoup(db, {"demo": DEMO_TENANT, "other": other}, PayerPortalClient(self.http),
                              PMSClient(self.http, "recoup", "pms-secret"), checkpointer=saver,
                              today=lambda: self.today)  # fmt: skip
        self.clearinghouse = ClearinghouseClient(self.http, "demo")
        api = create_app(self.service, KEYS, SECRET, self.clearinghouse)
        self.api = TestClient(api)

    def upload(self, path: str, field_path: Path) -> dict:  # type: ignore[type-arg]
        response = self.api.post(
            path, headers=KEY, files={"file": (field_path.name, field_path.read_bytes())}
        )
        assert response.status_code == 201, response.text
        return response.json()


@pytest.fixture
def world(tmp_path: Path, smoke: Path) -> World:
    return World(tmp_path, smoke, date(2026, 3, 1))


def load(world: World, via_clearinghouse: bool = False) -> None:
    for pdf in sorted((world.dataset / "contracts").glob("*.pdf")):
        assert world.upload("/contracts", pdf)["status"] in ("active", "needs_review")
    world.upload("/claims", world.dataset / "claims.csv")
    if via_clearinghouse:
        response = world.api.post("/sync/clearinghouse", headers=KEY)
        assert response.status_code == 200, response.text
    else:
        for remit in sorted((world.dataset / "remits").glob("*.835")):
            world.upload("/remits", remit)


def test_upload_audit_appeal_approve_submit_and_get_paid(world: World):
    load(world)
    truth = json.loads((world.dataset / "truth.json").read_text())["lines"]
    summary = world.api.get("/summary", headers=KEY).json()
    expected_kinds: dict[str, int] = {}
    for row in truth.values():
        expected_kinds[row["label"]] = expected_kinds.get(row["label"], 0) + 1
    assert summary["by_kind"] == expected_kinds

    candidates = world.api.get("/findings", headers=KEY, params={"kind": "underpaid", "route": "auto"}).json()
    finding = min(candidates, key=lambda f: f["payment_date"])
    world.today = date.fromisoformat(finding["payment_date"])
    claim_findings = [f["finding_id"] for f in candidates if f["claim_id"] == finding["claim_id"]]

    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": claim_findings}).json()
    assert appeal["status"] == "awaiting_approval", appeal["policy_issues"]
    assert "Contract, page" in appeal["letter"] and finding["claim_id"] in appeal["letter"]
    assert "—" not in appeal["letter"] and "–" not in appeal["letter"]
    assert world.mock_state.appeals == {}  # nothing leaves before a person approves

    decided = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).json()
    assert decided["status"] == "submitted" and decided["approved_by"] == "biller@practice"
    assert decided["portal_appeal_id"] in world.mock_state.appeals
    assert world.mock_state.notes[finding["claim_id"]]

    event = world.mock_http.post(
        f"/payer/_simulate/{decided['portal_appeal_id']}/decide", params={"outcome": "won"}
    ).json()
    headers = {"X-Recoup-Timestamp": event["timestamp"], "X-Recoup-Signature": event["signature"],
               "Content-Type": "application/json"}  # fmt: skip
    forged = {**headers, "X-Recoup-Signature": sign("wrong", event["body"].encode(), event["timestamp"])}
    assert world.api.post("/webhooks/payer", content=event["body"], headers=forged).status_code == 401
    assert world.api.post("/webhooks/payer", content=event["body"], headers=headers).json()["status"] == "won"
    assert world.api.post("/webhooks/payer", content=event["body"], headers=headers).json()["duplicate"]
    lost = world.mock_http.post(
        f"/payer/_simulate/{decided['portal_appeal_id']}/decide", params={"outcome": "lost"}
    ).json()
    flip = {"X-Recoup-Timestamp": lost["timestamp"], "X-Recoup-Signature": lost["signature"]}
    assert world.api.post("/webhooks/payer", content=lost["body"], headers=flip).status_code == 409
    assert world.api.get(f"/findings/{finding['finding_id']}", headers=KEY).json()["status"] == "won"


def test_approval_survives_a_restart(world: World):
    load(world)
    candidates = world.api.get("/findings", headers=KEY, params={"kind": "underpaid", "route": "auto"}).json()
    finding = min(candidates, key=lambda f: f["payment_date"])
    world.today = date.fromisoformat(finding["payment_date"])
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    assert appeal["status"] == "awaiting_approval"
    world.start()  # new process: new service object, same database and checkpoint file
    decided = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).json()
    assert decided["status"] == "submitted"


def test_policy_blocks_late_or_unreviewed_appeals(world: World):
    load(world)
    world.today = date(2027, 6, 1)
    finding = world.api.get("/findings", headers=KEY, params={"kind": "underpaid"}).json()[0]
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    assert appeal["status"] == "blocked"
    assert any("window closed" in issue for issue in appeal["policy_issues"])
    assert world.mock_state.appeals == {}
    again = world.api.get(f"/findings/{finding['finding_id']}", headers=KEY).json()
    assert again["status"] == "open"  # a blocked draft hands the finding back
    second = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True})
    assert second.status_code == 409


def test_rejected_appeal_sends_nothing(world: World):
    load(world)
    finding = min(world.api.get("/findings", headers=KEY, params={"kind": "underpaid", "route": "auto"}).json(),
                  key=lambda f: f["payment_date"])  # fmt: skip
    world.today = date.fromisoformat(finding["payment_date"])
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    rejected = world.api.post(
        f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": False}
    ).json()
    assert rejected["status"] == "rejected" and world.mock_state.appeals == {}


def test_review_queue_decision(world: World):
    load(world)
    tasks = world.api.get("/review-tasks", headers=KEY).json()
    assert tasks, "the smoke set routes some findings to review"
    task = tasks[0]
    body = {"approve": False, "note": "payer is right: overpayment refunded"}
    decided = world.api.post(f"/review-tasks/{task['id']}/decision", headers=KEY, json=body).json()
    assert decided["status"] == "rejected"
    again = world.api.post(f"/review-tasks/{task['id']}/decision", headers=KEY, json=body)
    assert again.status_code == 409


def test_clearinghouse_sync_survives_rate_limits_and_outages(world: World):
    load(world, via_clearinghouse=True)
    files = len(list((world.dataset / "remits").glob("*.835")))
    assert world.mock_state.requests > files  # some requests were 429/503 and retried
    summary = world.api.get("/summary", headers=KEY).json()
    assert summary["lines"] == len(json.loads((world.dataset / "truth.json").read_text())["lines"])
    resend = world.api.post("/sync/clearinghouse", headers=KEY).json()
    assert all(item["duplicate"] for item in resend)


def test_retries_give_up_and_do_not_retry_client_errors():
    from fastapi import FastAPI, HTTPException

    calls = {"n": 0}
    broken = FastAPI()

    @broken.get("/down")
    def down() -> None:
        calls["n"] += 1
        raise HTTPException(503)

    @broken.get("/bad")
    def bad() -> None:
        calls["n"] += 1
        raise HTTPException(400, "nope")

    client = ResilientClient(TestClient(broken), attempts=3, sleep=lambda _s: None)
    with pytest.raises(UpstreamError, match="after 3 attempts"):
        client.request("GET", "/down")
    calls["n"] = 0
    with pytest.raises(UpstreamError) as error:
        client.request("GET", "/bad")
    assert error.value.status == 400 and calls["n"] == 1


def test_payer_portal_idempotency(world: World):
    portal = PayerPortalClient(world.http)
    first = portal.submit_appeal({"claim_id": "C1", "amount": "10", "letter": "x"}, idempotency_key="k-1")
    again = portal.submit_appeal({"claim_id": "C1", "amount": "10", "letter": "x"}, idempotency_key="k-1")
    assert first == again and len(world.mock_state.appeals) == 1


def test_pms_token_refresh_on_revocation(world: World):
    pms = PMSClient(world.http, "recoup", "pms-secret")
    pms.add_claim_note("C1", "first")
    world.mock_state.tokens.clear()  # the PMS revokes every token
    assert pms.add_claim_note("C1", "second")["notes"] == 2


def test_tenants_cannot_see_each_other(world: World):
    load(world)
    finding = world.api.get("/findings", headers=KEY).json()[0]
    other = {"X-API-Key": "k2"}
    assert world.api.get(f"/findings/{finding['finding_id']}", headers=other).status_code == 404
    assert world.api.get("/findings", headers=other).json() == []
    assert world.api.get("/findings", headers={"X-API-Key": "nope"}).status_code == 401


def test_job_queue_runs_remit_ingest(world: World):
    for pdf in sorted((world.dataset / "contracts").glob("*.pdf")):
        world.upload("/contracts", pdf)
    remit = sorted((world.dataset / "remits").glob("*.835"))[0]
    job_id = enqueue(world.service.db, "demo", "ingest_remit", {"text": remit.read_text(), "source": "queue"})
    table = handlers(world.service, world.clearinghouse)
    assert run_one(world.service.db, table) is True
    assert run_one(world.service.db, table) is False
    from recoup.db.models import Job

    with world.service.db.session(world.service.db.SYSTEM) as session:
        job = session.get(Job, job_id)
        assert job is not None and job.status == "done" and job.result and job.result["findings"] > 0


def test_mcp_tools_can_draft_but_not_send(world: World):
    load(world)
    server = create_mcp(world.service, "demo")
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {
        "audit_summary",
        "list_findings",
        "explain_finding",
        "search_contract_clauses",
        "draft_appeal",
    } <= names
    assert not any("approve" in name or "submit" in name for name in names)
    finding = world.api.get("/findings", headers=KEY, params={"kind": "underpaid"}).json()[0]
    result = asyncio.run(server.call_tool("explain_finding", {"finding_id": finding["finding_id"]}))
    assert "Contract" in json.dumps(result, default=str)


# Phase 5 review regressions (docs/REVIEWS.md)


def first_auto_underpaid(world: World) -> dict:  # type: ignore[type-arg]
    candidates = world.api.get("/findings", headers=KEY, params={"kind": "underpaid", "route": "auto"}).json()
    finding = min(candidates, key=lambda f: f["payment_date"])
    world.today = date.fromisoformat(finding["payment_date"])
    return finding


def test_a_decision_after_the_first_cannot_change_or_repeat_it(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    assert (
        world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).status_code
        == 200
    )
    late = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": False})
    assert late.status_code == 409
    assert len(world.mock_state.appeals) == 1 and len(world.mock_state.notes[finding["claim_id"]]) == 1


def test_a_rejection_racing_an_approval_loses_cleanly(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    original = world.service.portal.submit_appeal
    outcomes: list[int] = []

    def slow_portal(payload: dict, idempotency_key: str) -> dict:  # type: ignore[type-arg]
        # While the approval is mid-send, someone clicks reject.
        outcomes.append(world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY,
                                       json={"approve": False}).status_code)  # fmt: skip
        return original(payload, idempotency_key=idempotency_key)

    world.service.portal.submit_appeal = slow_portal  # type: ignore[method-assign]
    decided = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).json()
    assert outcomes == [409] and decided["status"] == "submitted" and len(world.mock_state.appeals) == 1


def test_concurrent_or_repeated_drafts_cannot_double_file(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    fid = finding["finding_id"]
    assert world.api.post("/appeals", headers=KEY, json={"finding_ids": [fid, fid, fid]}).status_code == 409
    assert world.api.post("/appeals", headers=KEY, json={"finding_ids": [fid]}).status_code == 201
    assert world.api.post("/appeals", headers=KEY, json={"finding_ids": [fid]}).status_code == 409


def test_policy_is_checked_again_at_approval(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    world.today = date(2027, 12, 31)  # the approval comes long after the window closed
    late = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True})
    assert late.status_code == 409 and "window closed" in late.json()["detail"]
    assert world.mock_state.appeals == {}
    assert world.api.get(f"/findings/{finding['finding_id']}", headers=KEY).json()["status"] == "open"


def test_failed_submission_can_be_retried_or_withdrawn(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    original = world.service.portal.submit_appeal

    def down(*_args: object, **_kwargs: object) -> dict:  # type: ignore[type-arg]
        raise UpstreamError("payer portal unreachable", 503)

    world.service.portal.submit_appeal = down  # type: ignore[method-assign]
    failed = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).json()
    assert failed["status"] == "submit_failed"
    world.service.portal.submit_appeal = original  # type: ignore[method-assign]
    assert world.api.post(f"/appeals/{appeal['id']}/retry", headers=SERVICE_KEY).status_code == 403
    retried = world.api.post(f"/appeals/{appeal['id']}/retry", headers=KEY).json()
    assert retried["status"] == "submitted" and len(world.mock_state.appeals) == 1
    assert not any(i.startswith("submission failed") for i in retried["policy_issues"])
    again = world.api.post(f"/appeals/{appeal['id']}/retry", headers=KEY)
    assert again.status_code == 409


def test_rejecting_a_letter_frees_the_finding_for_another_try(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": False})
    assert world.api.get(f"/findings/{finding['finding_id']}", headers=KEY).json()["status"] == "open"
    assert (
        world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).status_code
        == 201
    )


def test_webhook_cannot_decide_an_appeal_that_was_never_sent(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    body = json.dumps({"event": "appeal.decided", "appeal_id": "APL-99999", "outcome": "won",
                       "external_ref": appeal["id"]}).encode()  # fmt: skip
    import time as _time

    stamp = str(int(_time.time()))
    headers = {"X-Recoup-Timestamp": stamp, "X-Recoup-Signature": sign(SECRET, body, stamp)}
    assert world.api.post("/webhooks/payer", content=body, headers=headers).status_code == 409
    nan = {"X-Recoup-Timestamp": "nan", "X-Recoup-Signature": sign(SECRET, body, "nan")}
    assert world.api.post("/webhooks/payer", content=body, headers=nan).status_code == 401
    listy = b"[1, 2]"
    headers = {"X-Recoup-Timestamp": stamp, "X-Recoup-Signature": sign(SECRET, listy, stamp)}
    assert world.api.post("/webhooks/payer", content=listy, headers=headers).status_code == 409


def test_the_same_line_on_a_reissued_835_is_not_money_twice(world: World):
    load(world)
    before = world.api.get("/summary", headers=KEY).json()["recoverable"]
    first = sorted((world.dataset / "remits").glob("*.835"))[0].read_text()
    reissued = first.replace("TRN*1*EFT", "TRN*1*REISSUE")
    response = world.api.post("/remits", headers=KEY, files={"file": ("reissued.835", reissued.encode())})
    assert response.status_code == 201
    assert world.api.get("/summary", headers=KEY).json()["recoverable"] == before


def test_only_a_named_person_can_decide(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post(
        "/appeals", headers=SERVICE_KEY, json={"finding_ids": [finding["finding_id"]]}
    ).json()
    assert appeal["status"] == "awaiting_approval"  # services may draft
    denied = world.api.post(f"/appeals/{appeal['id']}/decision", headers=SERVICE_KEY, json={"approve": True})
    assert denied.status_code == 403
    spoof = {**SERVICE_KEY, "X-User": "the-ceo"}
    assert (
        world.api.post(f"/appeals/{appeal['id']}/decision", headers=spoof, json={"approve": True}).status_code
        == 403
    )
    decided = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).json()
    assert decided["approved_by"] == "biller@practice"


def test_a_lost_checkpoint_does_not_strand_the_decision(world: World):
    load(world)
    finding = first_auto_underpaid(world)
    appeal = world.api.post("/appeals", headers=KEY, json={"finding_ids": [finding["finding_id"]]}).json()
    world.checkpoints.unlink()
    for suffix in ("-wal", "-shm"):
        Path(f"{world.checkpoints}{suffix}").unlink(missing_ok=True)
    world.start()
    decided = world.api.post(f"/appeals/{appeal['id']}/decision", headers=KEY, json={"approve": True}).json()
    assert decided["status"] == "submitted"


def test_bad_input_is_a_4xx_not_a_500(world: World):
    bad = [
        ("/claims", b"\xff\xfe not utf8"),
        ("/claims", b"foo,bar\n1,2\n"),
        ("/contracts", b"%PDF-1.4 garbage that is not a pdf"),
        ("/remits", b"not an 835"),
    ]
    for path, data in bad:
        response = world.api.post(path, headers=KEY, files={"file": ("x", data)})
        assert response.status_code == 422, (path, response.status_code, response.text)
    assert world.api.get("/findings", headers=KEY, params={"limit": 0}).status_code == 422


def test_unsafe_posts_are_not_repeated_and_bad_retry_after_is_ignored():
    from fastapi import FastAPI, HTTPException

    from recoup.integrations.http import retry_after_seconds

    calls = {"n": 0}
    flaky = FastAPI()

    @flaky.post("/once")
    def once() -> None:
        calls["n"] += 1
        raise HTTPException(503, headers={"Retry-After": "nan"})

    client = ResilientClient(TestClient(flaky), attempts=4, sleep=lambda _s: None)
    with pytest.raises(UpstreamError):
        client.request("POST", "/once")
    assert calls["n"] == 1
    calls["n"] = 0
    with pytest.raises(UpstreamError):
        client.request("POST", "/once", idempotency_key="k")
    assert calls["n"] == 4
    assert retry_after_seconds("-5") is None and retry_after_seconds("nan") is None
    assert retry_after_seconds("garbage") is None and retry_after_seconds("2") == 2.0


def test_a_job_whose_worker_died_is_picked_up_again(world: World):
    from datetime import timedelta

    from recoup.db.models import Job, now
    from recoup.db.queue import claim

    job_id = enqueue(world.service.db, "demo", "ingest_remit", {"text": "x"})
    first = claim(world.service.db)
    assert first is not None and first.id == job_id and claim(world.service.db) is None
    with world.service.db.session(world.service.db.SYSTEM) as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.run_after = now() - timedelta(seconds=1)  # the lease ran out: its worker is gone
    again = claim(world.service.db)
    assert again is not None and again.id == job_id and again.attempts == 2


def test_system_tenant_cannot_be_configured():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DEMO_TENANT.model_validate({**DEMO_TENANT.model_dump(), "tenant_id": "*"})
    with pytest.raises(ValueError, match="system tenant"):
        create_app(None, {"k": ("*", "x")}, SECRET)  # type: ignore[arg-type]


def test_summary_separates_money_by_what_blocks_it(world: World):
    load(world)
    world.today = date(2026, 12, 31)
    buckets = {k: float(v) for k, v in world.api.get("/summary", headers=KEY).json()["recoverable"].items()}
    truth = json.loads((world.dataset / "truth.json").read_text())["lines"]
    total = sum(float(r["recoverable"]) for r in truth.values())
    assert abs(buckets["ready"] + buckets["in_review"] + buckets["past_deadline"] - total) < 0.01
    assert buckets["past_deadline"] > 0 and buckets["appealed"] == 0
    me = world.api.get("/me", headers=KEY).json()
    assert me == {"tenant_id": "demo", "user": "biller@practice", "can_decide": True}
    assert world.api.get("/me", headers=SERVICE_KEY).json()["can_decide"] is False
