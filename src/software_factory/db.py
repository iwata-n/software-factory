import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str | None = None, *, clock=None):
        url = url or os.environ.get("FACTORY_DATABASE_URL")
        if not url or not url.startswith("postgresql+psycopg://"):
            raise ValueError("FACTORY_DATABASE_URL must be a postgresql+psycopg:// URL")
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, info={"clock": clock})

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            yield session


def db_now(session: Session) -> float:
    clock = session.info.get("clock")
    if clock is not None:
        return float(clock())
    # clock_timestamp, unlike CURRENT_TIMESTAMP, advances while waiting for a row lock.
    return float(session.scalar(select(func.extract("epoch", func.clock_timestamp()))))
