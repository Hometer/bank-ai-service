"""Add recoverable consultation and public safety state without rewriting history."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("context", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("conversations", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "safety_notices",
        sa.Column("owner_hash", sa.String(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_safety_notices_expires_at", "safety_notices", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("Destructive rollback requires a reviewed backup and migration plan")
