"""SPEC section 10: two clients on one file wait for each other instead of failing.

Two MCP clients mean two server processes, each with its own engine, on one
SQLite file. In WAL mode a deferred transaction that reads and then writes
fails at once with "database is locked" if another connection committed in
between; busy_timeout does not help. BEGIN IMMEDIATE takes the write lock up
front, so the second transaction waits instead.
"""

from __future__ import annotations

import threading
from pathlib import Path

import sqlalchemy as sa

from task_orchard.core.clock import FixedClock
from task_orchard.core.service import Orchard
from task_orchard.db.engine import make_engine, make_session_factory
from task_orchard.db.models import Task
from tests.conftest import NOW, TZ


def two_orchards(db_path: Path) -> tuple[Orchard, Orchard]:
    return tuple(  # type: ignore[return-value]
        Orchard(make_session_factory(make_engine(db_path)), FixedClock(NOW), TZ) for _ in range(2)
    )


def test_two_instances_writing_alternately(db_path: Path) -> None:
    a, b = two_orchards(db_path)
    parent = a.create_task(title="Parent")["task"]["id"]
    for n in range(10):
        writer = a if n % 2 else b
        writer.create_task(title=f"Child {n}", parent_id=parent)
        writer.update_task(parent, append_note=f"note {n}")
    assert len(b.get_task(parent)["task"]["children"]) == 10


def test_two_instances_writing_at_once(db_path: Path) -> None:
    a, b = two_orchards(db_path)
    parent = a.create_task(title="Parent")["task"]["id"]
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def work(orchard: Orchard, name: str) -> None:
        try:
            start.wait()
            for n in range(25):
                # Each call reads (the parent, the graph) before it writes.
                orchard.create_task(title=f"{name} {n}", parent_id=parent)
                orchard.get_task(parent)
        except BaseException as err:  # noqa: BLE001 - reported below
            errors.append(err)

    threads = [threading.Thread(target=work, args=(o, n)) for o, n in ((a, "A"), (b, "B"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    engine = make_engine(db_path)
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(Task)) == 51
    engine.dispose()


def test_every_call_holds_the_write_lock_from_its_first_read(db_path: Path) -> None:
    """Without BEGIN IMMEDIATE, a call's reads run outside its transaction and
    another process can commit between the read and the write."""
    import sqlite3

    orchard, _ = two_orchards(db_path)
    other = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        with orchard.tx() as tx:
            tx.session.scalar(sa.select(sa.func.count()).select_from(Task))  # a read only
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as err:
                assert "locked" in str(err)
            else:
                other.execute("ROLLBACK")
                raise AssertionError("another connection could write during a call")
        other.execute("BEGIN IMMEDIATE")  # free again once the call is over
        other.execute("ROLLBACK")
    finally:
        other.close()
