"""stage9: the feedback loop - config lineage, activation, run patterns, audit sample

- `scoring_configs` gains `parent_id` (the config a retune started from),
  `metrics` (what it measured when written) and `created_by`, and a new
  `fitted_from` value, `semi_supervised`.
- `config_activations` records every decision to make a config the live one.
  The active config is the newest row. The newest config at upgrade time is
  activated here, so runs keep scoring with exactly what they used before.
- `run_patterns` is each run's full candidate-pair tally, as distinct
  comparison vectors with counts: the population a retune fits EM on.
- `reconciliation_runs.audit_rate` and `match_results.audit_sampled`: the random
  audit of auto-rejects that gives the region below the reject threshold
  labels with a known inclusion probability.
- `lab_sweeps.kind` admits `feedback`: simulated review rounds.

Revision ID: d7a2c5e9f184
Revises: c4e1a9d2b7f3
Create Date: 2026-09-19 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd7a2c5e9f184'
down_revision: str | Sequence[str] | None = 'c4e1a9d2b7f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "concordance_app"


def upgrade() -> None:
    # -- scoring_configs: lineage and measurements ---------------------------
    op.add_column('scoring_configs', sa.Column('parent_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        'scoring_configs',
        sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column('scoring_configs', sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f('fk_scoring_configs_parent_id_scoring_configs'), 'scoring_configs', 'scoring_configs',
        ['parent_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_scoring_configs_created_by_users'), 'scoring_configs', 'users',
        ['created_by'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_scoring_configs_parent_id', 'scoring_configs', ['parent_id'])
    op.drop_constraint(op.f('ck_scoring_configs_fitted_from_valid'), 'scoring_configs', type_='check')
    op.create_check_constraint(
        op.f('ck_scoring_configs_fitted_from_valid'), 'scoring_configs',
        "fitted_from IN ('em', 'supervised', 'semi_supervised', 'manual')",
    )

    # -- config_activations ---------------------------------------------------
    op.create_table(
        'config_activations',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('scoring_config_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('activated_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['scoring_config_id'], ['scoring_configs.id'],
                                name=op.f('fk_config_activations_scoring_config_id_scoring_configs'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['activated_by'], ['users.id'],
                                name=op.f('fk_config_activations_activated_by_users'),
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_config_activations')),
    )
    op.create_index('ix_config_activations_created_at', 'config_activations', ['created_at'])
    op.create_index('ix_config_activations_scoring_config_id', 'config_activations',
                    ['scoring_config_id'])
    # Before this table, the live config was simply the newest one. Activating
    # that config now keeps every future run on it until someone decides
    # otherwise - a retune written later does not take over on its own.
    op.execute(
        """
        INSERT INTO config_activations (scoring_config_id, reason)
        SELECT id, 'activated by migration: the newest config when activation was introduced'
        FROM scoring_configs
        ORDER BY fitted_at DESC
        LIMIT 1
        """
    )

    # -- run_patterns ---------------------------------------------------------
    op.create_table(
        'run_patterns',
        sa.Column('run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('pattern', postgresql.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column('n', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'],
                                name=op.f('fk_run_patterns_run_id_reconciliation_runs'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id', 'kind', 'pattern', name='pk_run_patterns'),
    )

    # -- the audit sample -----------------------------------------------------
    op.add_column('reconciliation_runs', sa.Column('audit_rate', sa.Float(), nullable=True))
    op.add_column(
        'match_results',
        sa.Column('audit_sampled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.create_index(
        'ix_match_results_current_audit', 'match_results', ['review_status'],
        postgresql_where=sa.text('audit_sampled AND superseded_by IS NULL'),
    )

    # -- lab_sweeps: simulated review rounds ----------------------------------
    op.drop_constraint(op.f('ck_lab_sweeps_kind_valid'), 'lab_sweeps', type_='check')
    op.create_check_constraint(
        op.f('ck_lab_sweeps_kind_valid'), 'lab_sweeps', "kind IN ('sweep', 'llm', 'feedback')"
    )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regrole('{APP_ROLE}') IS NOT NULL THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE config_activations TO {APP_ROLE};
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE run_patterns TO {APP_ROLE};
                GRANT USAGE, SELECT ON SEQUENCE config_activations_id_seq TO {APP_ROLE};
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM lab_sweeps WHERE kind = 'feedback'")
    op.drop_constraint(op.f('ck_lab_sweeps_kind_valid'), 'lab_sweeps', type_='check')
    op.create_check_constraint(
        op.f('ck_lab_sweeps_kind_valid'), 'lab_sweeps', "kind IN ('sweep', 'llm')"
    )
    op.drop_index('ix_match_results_current_audit', table_name='match_results')
    op.drop_column('match_results', 'audit_sampled')
    op.drop_column('reconciliation_runs', 'audit_rate')
    op.drop_table('run_patterns')
    op.drop_index('ix_config_activations_scoring_config_id', table_name='config_activations')
    op.drop_index('ix_config_activations_created_at', table_name='config_activations')
    op.drop_table('config_activations')
    op.execute(
        "UPDATE scoring_configs SET fitted_from = 'supervised' WHERE fitted_from = 'semi_supervised'"
    )
    op.drop_constraint(op.f('ck_scoring_configs_fitted_from_valid'), 'scoring_configs', type_='check')
    op.create_check_constraint(
        op.f('ck_scoring_configs_fitted_from_valid'), 'scoring_configs',
        "fitted_from IN ('em', 'supervised', 'manual')",
    )
    op.drop_index('ix_scoring_configs_parent_id', table_name='scoring_configs')
    op.drop_constraint(op.f('fk_scoring_configs_created_by_users'), 'scoring_configs',
                       type_='foreignkey')
    op.drop_constraint(op.f('fk_scoring_configs_parent_id_scoring_configs'), 'scoring_configs',
                       type_='foreignkey')
    op.drop_column('scoring_configs', 'created_by')
    op.drop_column('scoring_configs', 'metrics')
    op.drop_column('scoring_configs', 'parent_id')
