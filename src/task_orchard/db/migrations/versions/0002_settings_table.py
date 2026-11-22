"""settings table

One row holding settings that live with the data: the user's timezone, set by
set_timezone (SPEC 3.1, 8.19).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_settings_one_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settings")),
    )


def downgrade() -> None:
    op.drop_table("settings")
