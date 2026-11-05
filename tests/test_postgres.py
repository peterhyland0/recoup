"""Runs only against a real Postgres: RECOUP_TEST_POSTGRES_URL=postgresql+psycopg:///recoup_test.

Checks the two things SQLite cannot: row-level security, and SKIP LOCKED in the job queue.
Migrations run as the admin URL; the app connects as `recoup_app`, a role with neither
SUPERUSER nor BYPASSRLS, because either of those silently switches row-level security off.
"""

import os
import subprocess
import threading

import pytest
from sqlalchemy import text

from recoup.db.models import FindingRecord, Job
from recoup.db.queue import claim, enqueue
from recoup.db.session import Database

URL = os.environ.get("RECOUP_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set RECOUP_TEST_POSTGRES_URL to run Postgres tests")


APP_ROLE = "recoup_app"


@pytest.fixture(scope="module")
def db() -> Database:
    env = {**os.environ, "DATABASE_URL": URL or ""}
    subprocess.run(["alembic", "downgrade", "base"], check=True, env=env, capture_output=True)
    subprocess.run(["alembic", "upgrade", "head"], check=True, env=env, capture_output=True)
    admin = Database(URL)
    with admin.engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
        ).first()
        if not exists:
            connection.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS"))
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
        )
        connection.execute(text(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))
    url = admin.engine.url.set(username=APP_ROLE)
    return Database(url.render_as_string(hide_password=False))


def test_row_level_security_fails_closed(db: Database):
    with db.session("tenant-a") as session:
        session.execute(text("DELETE FROM jobs"))
    enqueue(db, "tenant-a", "noop", {})
    enqueue(db, "tenant-b", "noop", {})
    with db.session("tenant-a") as session:
        assert {j.tenant_id for j in session.query(Job).all()} == {"tenant-a"}
    with db.session() as session:  # no tenant set: sees nothing, even without a WHERE clause
        assert session.query(Job).count() == 0
        assert session.query(FindingRecord).count() == 0
    with db.session(db.SYSTEM) as session:
        assert session.query(Job).count() == 2
    with pytest.raises(Exception, match="row-level security"), db.session("tenant-a") as session:
        session.add(Job(tenant_id="tenant-b", kind="sneaky", payload={}))
        session.flush()


def test_skip_locked_hands_each_job_to_one_worker(db: Database):
    with db.session(db.SYSTEM) as session:
        session.execute(text("DELETE FROM jobs"))
    for n in range(20):
        enqueue(db, "tenant-a", "noop", {"n": n})
    claimed: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        while (job := claim(db)) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 20 and len(set(claimed)) == 20
