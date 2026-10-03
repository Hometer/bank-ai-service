"""Append recoverable public reading navigation without modifying old messages."""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("navigation", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    raise RuntimeError("Destructive rollback requires a reviewed backup and migration plan")
