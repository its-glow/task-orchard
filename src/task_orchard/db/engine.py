"""Engine, sessions and migrations.

There is exactly one way a database comes into being: init_db(), which runs the
Alembic migrations to head. Tests use the same path, so the schema the tests
see is always the schema users get (including the full-text tables, which are
not part of the ORM metadata).
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# The full-text tables are created by hand in the migrations and are not ORM
# models. Anything that compares the database with the models must skip them
# (and the shadow tables SQLite creates alongside each one).
FTS_PREFIXES = ("tasks_fts", "patterns_fts")


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:  # noqa: ARG001
    """Alembic filter: ignore the full-text tables when diffing against the models."""
    return not (type_ == "table" and name is not None and name.startswith(FTS_PREFIXES))


def make_engine(db_path: Path | str) -> sa.Engine:
    """An engine for the SQLite file at `db_path`, created if missing."""
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(f"sqlite:///{path}")

    @sa.event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # SQLite ignores foreign keys unless asked, per connection.
        cursor.execute("PRAGMA foreign_keys = ON")
        # WAL lets the MCP server and a later HTTP reader share the file.
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    return engine


def alembic_config(connection: sa.Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def init_db(engine: sa.Engine) -> None:
    """Bring the database to the latest schema. Safe to call on every start."""
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")


def make_session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: rows are read after commit to build tool responses.
    return sessionmaker(engine, expire_on_commit=False)
