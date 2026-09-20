"""stage9: the assistant's read-only role and the five views it may read

The assistant writes SQL from a question, so the database is its own second
opinion about what that SQL may touch. `concordance_assistant` owns nothing,
has `USAGE` on the schema and `SELECT` on five views - and on nothing else, so
a query that gets past the parser still cannot read `users`, `refresh_tokens`,
`audit_logs` or `llm_calls`.

The role has no password and cannot log in. A query runs as it through
`SET LOCAL ROLE` inside a read-only transaction, which needs no second
connection string and therefore no second secret to leak; the role is granted
to the application and owner roles so they can assume it. The transaction is
rolled back either way.

Views rather than table grants because a view is also a projection: no JSONB
column is exposed, so `sanction_records.raw` and `match_results.explanation` -
the two columns that carry whatever a source file said - cannot be read at all.

Revision ID: e8b3f1c62d94
Revises: d7a2c5e9f184
Create Date: 2026-09-20 09:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e8b3f1c62d94'
down_revision: str | Sequence[str] | None = 'd7a2c5e9f184'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "concordance_assistant"
APP_ROLE = "concordance_app"

VIEWS = {
    "assistant_matches": """
        SELECT
            r.id                        AS match_id,
            r.run_id                    AS run_id,
            r.decision                  AS decision,
            r.route                     AS route,
            r.calibrated_confidence     AS confidence,
            r.review_status             AS review_status,
            r.audit_sampled             AS audit_sampled,
            r.created_at                AS decided_at,
            r.reviewed_at               AS reviewed_at,
            r.chosen_provider_id        AS provider_id,
            r.approved_provider_id      AS approved_provider_id,
            s.record_id                 AS record_id,
            s.source_authority          AS source_authority,
            CASE WHEN s.is_organization
                 THEN s.organization_name
                 ELSE btrim(concat_ws(' ', s.first_name, s.middle_name, s.last_name, s.suffix))
            END                         AS subject_name,
            s.is_organization           AS is_organization,
            s.state                     AS state,
            s.sanction_type             AS sanction_type,
            s.exclusion_date            AS exclusion_date
        FROM match_results r
        JOIN sanction_records s ON s.id = r.sanction_record_id
        WHERE r.superseded_by IS NULL
    """,
    "assistant_cases": """
        SELECT
            c.id                        AS case_id,
            c.case_number               AS case_number,
            c.status                    AS status,
            CASE WHEN c.status = 'ACTIVE' AND c.start_date > CURRENT_DATE
                 THEN 'PENDING' ELSE c.status
            END                         AS phase,
            c.provider_id               AS provider_id,
            s.record_id                 AS record_id,
            c.start_date                AS start_date,
            c.end_date                  AS end_date,
            c.conflict_flag             AS conflict_flag,
            c.created_at                AS opened_at,
            CASE WHEN c.status = 'CLOSED' THEN c.updated_at END AS closed_at
        FROM cases c
        JOIN sanction_records s ON s.id = c.sanction_record_id
    """,
    "assistant_providers": """
        SELECT
            p.provider_id               AS provider_id,
            p.npi                       AS npi,
            CASE WHEN p.is_organization
                 THEN p.organization_name
                 ELSE btrim(concat_ws(' ', p.first_name, p.middle_name, p.last_name, p.suffix))
            END                         AS full_name,
            p.is_organization           AS is_organization,
            p.city                      AS city,
            p.state                     AS state,
            p.specialty                 AS specialty,
            p.status                    AS status
        FROM providers p
    """,
    "assistant_sanctions": """
        SELECT
            s.record_id                 AS record_id,
            s.source_authority          AS source_authority,
            CASE WHEN s.is_organization
                 THEN s.organization_name
                 ELSE btrim(concat_ws(' ', s.first_name, s.middle_name, s.last_name, s.suffix))
            END                         AS subject_name,
            s.is_organization           AS is_organization,
            s.npi                       AS npi,
            s.state                     AS state,
            s.sanction_type             AS sanction_type,
            s.exclusion_date            AS exclusion_date,
            s.reinstatement_date        AS reinstatement_date,
            s.created_at                AS ingested_at
        FROM sanction_records s
        WHERE s.is_current
    """,
    "assistant_runs": """
        SELECT
            run.id                      AS run_id,
            run.status                  AS status,
            run.strategy                AS strategy,
            cfg.version                 AS config_version,
            run.records_total           AS records_total,
            run.matched_count           AS matched_count,
            run.ambiguous_count         AS ambiguous_count,
            run.no_match_count          AS no_match_count,
            run.llm_calls               AS llm_calls,
            run.llm_cost_usd            AS llm_cost_usd,
            run.started_at              AS started_at,
            run.finished_at             AS finished_at
        FROM reconciliation_runs run
        LEFT JOIN scoring_configs cfg ON cfg.id = run.scoring_config_id
    """,
}


def upgrade() -> None:
    for name, body in VIEWS.items():
        op.execute(f"CREATE OR REPLACE VIEW {name} AS {body}")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regrole('{ROLE}') IS NULL THEN
                CREATE ROLE {ROLE} NOLOGIN NOINHERIT;
            END IF;

            -- Nothing but the schema and the views, and never CREATE.
            EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', '{ROLE}');
            EXECUTE format('REVOKE CREATE ON SCHEMA public FROM %I', '{ROLE}');
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', '{ROLE}');
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', '{ROLE}');
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', '{ROLE}');
        END
        $$;
        """
    )
    for name in VIEWS:
        op.execute(f"GRANT SELECT ON {name} TO {ROLE}")

    # Whoever runs queries must be able to assume the role. The owner runs the
    # migrations; the application role is what the API connects as.
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('GRANT {ROLE} TO %I', current_user);
            IF to_regrole('{APP_ROLE}') IS NOT NULL THEN
                EXECUTE format('GRANT {ROLE} TO %I', '{APP_ROLE}');
                EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', '{APP_ROLE}');
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for name in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regrole('{ROLE}') IS NOT NULL THEN
                EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', '{ROLE}');
                EXECUTE format('DROP ROLE %I', '{ROLE}');
            END IF;
        END
        $$;
        """
    )
