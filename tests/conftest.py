"""Shared fixtures.

Every test database is built by the real migrations, never by
Base.metadata.create_all(), so tests see exactly the schema users get. The
migrations run once per session into a template file that is copied per test.

Time never comes from the system clock. NOW is a fixed instant, and tests move
the `clock` fixture when they need time to pass.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from task_orchard.db.engine import init_db, make_engine, make_session_factory
from task_orchard.devtools.seed import seed_demo

# Wednesday 18 March 2026, 10:00 in London (GMT, so also 10:00 UTC).
# Chosen so that a UK daylight-saving change (29 March 2026) is eleven days
# ahead, which recurrence tests can cross by advancing the clock.
TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 3, 18, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "template.db"
    engine = make_engine(path)
    init_db(engine)
    # Fold the write-ahead log into the main file so that copying one file is enough.
    # A checkpoint cannot run inside a transaction, so this one runs without.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    return path


@pytest.fixture
def db_path(_template_db: Path, tmp_path: Path) -> Path:
    path = tmp_path / "orchard.db"
    shutil.copy(_template_db, path)
    return path


@pytest.fixture
def engine(db_path: Path) -> Iterator[sa.Engine]:
    engine = make_engine(db_path)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: sa.Engine) -> Iterator[Session]:
    """A raw session, for tests of the schema itself. Service tests use `orchard`."""
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def orchard(engine: sa.Engine, clock: FixedClock) -> Orchard:
    """An empty orchard on a fixed clock."""
    return Orchard(make_session_factory(engine), clock, TZ)


@pytest.fixture
def seeded(orchard: Orchard) -> dict[str, int]:
    """Fill `orchard` with the demo data. Returns {handle: task id}; see devtools/seed.py."""
    with orchard.tx() as tx:
        return seed_demo(tx.session, tx.now, tx.tz)
