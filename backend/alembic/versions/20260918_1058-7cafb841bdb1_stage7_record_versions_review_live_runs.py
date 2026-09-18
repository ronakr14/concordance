"""stage7: versioned sanction records, reviewer's provider, one live run per scope

Three changes, each forced by a Stage 7 workflow rather than chosen for tidiness.

**Sanction records are versioned.** An upload that changes a record already on
file inserts a new row and marks the old one not current, instead of editing it
in place. The old row is what earlier runs were decided against, and replay
recomputes the snapshot hash over it; editing it would make every earlier run
unreplayable. Identity is `(source_authority, record_id)` and a partial unique
index allows exactly one current version of each. The global unique index on
`record_id` goes, because two versions of one record share a key by definition.

**`match_results.approved_provider_id`** holds the provider a reviewer
confirmed, beside the engine's `chosen_provider_id`. On an ambiguous result the
engine's column holds only its top-ranked candidate, and the reviewer often
picks another; storing the pick in the engine's column would erase the evidence
that they differed.

**One live run per scope.** A partial unique index over `file_id` (NULL folded
to a fixed UUID for the global scope) among `QUEUED`/`RUNNING` runs. Two
concurrent runs over the same records would supersede each other's results in
whatever order their chunks committed.

The remaining indexes serve the review queue, the dashboards and the audit
filter, which otherwise scan.

Revision ID: 7cafb841bdb1
Revises: 890ab105d7d4
Create Date: 2026-09-18 10:58:38.798106
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7cafb841bdb1'
down_revision: str | Sequence[str] | None = '890ab105d7d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'], unique=False)
    op.create_index('ix_cases_created_at', 'cases', ['created_at'], unique=False)
    op.create_index('ix_cases_match_result_id', 'cases', ['match_result_id'], unique=False)
    op.create_index('ix_cases_provider_id', 'cases', ['provider_id'], unique=False)
    op.add_column('match_results', sa.Column('approved_provider_id', sa.String(length=64), nullable=True))
    op.create_index('ix_match_results_created_at', 'match_results', ['created_at'], unique=False)
    op.create_index('ix_match_results_current_confidence', 'match_results', [sa.literal_column('calibrated_confidence DESC NULLS LAST')], unique=False, postgresql_where=sa.text('superseded_by IS NULL'))
    op.create_index('ix_match_results_current_decision', 'match_results', ['decision', 'review_status'], unique=False, postgresql_where=sa.text('superseded_by IS NULL'))
    op.create_foreign_key(op.f('fk_match_results_approved_provider_id_providers'), 'match_results', 'providers', ['approved_provider_id'], ['provider_id'], ondelete='SET NULL')
    op.add_column('reconciliation_runs', sa.Column('job_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_reconciliation_runs_created_at', 'reconciliation_runs', ['created_at'], unique=False)
    op.create_index('uq_reconciliation_runs_live_scope', 'reconciliation_runs', [sa.literal_column("coalesce(file_id, '00000000-0000-0000-0000-000000000000'::uuid)")], unique=True, postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"))
    op.add_column('sanction_records', sa.Column('is_current', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('sanction_records', sa.Column('replaced_by', sa.UUID(), nullable=True))
    op.add_column('sanction_records', sa.Column('replaced_at', sa.DateTime(timezone=True), nullable=True))
    op.drop_index(op.f('uq_sanction_records_record_id'), table_name='sanction_records')
    op.create_index('ix_sanction_records_current_ordinal', 'sanction_records', ['ordinal'], unique=False, postgresql_where=sa.text('is_current'))
    op.create_index('ix_sanction_records_record_id', 'sanction_records', ['record_id'], unique=False)
    op.create_index('ix_sanction_records_sanction_type', 'sanction_records', ['sanction_type'], unique=False)
    op.create_index('uq_sanction_records_current_identity', 'sanction_records', [sa.literal_column("coalesce(source_authority, '')"), 'record_id'], unique=True, postgresql_where=sa.text('is_current'))
    op.create_index('uq_sanction_records_file_id_record_id', 'sanction_records', ['file_id', 'record_id'], unique=True)
    op.create_foreign_key(op.f('fk_sanction_records_replaced_by_sanction_records'), 'sanction_records', 'sanction_records', ['replaced_by'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(op.f('fk_sanction_records_replaced_by_sanction_records'), 'sanction_records', type_='foreignkey')
    op.drop_index('uq_sanction_records_file_id_record_id', table_name='sanction_records')
    op.drop_index('uq_sanction_records_current_identity', table_name='sanction_records', postgresql_where=sa.text('is_current'))
    op.drop_index('ix_sanction_records_sanction_type', table_name='sanction_records')
    op.drop_index('ix_sanction_records_record_id', table_name='sanction_records')
    op.drop_index('ix_sanction_records_current_ordinal', table_name='sanction_records', postgresql_where=sa.text('is_current'))
    # Fails, correctly, if any record has more than one version by now: the
    # old schema cannot represent that, and dropping versions is data loss.
    op.create_index(op.f('uq_sanction_records_record_id'), 'sanction_records', ['record_id'], unique=True)
    op.drop_column('sanction_records', 'replaced_at')
    op.drop_column('sanction_records', 'replaced_by')
    op.drop_column('sanction_records', 'is_current')
    op.drop_index('uq_reconciliation_runs_live_scope', table_name='reconciliation_runs', postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"))
    op.drop_index('ix_reconciliation_runs_created_at', table_name='reconciliation_runs')
    op.drop_column('reconciliation_runs', 'job_id')
    op.drop_constraint(op.f('fk_match_results_approved_provider_id_providers'), 'match_results', type_='foreignkey')
    op.drop_index('ix_match_results_current_decision', table_name='match_results', postgresql_where=sa.text('superseded_by IS NULL'))
    op.drop_index('ix_match_results_current_confidence', table_name='match_results', postgresql_where=sa.text('superseded_by IS NULL'))
    op.drop_index('ix_match_results_created_at', table_name='match_results')
    op.drop_column('match_results', 'approved_provider_id')
    op.drop_index('ix_cases_provider_id', table_name='cases')
    op.drop_index('ix_cases_match_result_id', table_name='cases')
    op.drop_index('ix_cases_created_at', table_name='cases')
    op.drop_index('ix_audit_logs_action', table_name='audit_logs')
