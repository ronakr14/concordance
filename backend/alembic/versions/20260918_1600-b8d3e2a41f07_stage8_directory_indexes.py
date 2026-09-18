"""stage8: indexes behind the provider directory

Two indexes, both for `GET /providers`, which otherwise reads every provider.

**`ix_providers_name_norm_gin`** - the directory searches names by substring,
and `LIKE '%term%'` cannot use the existing b-tree on `name_norm`. A trigram GIN
index can. `pg_trgm` is already installed by the initial migration.

**`ix_match_results_current_chosen_provider`** - a provider's derived compliance
status probes "is this provider the engine's choice on a current, undecided
result" for every row of a directory page. Partial on `superseded_by IS NULL`,
like the other queue indexes, because superseded results never count.

Revision ID: b8d3e2a41f07
Revises: 7cafb841bdb1
Create Date: 2026-09-18 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8d3e2a41f07'
down_revision: str | Sequence[str] | None = '7cafb841bdb1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        'ix_providers_name_norm_gin',
        'providers',
        ['name_norm'],
        unique=False,
        postgresql_using='gin',
        postgresql_ops={'name_norm': 'gin_trgm_ops'},
    )
    op.create_index(
        'ix_match_results_current_chosen_provider',
        'match_results',
        ['chosen_provider_id'],
        unique=False,
        postgresql_where=sa.text('superseded_by IS NULL'),
    )


def downgrade() -> None:
    op.drop_index(
        'ix_match_results_current_chosen_provider',
        table_name='match_results',
        postgresql_where=sa.text('superseded_by IS NULL'),
    )
    op.drop_index(
        'ix_providers_name_norm_gin',
        table_name='providers',
        postgresql_using='gin',
        postgresql_ops={'name_norm': 'gin_trgm_ops'},
    )
