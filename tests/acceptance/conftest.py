import pytest
from sqlalchemy import text


@pytest.fixture(scope="session", autouse=True)
def require_postgresql17(migrated_database, pytestconfig):
    with migrated_database.engine.connect() as connection:
        version = connection.scalar(text("SHOW server_version_num"))
        assert int(version) // 10000 == 17, "Acceptance requires actual PostgreSQL 17"
        reporter = pytestconfig.pluginmanager.get_plugin("factory-acceptance-reporter")
        reporter.postgres_version = connection.scalar(text("SELECT version()"))


@pytest.fixture
def clock(database):
    from software_factory.db import db_now

    with database.transaction() as session:
        initial = db_now(session)

    class Clock:
        value = initial

        def __call__(self):
            return self.value

        def advance(self, seconds):
            self.value += seconds

    current = Clock()
    database.sessions.configure(info={"clock": current})
    try:
        yield current
    finally:
        database.sessions.configure(info={"clock": None})
