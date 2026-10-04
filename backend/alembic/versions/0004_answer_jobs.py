"""Add recoverable answer jobs; keep all existing conversations and history."""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "answer_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "session_id", sa.String(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.UniqueConstraint("session_id", "request_id", name="uq_answer_job_request"),
    )
    op.create_index("ix_answer_jobs_session_id", "answer_jobs", ["session_id"])


def downgrade() -> None:
    raise RuntimeError("Destructive rollback requires a reviewed backup and migration plan")
