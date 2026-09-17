"""stage6: record the strategy and the request a run was started with

Replay re-executes a finished run and asserts it reaches the same decisions.
That is only possible if the run row says what it was: the strategy that
decided it, and the request it was started with - `max_candidates` above all,
because blocking with a different cap proposes a different candidate set and
every downstream number moves with it.

`strategy` is a column rather than a key inside `request` because the UI filters
on it and a check constraint should refuse a value the engine cannot produce.

Revision ID: 890ab105d7d4
Revises: 0001_initial
Create Date: 2026-09-17 14:36:00.831250
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "890ab105d7d4"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRATEGIES = ("deterministic", "fuzzy", "probabilistic", "probabilistic_llm")


def upgrade() -> None:
    op.add_column(
        "reconciliation_runs",
        sa.Column(
            "strategy", sa.String(length=30), server_default="probabilistic", nullable=False
        ),
    )
    op.add_column(
        "reconciliation_runs",
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "run_strategy_valid",
        "reconciliation_runs",
        sa.text("strategy IN (" + ", ".join(f"'{s}'" for s in STRATEGIES) + ")"),
    )


def downgrade() -> None:
    op.drop_constraint("run_strategy_valid", "reconciliation_runs", type_="check")
    op.drop_column("reconciliation_runs", "request")
    op.drop_column("reconciliation_runs", "strategy")
