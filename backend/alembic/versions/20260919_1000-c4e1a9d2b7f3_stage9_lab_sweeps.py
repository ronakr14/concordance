"""stage9: lab sweeps, and eval_runs rows that belong to one

`lab_sweeps` is one Lab experiment - a corruption sweep, or an LLM sample that
extends one. `eval_runs` gains `sweep_id`, so a robustness curve is always one
experiment's cells and never a mixture of two, and `detail`, for everything the
columns do not hold (per-scenario tallies, the fit's before/after calibration,
an LLM sample's intervals and costs).

The first table since the initial schema, so the first migration that has to
grant the application role its privileges itself: the initial migration's
blanket `GRANT ... ON ALL TABLES` covered only the tables that existed then.

Revision ID: c4e1a9d2b7f3
Revises: b8d3e2a41f07
Create Date: 2026-09-19 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4e1a9d2b7f3'
down_revision: str | Sequence[str] | None = 'b8d3e2a41f07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "concordance_app"


def upgrade() -> None:
    op.create_table(
        'lab_sweeps',
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('parent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='QUEUED', nullable=False),
        sa.Column('requested_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('job_id', sa.BigInteger(), nullable=True),
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('progress', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('error', sa.String(length=2000), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', postgresql.UUID(as_uuid=True),
                  server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind IN ('sweep', 'llm')", name=op.f('ck_lab_sweeps_kind_valid')),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name=op.f('ck_lab_sweeps_status_valid'),
        ),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'],
                                name=op.f('fk_lab_sweeps_job_id_jobs'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['parent_id'], ['lab_sweeps.id'],
                                name=op.f('fk_lab_sweeps_parent_id_lab_sweeps'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'],
                                name=op.f('fk_lab_sweeps_requested_by_users'),
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_lab_sweeps')),
    )
    op.create_index('ix_lab_sweeps_kind_created_at', 'lab_sweeps', ['kind', 'created_at'])
    op.create_index('ix_lab_sweeps_parent_id', 'lab_sweeps', ['parent_id'])

    op.add_column('eval_runs', sa.Column('sweep_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        'eval_runs',
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_foreign_key(
        op.f('fk_eval_runs_sweep_id_lab_sweeps'), 'eval_runs', 'lab_sweeps',
        ['sweep_id'], ['id'], ondelete='CASCADE',
    )
    op.create_index('ix_eval_runs_sweep_id', 'eval_runs', ['sweep_id'])

    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regrole('{APP_ROLE}') IS NOT NULL THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE lab_sweeps TO {APP_ROLE};
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index('ix_eval_runs_sweep_id', table_name='eval_runs')
    op.drop_constraint(op.f('fk_eval_runs_sweep_id_lab_sweeps'), 'eval_runs', type_='foreignkey')
    op.drop_column('eval_runs', 'detail')
    op.drop_column('eval_runs', 'sweep_id')
    op.drop_index('ix_lab_sweeps_parent_id', table_name='lab_sweeps')
    op.drop_index('ix_lab_sweeps_kind_created_at', table_name='lab_sweeps')
    op.drop_table('lab_sweeps')
