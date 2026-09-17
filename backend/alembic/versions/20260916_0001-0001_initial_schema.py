"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-16

Everything Stage 5 needs, in one migration: eighteen tables, their constraints
and indexes, the `pg_trgm` extension the trigram block depends on, and the
revoke that makes `audit_logs` append-only.

Three things in here are not ordinary autogenerate output and should stay
hand-written.

1. **`CREATE EXTENSION pg_trgm` runs first.** The GIN indexes on
   `providers.trigram_key` and `sanction_records.trigram_key` use
   `gin_trgm_ops`, which does not exist until the extension does. Creating the
   extension inside the migration rather than in a setup script means a fresh
   database restores from `alembic upgrade head` alone.

2. **`audit_logs` loses UPDATE and DELETE.** The application role may insert and
   select; it may not rewrite history. The revoke is wrapped in a `DO` block
   that checks the role exists, because a developer database created by the
   owning superuser has no separate application role and the migration must not
   fail there.

3. **The downgrade drops everything, including the extension.** GATE 5 asks for
   `downgrade base` to leave no orphaned objects, and an extension the migration
   created is an object it owns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

#: The least-privileged role the application connects as. A developer box
#: usually has no such role and the grant block quietly skips.
APP_ROLE = "concordance_app"


def upgrade() -> None:
    # pg_trgm before any index that uses gin_trgm_ops.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # gen_random_uuid() is core from Postgres 13 on; pgcrypto is not needed.

    op.create_table('jobs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED', 'DEAD')", name=op.f('ck_jobs_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs'))
    )
    op.create_index('ix_jobs_kind', 'jobs', ['kind'], unique=False)
    op.create_index('ix_jobs_status_run_after', 'jobs', ['status', 'run_after'], unique=False)
    op.create_table('llm_calls',
    sa.Column('cache_key', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('prompt_version', sa.String(length=50), nullable=False),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('latency_ms', sa.Integer(), server_default='0', nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls'))
    )
    op.create_index('uq_llm_calls_cache_key', 'llm_calls', ['cache_key'], unique=True)
    op.create_table('providers',
    sa.Column('provider_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='ACTIVE', nullable=False),
    sa.Column('ordinal', sa.BigInteger(), nullable=False),
    sa.Column('cluster_id', sa.String(length=64), nullable=True),
    sa.Column('cluster_role', sa.String(length=32), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('npi', sa.String(length=20), nullable=True),
    sa.Column('first_name', sa.String(length=100), nullable=True),
    sa.Column('middle_name', sa.String(length=100), nullable=True),
    sa.Column('last_name', sa.String(length=100), nullable=True),
    sa.Column('suffix', sa.String(length=20), nullable=True),
    sa.Column('dob', sa.Date(), nullable=True),
    sa.Column('address_line1', sa.String(length=200), nullable=True),
    sa.Column('address_line2', sa.String(length=200), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('state', sa.String(length=2), nullable=True),
    sa.Column('zip', sa.String(length=10), nullable=True),
    sa.Column('license_number', sa.String(length=50), nullable=True),
    sa.Column('license_state', sa.String(length=2), nullable=True),
    sa.Column('specialty', sa.String(length=100), nullable=True),
    sa.Column('organization_name', sa.String(length=200), nullable=True),
    sa.Column('dba_name', sa.String(length=200), nullable=True),
    sa.Column('ein', sa.String(length=20), nullable=True),
    sa.Column('is_organization', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('name_norm', sa.String(length=200), server_default='', nullable=False),
    sa.Column('name_sorted_norm', sa.String(length=200), server_default='', nullable=False),
    sa.Column('name_phonetic', sa.String(length=64), server_default='', nullable=False),
    sa.Column('addr_norm', sa.String(length=300), server_default='', nullable=False),
    sa.Column('zip5', sa.String(length=5), server_default='', nullable=False),
    sa.Column('trigram_key', sa.String(length=200), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE', 'RETIRED')", name=op.f('ck_providers_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_providers'))
    )
    op.create_index('ix_providers_license_number_license_state', 'providers', ['license_number', 'license_state'], unique=False)
    op.create_index('ix_providers_name_norm', 'providers', ['name_norm'], unique=False)
    op.create_index('ix_providers_name_phonetic_state', 'providers', ['name_phonetic', 'state'], unique=False)
    op.create_index('ix_providers_npi', 'providers', ['npi'], unique=False)
    op.create_index('ix_providers_ordinal', 'providers', ['ordinal'], unique=False)
    op.create_index('ix_providers_state_dob', 'providers', ['state', 'dob'], unique=False)
    op.create_index('ix_providers_trigram_key_gin', 'providers', ['trigram_key'], unique=False, postgresql_using='gin', postgresql_ops={'trigram_key': 'gin_trgm_ops'})
    op.create_index('ix_providers_zip5_last_name', 'providers', ['zip5', 'last_name'], unique=False)
    op.create_index('uq_providers_provider_id', 'providers', ['provider_id'], unique=True)
    op.create_table('scoring_configs',
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('t_auto_accept', sa.Float(), nullable=False),
    sa.Column('t_auto_reject', sa.Float(), nullable=False),
    sa.Column('calibrator', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('fitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('fitted_from', sa.String(length=20), server_default='em', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.CheckConstraint("fitted_from IN ('em', 'supervised', 'manual')", name=op.f('ck_scoring_configs_fitted_from_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scoring_configs'))
    )
    op.create_index('uq_scoring_configs_version', 'scoring_configs', ['version'], unique=True)
    op.create_table('users',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role IN ('analyst', 'admin')", name=op.f('ck_users_role_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index('uq_users_email_lower', 'users', [sa.literal_column('lower(email)')], unique=True)
    op.create_table('audit_logs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('actor_role', sa.String(length=20), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('ip', postgresql.INET(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_logs_actor_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index('ix_audit_logs_actor_user_id', 'audit_logs', ['actor_user_id'], unique=False)
    op.create_index('ix_audit_logs_created_at', 'audit_logs', [sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_audit_logs_entity_type_entity_id', 'audit_logs', ['entity_type', 'entity_id'], unique=False)
    op.create_table('column_mappings',
    sa.Column('source_authority', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('mapping', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_column_mappings_created_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_column_mappings'))
    )
    op.create_index('uq_column_mappings_source_authority_name', 'column_mappings', ['source_authority', 'name'], unique=True)
    op.create_table('provider_block_keys',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('provider_id', sa.String(length=64), nullable=False),
    sa.Column('block', sa.String(length=32), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('ordinal', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.provider_id'], name=op.f('fk_provider_block_keys_provider_id_providers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_block_keys'))
    )
    op.create_index('ix_provider_block_keys_block_key', 'provider_block_keys', ['block', 'key'], unique=False)
    op.create_index('ix_provider_block_keys_provider_id', 'provider_block_keys', ['provider_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens'))
    )
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'], unique=False)
    op.create_index('uq_refresh_tokens_token_hash', 'refresh_tokens', ['token_hash'], unique=True)
    op.create_table('sanction_files',
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('storage_uri', sa.String(length=500), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('uploaded_by', sa.UUID(), nullable=True),
    sa.Column('row_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('mapping_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='INSPECTED', nullable=False),
    sa.Column('source_authority', sa.String(length=100), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.CheckConstraint("status IN ('INSPECTED', 'COMMITTED', 'REJECTED')", name=op.f('ck_sanction_files_status_valid')),
    sa.ForeignKeyConstraint(['mapping_id'], ['column_mappings.id'], name=op.f('fk_sanction_files_mapping_id_column_mappings'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_sanction_files_uploaded_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sanction_files'))
    )
    op.create_index('ix_sanction_files_status', 'sanction_files', ['status'], unique=False)
    op.create_index('uq_sanction_files_sha256', 'sanction_files', ['sha256'], unique=True)
    op.create_table('reconciliation_runs',
    sa.Column('triggered_by', sa.UUID(), nullable=True),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='QUEUED', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('engine_version', sa.String(length=50), nullable=True),
    sa.Column('scoring_config_id', sa.UUID(), nullable=True),
    sa.Column('prompt_version', sa.String(length=50), nullable=True),
    sa.Column('provider_snapshot_hash', sa.String(length=64), nullable=True),
    sa.Column('sanction_snapshot_hash', sa.String(length=64), nullable=True),
    sa.Column('records_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('matched_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ambiguous_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('no_match_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('llm_calls', sa.Integer(), server_default='0', nullable=False),
    sa.Column('llm_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('llm_cost_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.CheckConstraint("status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')", name=op.f('ck_reconciliation_runs_status_valid')),
    sa.ForeignKeyConstraint(['file_id'], ['sanction_files.id'], name=op.f('fk_reconciliation_runs_file_id_sanction_files'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['scoring_config_id'], ['scoring_configs.id'], name=op.f('fk_reconciliation_runs_scoring_config_id_scoring_configs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['triggered_by'], ['users.id'], name=op.f('fk_reconciliation_runs_triggered_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reconciliation_runs'))
    )
    op.create_index('ix_reconciliation_runs_file_id', 'reconciliation_runs', ['file_id'], unique=False)
    op.create_index('ix_reconciliation_runs_status', 'reconciliation_runs', ['status'], unique=False)
    op.create_table('sanction_records',
    sa.Column('record_id', sa.String(length=64), nullable=False),
    sa.Column('file_id', sa.UUID(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('dob_raw', sa.String(length=64), nullable=True),
    sa.Column('sanction_type', sa.String(length=100), nullable=True),
    sa.Column('exclusion_date', sa.Date(), nullable=True),
    sa.Column('reinstatement_date', sa.Date(), nullable=True),
    sa.Column('source_authority', sa.String(length=100), nullable=True),
    sa.Column('ordinal', sa.BigInteger(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('npi', sa.String(length=20), nullable=True),
    sa.Column('first_name', sa.String(length=100), nullable=True),
    sa.Column('middle_name', sa.String(length=100), nullable=True),
    sa.Column('last_name', sa.String(length=100), nullable=True),
    sa.Column('suffix', sa.String(length=20), nullable=True),
    sa.Column('dob', sa.Date(), nullable=True),
    sa.Column('address_line1', sa.String(length=200), nullable=True),
    sa.Column('address_line2', sa.String(length=200), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.Column('state', sa.String(length=2), nullable=True),
    sa.Column('zip', sa.String(length=10), nullable=True),
    sa.Column('license_number', sa.String(length=50), nullable=True),
    sa.Column('license_state', sa.String(length=2), nullable=True),
    sa.Column('specialty', sa.String(length=100), nullable=True),
    sa.Column('organization_name', sa.String(length=200), nullable=True),
    sa.Column('dba_name', sa.String(length=200), nullable=True),
    sa.Column('ein', sa.String(length=20), nullable=True),
    sa.Column('is_organization', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('name_norm', sa.String(length=200), server_default='', nullable=False),
    sa.Column('name_sorted_norm', sa.String(length=200), server_default='', nullable=False),
    sa.Column('name_phonetic', sa.String(length=64), server_default='', nullable=False),
    sa.Column('addr_norm', sa.String(length=300), server_default='', nullable=False),
    sa.Column('zip5', sa.String(length=5), server_default='', nullable=False),
    sa.Column('trigram_key', sa.String(length=200), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['sanction_files.id'], name=op.f('fk_sanction_records_file_id_sanction_files'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sanction_records'))
    )
    op.create_index('ix_sanction_records_file_id', 'sanction_records', ['file_id'], unique=False)
    op.create_index('ix_sanction_records_license_number_license_state', 'sanction_records', ['license_number', 'license_state'], unique=False)
    op.create_index('ix_sanction_records_name_norm', 'sanction_records', ['name_norm'], unique=False)
    op.create_index('ix_sanction_records_name_phonetic_state', 'sanction_records', ['name_phonetic', 'state'], unique=False)
    op.create_index('ix_sanction_records_npi', 'sanction_records', ['npi'], unique=False)
    op.create_index('ix_sanction_records_ordinal', 'sanction_records', ['ordinal'], unique=False)
    op.create_index('ix_sanction_records_state_dob', 'sanction_records', ['state', 'dob'], unique=False)
    op.create_index('ix_sanction_records_trigram_key_gin', 'sanction_records', ['trigram_key'], unique=False, postgresql_using='gin', postgresql_ops={'trigram_key': 'gin_trgm_ops'})
    op.create_index('ix_sanction_records_zip5_last_name', 'sanction_records', ['zip5', 'last_name'], unique=False)
    op.create_index('uq_sanction_records_record_id', 'sanction_records', ['record_id'], unique=True)
    op.create_table('eval_runs',
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('corruption_level', sa.Float(), nullable=True),
    sa.Column('strategy', sa.String(length=30), nullable=False),
    sa.Column('precision', sa.Float(), nullable=True),
    sa.Column('recall', sa.Float(), nullable=True),
    sa.Column('f1', sa.Float(), nullable=True),
    sa.Column('false_positives', sa.Integer(), server_default='0', nullable=False),
    sa.Column('false_negatives', sa.Integer(), server_default='0', nullable=False),
    sa.Column('brier', sa.Float(), nullable=True),
    sa.Column('ece', sa.Float(), nullable=True),
    sa.Column('reliability_bins', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('blocking_recall', sa.Float(), nullable=True),
    sa.Column('scoring_config_id', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("strategy IN ('deterministic', 'fuzzy', 'probabilistic', 'probabilistic_llm')", name=op.f('ck_eval_runs_strategy_valid')),
    sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], name=op.f('fk_eval_runs_run_id_reconciliation_runs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['scoring_config_id'], ['scoring_configs.id'], name=op.f('fk_eval_runs_scoring_config_id_scoring_configs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_runs'))
    )
    op.create_index('ix_eval_runs_strategy_corruption_level', 'eval_runs', ['strategy', 'corruption_level'], unique=False)
    op.create_table('ground_truth',
    sa.Column('sanction_record_id', sa.UUID(), nullable=False),
    sa.Column('expected_provider_id', sa.String(length=64), nullable=True),
    sa.Column('expected_outcome', sa.String(length=20), nullable=False),
    sa.Column('corruption_profile', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('scenario_tag', sa.String(length=50), server_default='', nullable=False),
    sa.CheckConstraint("expected_outcome IN ('MATCH', 'NO_MATCH', 'AMBIGUOUS')", name=op.f('ck_ground_truth_expected_outcome_valid')),
    sa.ForeignKeyConstraint(['expected_provider_id'], ['providers.provider_id'], name=op.f('fk_ground_truth_expected_provider_id_providers'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['sanction_record_id'], ['sanction_records.id'], name=op.f('fk_ground_truth_sanction_record_id_sanction_records'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('sanction_record_id', name=op.f('pk_ground_truth'))
    )
    op.create_index('ix_ground_truth_expected_provider_id', 'ground_truth', ['expected_provider_id'], unique=False)
    op.create_index('ix_ground_truth_scenario_tag', 'ground_truth', ['scenario_tag'], unique=False)
    op.create_table('match_results',
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('sanction_record_id', sa.UUID(), nullable=False),
    sa.Column('decision', sa.String(length=20), nullable=False),
    sa.Column('chosen_provider_id', sa.String(length=64), nullable=True),
    sa.Column('posterior', sa.Float(), nullable=True),
    sa.Column('calibrated_confidence', sa.Float(), nullable=True),
    sa.Column('raw_match_weight', sa.Float(), nullable=True),
    sa.Column('route', sa.String(length=20), nullable=False),
    sa.Column('llm_call_id', sa.UUID(), nullable=True),
    sa.Column('explanation', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('review_status', sa.String(length=20), server_default='PENDING', nullable=False),
    sa.Column('reviewed_by', sa.UUID(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reviewer_comment', sa.Text(), nullable=True),
    sa.Column('superseded_by', sa.UUID(), nullable=True),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("decision IN ('MATCH', 'AMBIGUOUS', 'NO_MATCH')", name=op.f('ck_match_results_decision_valid')),
    sa.CheckConstraint("review_status IN ('PENDING', 'APPROVED', 'REJECTED', 'ESCALATED')", name=op.f('ck_match_results_review_status_valid')),
    sa.CheckConstraint("route IN ('deterministic', 'probabilistic', 'llm')", name=op.f('ck_match_results_route_valid')),
    sa.ForeignKeyConstraint(['chosen_provider_id'], ['providers.provider_id'], name=op.f('fk_match_results_chosen_provider_id_providers'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['llm_call_id'], ['llm_calls.id'], name=op.f('fk_match_results_llm_call_id_llm_calls'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], name=op.f('fk_match_results_reviewed_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], name=op.f('fk_match_results_run_id_reconciliation_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['sanction_record_id'], ['sanction_records.id'], name=op.f('fk_match_results_sanction_record_id_sanction_records'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['superseded_by'], ['match_results.id'], name=op.f('fk_match_results_superseded_by_match_results'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_match_results'))
    )
    op.create_index('ix_match_results_current', 'match_results', ['run_id', 'review_status'], unique=False, postgresql_where=sa.text('superseded_by IS NULL'))
    op.create_index('ix_match_results_run_id_review_status', 'match_results', ['run_id', 'review_status'], unique=False)
    op.create_index('ix_match_results_sanction_record_id', 'match_results', ['sanction_record_id'], unique=False)
    op.create_index('uq_match_results_run_id_sanction_record_id', 'match_results', ['run_id', 'sanction_record_id'], unique=True)
    op.create_table('cases',
    sa.Column('case_number', sa.String(length=50), nullable=False),
    sa.Column('provider_id', sa.String(length=64), nullable=False),
    sa.Column('sanction_record_id', sa.UUID(), nullable=False),
    sa.Column('match_result_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='ACTIVE', nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('duration_months', sa.Integer(), server_default='3', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('closed_by', sa.UUID(), nullable=True),
    sa.Column('close_reason', sa.Text(), nullable=True),
    sa.Column('conflict_flag', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('conflict_match_result_id', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE', 'EXPIRED', 'CLOSED', 'REJECTED')", name=op.f('ck_cases_status_valid')),
    sa.ForeignKeyConstraint(['closed_by'], ['users.id'], name=op.f('fk_cases_closed_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conflict_match_result_id'], ['match_results.id'], name=op.f('fk_cases_conflict_match_result_id_match_results'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_cases_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['match_result_id'], ['match_results.id'], name=op.f('fk_cases_match_result_id_match_results'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.provider_id'], name=op.f('fk_cases_provider_id_providers'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['sanction_record_id'], ['sanction_records.id'], name=op.f('fk_cases_sanction_record_id_sanction_records'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cases'))
    )
    op.create_index('ix_cases_conflict_flag', 'cases', ['conflict_flag'], unique=False, postgresql_where=sa.text('conflict_flag'))
    op.create_index('ix_cases_status_end_date', 'cases', ['status', 'end_date'], unique=False)
    op.create_index('uq_cases_active_provider_sanction', 'cases', ['provider_id', 'sanction_record_id'], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))
    op.create_index('uq_cases_case_number', 'cases', ['case_number'], unique=True)
    op.create_table('feedback_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('match_result_id', sa.UUID(), nullable=False),
    sa.Column('reviewer_id', sa.UUID(), nullable=True),
    sa.Column('label', sa.String(length=20), nullable=False),
    sa.Column('comparison_vector', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("label IN ('TRUE_MATCH', 'FALSE_MATCH')", name=op.f('ck_feedback_events_label_valid')),
    sa.ForeignKeyConstraint(['match_result_id'], ['match_results.id'], name=op.f('fk_feedback_events_match_result_id_match_results'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewer_id'], ['users.id'], name=op.f('fk_feedback_events_reviewer_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_feedback_events'))
    )
    op.create_index('ix_feedback_events_match_result_id', 'feedback_events', ['match_result_id'], unique=False)
    op.create_table('match_candidates',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('match_result_id', sa.UUID(), nullable=False),
    sa.Column('provider_id', sa.String(length=64), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('field_levels', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('field_weights', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('match_weight', sa.Float(), server_default='0', nullable=False),
    sa.Column('posterior', sa.Float(), server_default='0', nullable=False),
    sa.Column('blocking_keys', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.ForeignKeyConstraint(['match_result_id'], ['match_results.id'], name=op.f('fk_match_candidates_match_result_id_match_results'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.provider_id'], name=op.f('fk_match_candidates_provider_id_providers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_match_candidates'))
    )
    op.create_index('ix_match_candidates_match_result_id_rank', 'match_candidates', ['match_result_id', 'rank'], unique=False)
    op.create_index('ix_match_candidates_provider_id', 'match_candidates', ['provider_id'], unique=False)

    # -- privileges for the application role ----------------------------
    # The role in DATABASE_URL owns these tables and runs migrations. The
    # application connects as APP_ROLE instead, which holds data privileges
    # and no DDL. Both statements are guarded: a GRANT or REVOKE naming a role
    # that does not exist is an error, not a no-op, and `to_regrole` returns
    # NULL for an unknown role. The REVOKE must come after the GRANT -- the
    # blanket grant would otherwise hand back the two privileges audit_logs
    # exists to withhold.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regrole('{APP_ROLE}') IS NOT NULL THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
                GRANT USAGE, SELECT
                    ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};

                -- audit_logs is append-only. An owner cannot be revoked from
                -- its own table, which is precisely why the application does
                -- not connect as the owner.
                REVOKE UPDATE, DELETE ON TABLE audit_logs FROM {APP_ROLE};
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index('ix_match_candidates_provider_id', table_name='match_candidates')
    op.drop_index('ix_match_candidates_match_result_id_rank', table_name='match_candidates')
    op.drop_table('match_candidates')
    op.drop_index('ix_feedback_events_match_result_id', table_name='feedback_events')
    op.drop_table('feedback_events')
    op.drop_index('uq_cases_case_number', table_name='cases')
    op.drop_index('uq_cases_active_provider_sanction', table_name='cases')
    op.drop_index('ix_cases_status_end_date', table_name='cases')
    op.drop_index('ix_cases_conflict_flag', table_name='cases')
    op.drop_table('cases')
    op.drop_index('uq_match_results_run_id_sanction_record_id', table_name='match_results')
    op.drop_index('ix_match_results_sanction_record_id', table_name='match_results')
    op.drop_index('ix_match_results_run_id_review_status', table_name='match_results')
    op.drop_index('ix_match_results_current', table_name='match_results')
    op.drop_table('match_results')
    op.drop_index('ix_ground_truth_scenario_tag', table_name='ground_truth')
    op.drop_index('ix_ground_truth_expected_provider_id', table_name='ground_truth')
    op.drop_table('ground_truth')
    op.drop_index('ix_eval_runs_strategy_corruption_level', table_name='eval_runs')
    op.drop_table('eval_runs')
    op.drop_index('uq_sanction_records_record_id', table_name='sanction_records')
    op.drop_index('ix_sanction_records_zip5_last_name', table_name='sanction_records')
    op.drop_index('ix_sanction_records_trigram_key_gin', table_name='sanction_records')
    op.drop_index('ix_sanction_records_state_dob', table_name='sanction_records')
    op.drop_index('ix_sanction_records_ordinal', table_name='sanction_records')
    op.drop_index('ix_sanction_records_npi', table_name='sanction_records')
    op.drop_index('ix_sanction_records_name_phonetic_state', table_name='sanction_records')
    op.drop_index('ix_sanction_records_name_norm', table_name='sanction_records')
    op.drop_index('ix_sanction_records_license_number_license_state', table_name='sanction_records')
    op.drop_index('ix_sanction_records_file_id', table_name='sanction_records')
    op.drop_table('sanction_records')
    op.drop_index('ix_reconciliation_runs_status', table_name='reconciliation_runs')
    op.drop_index('ix_reconciliation_runs_file_id', table_name='reconciliation_runs')
    op.drop_table('reconciliation_runs')
    op.drop_index('uq_sanction_files_sha256', table_name='sanction_files')
    op.drop_index('ix_sanction_files_status', table_name='sanction_files')
    op.drop_table('sanction_files')
    op.drop_index('uq_refresh_tokens_token_hash', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_provider_block_keys_provider_id', table_name='provider_block_keys')
    op.drop_index('ix_provider_block_keys_block_key', table_name='provider_block_keys')
    op.drop_table('provider_block_keys')
    op.drop_index('uq_column_mappings_source_authority_name', table_name='column_mappings')
    op.drop_table('column_mappings')
    op.drop_index('ix_audit_logs_entity_type_entity_id', table_name='audit_logs')
    op.drop_index('ix_audit_logs_created_at', table_name='audit_logs')
    op.drop_index('ix_audit_logs_actor_user_id', table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_index('uq_users_email_lower', table_name='users')
    op.drop_table('users')
    op.drop_index('uq_scoring_configs_version', table_name='scoring_configs')
    op.drop_table('scoring_configs')
    op.drop_index('uq_providers_provider_id', table_name='providers')
    op.drop_index('ix_providers_zip5_last_name', table_name='providers')
    op.drop_index('ix_providers_trigram_key_gin', table_name='providers')
    op.drop_index('ix_providers_state_dob', table_name='providers')
    op.drop_index('ix_providers_ordinal', table_name='providers')
    op.drop_index('ix_providers_npi', table_name='providers')
    op.drop_index('ix_providers_name_phonetic_state', table_name='providers')
    op.drop_index('ix_providers_name_norm', table_name='providers')
    op.drop_index('ix_providers_license_number_license_state', table_name='providers')
    op.drop_table('providers')
    op.drop_index('uq_llm_calls_cache_key', table_name='llm_calls')
    op.drop_table('llm_calls')
    op.drop_index('ix_jobs_status_run_after', table_name='jobs')
    op.drop_index('ix_jobs_kind', table_name='jobs')
    op.drop_table('jobs')
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
