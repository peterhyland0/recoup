"""Engine and sessions. SQLite for local runs and tests, Postgres in production (DATABASE_URL)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from recoup.db.models import Base

DEFAULT_URL = "sqlite:///recoup.db"


def make_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgres://")
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _pragmas(connection, _record):  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


class Database:
    def __init__(self, url: str | None = None):
        self.engine = make_engine(url)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Local and test convenience. Production schemas come from Alembic migrations."""
        Base.metadata.create_all(self.engine)

    SYSTEM = "*"
    """Pass as tenant_id for cross-tenant system work: the queue worker and verified webhooks."""

    @contextmanager
    def session(self, tenant_id: str | None = None) -> Iterator[Session]:
        with self._sessions() as session, session.begin():
            if tenant_id and self.engine.dialect.name == "postgresql":
                # Row-level security policies read this setting (see the initial migration).
                session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id}
                )
            yield session

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"
