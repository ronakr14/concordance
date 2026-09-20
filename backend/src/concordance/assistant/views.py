"""The only tables the assistant may read: five views, and what is kept out.

The assistant answers questions by writing SQL. The guard in `guard.py` refuses
anything that names an object outside this list, and the read-only role the
query runs as has been granted nothing else - two independent answers to the
same question, because a whitelist that is only enforced in Python is one
parser bug away from being no whitelist at all.

**What is deliberately absent.** `users` and `refresh_tokens` (password hashes,
tokens), `audit_logs` (who looked at what, including actor ids), `llm_calls`
(raw prompts and responses), `jobs` and `column_mappings`. None of them answer
an analyst's question about sanctions, and all of them would widen what a
prompt-injected query could reach.

The views also flatten: no JSONB column is exposed, so a question cannot pull
`raw` off a sanction record or `explanation` off a result and read whatever a
source file happened to contain.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Views the assistant may read. Nothing else, and no bare table.
ALLOWED_VIEWS: frozenset[str] = frozenset(
    {
        "assistant_matches",
        "assistant_cases",
        "assistant_providers",
        "assistant_sanctions",
        "assistant_runs",
    }
)

#: The role the query executes as. It owns nothing and is granted SELECT on
#: exactly the views above (see the Stage 9 assistant migration).
READONLY_ROLE = "concordance_assistant"


@dataclass(frozen=True, slots=True)
class ViewDoc:
    """One view as the prompt describes it: what it holds, and its columns."""

    name: str
    about: str
    columns: tuple[tuple[str, str], ...]


#: What the model is told. Written by hand rather than read from
#: `information_schema`, because a column list without meanings produces SQL
#: that parses and answers the wrong question - and because this way the prompt
#: cannot grow a column the guard has not seen.
VIEWS: tuple[ViewDoc, ...] = (
    ViewDoc(
        name="assistant_matches",
        about=(
            "One row per engine decision about one sanction record, from the current "
            "(non-superseded) results only. The table to answer questions about what "
            "the engine decided, what a reviewer did with it, and how confident it was."
        ),
        columns=(
            ("match_id", "uuid of the decision; join key for nothing else here"),
            ("run_id", "uuid of the reconciliation run that produced it"),
            ("decision", "MATCH, AMBIGUOUS or NO_MATCH"),
            ("route", "deterministic, probabilistic or llm - which path decided it"),
            ("confidence", "calibrated probability the match is right, 0 to 1; null when there was no candidate"),
            ("review_status", "PENDING, APPROVED, REJECTED or ESCALATED"),
            ("audit_sampled", "true when the record was drawn into the random audit of auto-rejects"),
            ("decided_at", "when the engine decided it"),
            ("reviewed_at", "when a person actioned it, or null"),
            ("provider_id", "the provider the engine chose, or null"),
            ("approved_provider_id", "the provider a reviewer confirmed, or null"),
            ("record_id", "the sanction record's key in its source file"),
            ("source_authority", "which list the record came from, e.g. OIG-LEIE"),
            ("subject_name", "the sanctioned party's name as filed"),
            ("is_organization", "true for an organization, false for a person"),
            ("state", "two-letter state on the sanction record"),
            ("sanction_type", "the exclusion type as filed"),
            ("exclusion_date", "date of exclusion"),
        ),
    ),
    ViewDoc(
        name="assistant_cases",
        about="One row per compliance case opened on an approved match, with its lifecycle dates.",
        columns=(
            ("case_id", "uuid"),
            ("case_number", "human-readable case number"),
            ("status", "ACTIVE, EXPIRED, CLOSED or REJECTED"),
            ("phase", "derived: PENDING before it starts, otherwise the status"),
            ("provider_id", "the provider the case is about"),
            ("record_id", "the sanction record it was opened on"),
            ("start_date", "when the exclusion period starts"),
            ("end_date", "when it ends"),
            ("conflict_flag", "true when a later run disagreed with the decision it was opened on"),
            ("opened_at", "when the case was created"),
            ("closed_at", "when it was closed, or null"),
        ),
    ),
    ViewDoc(
        name="assistant_providers",
        about="The provider master, one row per provider. No contact details beyond city and state.",
        columns=(
            ("provider_id", "the provider's key"),
            ("npi", "National Provider Identifier, 10 digits"),
            ("full_name", "person's name, or the organization's"),
            ("is_organization", "true for an organization"),
            ("city", "city"),
            ("state", "two-letter state"),
            ("specialty", "taxonomy or specialty as filed"),
            ("status", "ACTIVE, INACTIVE or RETIRED"),
        ),
    ),
    ViewDoc(
        name="assistant_sanctions",
        about="Current sanction records as ingested, one row each, whether or not they were matched.",
        columns=(
            ("record_id", "the record's key in its source file"),
            ("source_authority", "which list it came from"),
            ("subject_name", "the sanctioned party's name as filed"),
            ("is_organization", "true for an organization"),
            ("npi", "NPI as filed, often missing or a placeholder"),
            ("state", "two-letter state"),
            ("sanction_type", "exclusion type"),
            ("exclusion_date", "date of exclusion"),
            ("reinstatement_date", "date of reinstatement, or null"),
            ("ingested_at", "when the record was loaded"),
        ),
    ),
    ViewDoc(
        name="assistant_runs",
        about="One row per reconciliation run: its outcome counts, what decided it, and what it cost.",
        columns=(
            ("run_id", "uuid"),
            ("status", "QUEUED, RUNNING, COMPLETED, FAILED or CANCELLED"),
            ("strategy", "deterministic, fuzzy, probabilistic or probabilistic_llm"),
            ("config_version", "the scoring config version that decided the run"),
            ("records_total", "records reconciled"),
            ("matched_count", "decisions of MATCH"),
            ("ambiguous_count", "decisions sent to the grey band"),
            ("no_match_count", "decisions of NO_MATCH"),
            ("llm_calls", "adjudication calls made"),
            ("llm_cost_usd", "what those calls cost"),
            ("started_at", "when it started"),
            ("finished_at", "when it finished, or null"),
        ),
    ),
)


def schema_prompt() -> str:
    """The schema as the model is told it: the whitelist, and nothing else."""
    blocks: list[str] = []
    for view in VIEWS:
        columns = "\n".join(f"  {name} - {about}" for name, about in view.columns)
        blocks.append(f"{view.name}\n  {view.about}\n{columns}")
    return "\n\n".join(blocks)


__all__ = ["ALLOWED_VIEWS", "READONLY_ROLE", "VIEWS", "ViewDoc", "schema_prompt"]
