"""Alembic environment.

Two ways in:

- At runtime, db.engine.init_db() passes an open connection in
  config.attributes["connection"].
- From the command line (`uv run alembic ...`), the database is taken from
  TASK_ORCHARD_DB, falling back to ./dev.db. This is only used to author new
  revisions with --autogenerate.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import context
from sqlalchemy.types import TypeDecorator

from task_orchard.db.engine import include_name
from task_orchard.db.models import Base

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:  # noqa: ARG001
    """Render our custom column types as their plain SQLAlchemy equivalents.

    A migration is a frozen record of the schema at one point in time, so it
    must not import application code that will keep changing.
    """
    ours = isinstance(obj, TypeDecorator) and type(obj).__module__.startswith("task_orchard")
    if type_ == "type" and ours:
        return f"sa.{obj.impl!r}"
    return False


def _run(connection: sa.Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        render_item=render_item,
        # SQLite cannot ALTER most things; batch mode rebuilds the table instead.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    db_path = os.environ.get("TASK_ORCHARD_DB", "dev.db")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        _run(conn)
        conn.commit()


if context.is_offline_mode():
    raise SystemExit("Offline mode is not supported; run against a SQLite file.")
run_migrations()
