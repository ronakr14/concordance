"""Request and response models. No endpoint returns a bare dict.

Two reasons this file exists rather than each router shaping its own JSON. The
OpenAPI schema is the contract the Stage 8 client is generated from, and a
response typed as `dict` documents nothing. And a response model is also a
filter: `UserOut` cannot leak `password_hash`, because it has nowhere to put it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from concordance.db.enums import CasePhase, case_phase


class ApiModel(BaseModel):
    """Shared configuration: read straight off ORM objects, refuse unknown fields."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Page[T](BaseModel):
    """The one pagination envelope, used by every list endpoint."""

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


class RegisterIn(ApiModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)
    full_name: str | None = Field(default=None, max_length=200)
    role: str = Field(default="analyst", pattern="^(analyst|admin)$")


class LoginIn(ApiModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    transport: Literal["body", "cookie"] = Field(
        default="body",
        description=(
            "`cookie` sets the refresh token as an httpOnly cookie and leaves it out of "
            "the body, so browser script never holds it. `body` is for the CLI and tests."
        ),
    )


class RefreshIn(ApiModel):
    refresh_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Omit to use the refresh cookie a `cookie` login set.",
    )


class TokenOut(ApiModel):
    access_token: str
    refresh_token: str | None = Field(
        default=None, description="Null when the token travels in the refresh cookie."
    )
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(ApiModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------
# sanctions and files
# --------------------------------------------------------------------------


class ColumnProfileOut(ApiModel):
    """One column as the inspector found it."""

    name: str
    non_empty: int
    samples: list[str]
    proposed_field: str | None = None
    confidence: float = 0.0


class InspectionOut(ApiModel):
    """What the first phase of an upload returns (Q1). Nothing is ingested yet."""

    file_id: uuid.UUID
    filename: str
    sha256: str
    rows: int
    status: str
    columns: list[ColumnProfileOut]
    proposed_mapping: dict[str, str] = Field(
        description="`{canonical_field: column header}`. Confirm or edit, then commit.",
        examples=[{"last_name": "LASTNAME", "first_name": "FIRSTNAME", "npi": "NPI"}],
    )
    proposal_source: str = Field(description="`stored` when a saved mapping fit this file.")
    mapping_id: uuid.UUID | None = None
    source_authority: str | None = None
    unmapped_required: list[str] = Field(default_factory=list)
    canonical_fields: list[str]


class CommitIn(ApiModel):
    mapping: dict[str, str] = Field(
        description="`{canonical_field: column header}`. Needs last_name or organization_name.",
        examples=[{"last_name": "LASTNAME", "first_name": "FIRSTNAME", "npi": "NPI", "state": "STATE"}],
    )
    source_authority: str | None = Field(default=None, max_length=100, examples=["OIG-LEIE"])
    save_as_default: bool = False
    mapping_name: str | None = Field(default=None, max_length=100)


class RowIssueOut(ApiModel):
    row: int
    reason: str
    field: str | None = None


class CommitOut(ApiModel):
    file_id: uuid.UUID
    status: str
    mapping_id: uuid.UUID
    source_authority: str
    accepted: int = Field(description="Rows that parsed into records.")
    new: int = Field(description="Records not seen before.")
    replaced: int = Field(description="New versions of records already on file.")
    unchanged: int = Field(description="Rows identical to the current version; not re-inserted.")
    inserted: int
    rejected: int
    warnings: int
    rejected_rows: list[RowIssueOut]
    warning_rows: list[RowIssueOut]


class SanctionFileOut(ApiModel):
    id: uuid.UUID
    filename: str
    sha256: str
    status: str
    source_authority: str | None
    row_count: int
    uploaded_at: datetime
    uploaded_by: uuid.UUID | None
    mapping_id: uuid.UUID | None
    mapping_name: str | None = None
    summary: dict[str, Any] | None = None


class SanctionRecordOut(ApiModel):
    id: uuid.UUID
    record_id: str
    file_id: uuid.UUID | None
    source_authority: str | None
    is_current: bool
    npi: str | None
    first_name: str | None
    middle_name: str | None
    last_name: str | None
    organization_name: str | None
    is_organization: bool
    dob: str | None = Field(default=None, description="As the file wrote it.")
    city: str | None
    state: str | None
    zip: str | None
    sanction_type: str | None
    exclusion_date: date | None
    reinstatement_date: date | None


class SanctionRecordDetailOut(SanctionRecordOut):
    suffix: str | None
    dba_name: str | None
    address_line1: str | None
    address_line2: str | None
    license_number: str | None
    license_state: str | None
    specialty: str | None
    ein: str | None
    replaced_by: uuid.UUID | None
    replaced_at: datetime | None
    raw: dict[str, Any] = Field(description="The row exactly as uploaded, unmapped columns included.")


class ColumnMappingIn(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    source_authority: str = Field(min_length=1, max_length=100)
    mapping: dict[str, str]
    is_default: bool = False


class ColumnMappingOut(ApiModel):
    id: uuid.UUID
    name: str
    source_authority: str
    mapping: dict[str, str]
    is_default: bool
    created_by: uuid.UUID | None
    created_at: datetime


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------


class RunIn(ApiModel):
    strategy: str = Field(
        default="probabilistic",
        max_length=30,
        description="deterministic, fuzzy, probabilistic, or probabilistic_llm.",
    )
    scoring_config_id: uuid.UUID | None = Field(
        default=None, description="Defaults to the most recently fitted config."
    )
    file_id: uuid.UUID | None = Field(
        default=None,
        description="Reconcile one committed file. Omitted: every current sanction record.",
    )
    limit: int | None = Field(default=None, ge=1, le=1_000_000)
    max_candidates: int | None = Field(default=None, ge=1, le=1000)


class RunOut(ApiModel):
    id: uuid.UUID
    status: str
    strategy: str
    file_id: uuid.UUID | None
    job_id: int | None
    triggered_by: uuid.UUID | None
    engine_version: str | None
    scoring_config_id: uuid.UUID | None
    prompt_version: str | None
    provider_snapshot_hash: str | None
    sanction_snapshot_hash: str | None
    records_total: int
    matched_count: int
    ambiguous_count: int
    no_match_count: int
    llm_calls: int
    llm_tokens: int
    llm_cost_usd: float
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    error: str | None


# --------------------------------------------------------------------------
# matches
# --------------------------------------------------------------------------


class ProviderBriefOut(ApiModel):
    """The provider side of a comparison, as the Investigation screen shows it."""

    provider_id: str
    npi: str | None
    first_name: str | None
    middle_name: str | None
    last_name: str | None
    suffix: str | None
    organization_name: str | None
    dba_name: str | None
    ein: str | None
    is_organization: bool
    dob: date | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    zip: str | None
    license_number: str | None
    license_state: str | None
    specialty: str | None
    status: str


class CandidateOut(ApiModel):
    provider_id: str
    rank: int
    field_levels: dict[str, Any] = Field(description="Agreement level per compared field.")
    field_weights: dict[str, float] = Field(
        description="Each field's contribution to the match weight, in bits."
    )
    match_weight: float
    posterior: float
    blocking_keys: list[str] | dict[str, Any]
    provider: ProviderBriefOut | None = None


class MatchOut(ApiModel):
    id: uuid.UUID
    run_id: uuid.UUID
    sanction_record_id: uuid.UUID
    decision: str
    chosen_provider_id: str | None
    approved_provider_id: str | None
    posterior: float | None
    calibrated_confidence: float | None
    raw_match_weight: float | None
    route: str
    review_status: str
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    reviewer_comment: str | None
    superseded_by: uuid.UUID | None
    created_at: datetime


class MatchListItemOut(MatchOut):
    """A queue row: the decision and enough of the record to recognise it."""

    record_id: str
    source_authority: str | None
    subject_name: str
    is_organization: bool
    state: str | None
    sanction_type: str | None


class MatchDetailOut(MatchOut):
    """Everything the Investigation screen renders."""

    explanation: dict[str, Any] = Field(
        description="Reason code, notes, and the adjudicator's reasoning and evidence when an LLM decided."
    )
    candidates: list[CandidateOut]
    sanction_record: SanctionRecordDetailOut
    llm: dict[str, Any] | None = Field(
        default=None, description="Provider, model and prompt version of the adjudication call."
    )
    cases: list[CaseOut] = Field(default_factory=list)
    conflicting_cases: list[CaseOut] = Field(
        default_factory=list,
        description="Live cases opened on an earlier result that this result disagrees with (Q5).",
    )
    band: BandOut | None = Field(
        default=None, description="The thresholds of the scoring config that decided this result."
    )
    reviewed_by_email: str | None = None
    adjudication: AdjudicationOut | None = Field(
        default=None,
        description="The LLM's answer, re-validated from the stored response, when an LLM decided.",
    )


class AdjudicationOut(ApiModel):
    """What the adjudicator said, after the same checks the pipeline ran on it.

    `evidence_cited` entries are `provider_id.field` - each names a field of a
    candidate the model was shown, which the Investigation page highlights.
    """

    decision: str
    provider_id: str | None
    confidence: float
    evidence_cited: list[str]
    reasoning: str


class BandOut(ApiModel):
    """Where the accept / grey / reject boundaries sat for the run that decided a result.

    Both thresholds are on the calibrated confidence: at or above
    `t_auto_accept` the engine matches, below `t_auto_reject` it rejects, and
    the grey band between is where the LLM adjudicates.
    """

    scoring_config_id: uuid.UUID
    version: str
    t_auto_accept: float
    t_auto_reject: float


class BulkIn(ApiModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    action: Literal["reject", "escalate"] = Field(
        description="Approval is deliberately absent: it opens a case, one record at a time."
    )
    comment: str = Field(min_length=1, max_length=2000)


class BulkItemOut(ApiModel):
    id: uuid.UUID
    ok: bool
    review_status: str | None = None
    error: ErrorBodyOut | None = None


class BulkOut(ApiModel):
    succeeded: int
    failed: int
    results: list[BulkItemOut]


class ErrorBodyOut(ApiModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ReviewIn(ApiModel):
    comment: str | None = Field(default=None, max_length=2000)
    provider_id: str | None = Field(
        default=None,
        max_length=64,
        description="Required to approve an ambiguous match; must be one of its candidates.",
    )
    duration_months: int | None = Field(
        default=None, ge=1, le=60, description="Case length on approval; default 3."
    )
    start_date: date | None = None


class CommentIn(ApiModel):
    comment: str = Field(min_length=1, max_length=2000)
    provider_id: str | None = Field(default=None, max_length=64)


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------


class CaseIn(ApiModel):
    match_result_id: uuid.UUID
    duration_months: int | None = Field(
        default=None, ge=1, le=60, description="Defaults to DEFAULT_CASE_MONTHS (3)."
    )
    start_date: date | None = None


class CaseOut(ApiModel):
    id: uuid.UUID
    case_number: str
    provider_id: str
    sanction_record_id: uuid.UUID
    match_result_id: uuid.UUID | None
    status: str
    start_date: date
    end_date: date
    duration_months: int
    created_by: uuid.UUID | None
    closed_by: uuid.UUID | None
    close_reason: str | None
    conflict_flag: bool
    conflict_match_result_id: uuid.UUID | None
    created_at: datetime

    @computed_field(  # type: ignore[prop-decorator]
        description="`status` as a person reads it: an `ACTIVE` case whose window has "
        "not begun is `PENDING`. Derived today, never stored."
    )
    @property
    def phase(self) -> CasePhase:
        return case_phase(self.status, self.start_date, datetime.now(UTC).date())


class CaseListItemOut(CaseOut):
    """A case row with the names a person reads, not only the ids."""

    provider_name: str | None
    subject_name: str | None
    sanction_type: str | None
    source_authority: str | None
    created_by_email: str | None
    closed_by_email: str | None


class CaseDetailOut(CaseListItemOut):
    provider: ProviderBriefOut | None
    sanction_record: SanctionRecordOut | None
    match: MatchOut | None


class CloseCaseIn(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)


class ApprovalOut(ApiModel):
    match: MatchOut
    case: CaseOut
    case_created: bool = Field(description="False when an active case already covered this pair.")


# --------------------------------------------------------------------------
# audit and stats
# --------------------------------------------------------------------------


class AuditOut(ApiModel):
    id: int
    action: str
    entity_type: str
    entity_id: str
    actor_user_id: uuid.UUID | None
    actor_email: str | None = None
    actor_role: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
    ip: str | None = None
    created_at: datetime


class KpiOut(ApiModel):
    providers: int
    sanction_records: int
    matched: int
    ambiguous: int
    no_match: int
    pending_review: int
    escalated: int
    approved: int
    rejected: int
    cases_pending: int
    cases_active: int
    cases_expired: int
    cases_closed: int
    conflicts: int


class BucketOut(ApiModel):
    label: str
    count: int


class VolumePointOut(ApiModel):
    period: str
    runs: int
    records: int
    matched: int
    llm_cost_usd: float


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


class ProviderListItemOut(ProviderBriefOut):
    compliance_status: Literal["EXCLUDED", "UNDER_REVIEW", "CLEAR"] = Field(
        description=(
            "`EXCLUDED`: an active case. `UNDER_REVIEW`: the engine's choice on a current "
            "result a reviewer has not decided. `CLEAR`: neither."
        )
    )


class ProviderMatchOut(MatchListItemOut):
    """A decision in which this provider was a candidate."""

    candidate_rank: int
    candidate_posterior: float


class ProviderDetailOut(ProviderListItemOut):
    cases: list[CaseOut]
    matches: list[ProviderMatchOut] = Field(
        description="Results that ranked this provider, newest first, superseded ones included."
    )


class FacetsOut(ApiModel):
    """Distinct values the filter bars offer, over current sanction records."""

    sanction_types: list[str]
    source_authorities: list[str]
    states: list[str]


class HealthOut(ApiModel):
    status: str
    version: str
    database: bool


# --------------------------------------------------------------------------
# lab
# --------------------------------------------------------------------------


class LabSweepIn(ApiModel):
    levels: list[float] | None = Field(
        default=None,
        max_length=10,
        description="Corruption levels, 0.0 to 0.9. Omitted: all ten.",
        examples=[[0.0, 0.3, 0.6, 0.9]],
    )
    providers: int | None = Field(default=None, ge=1_000, le=200_000)
    sanctions: int | None = Field(default=None, ge=100, le=50_000)
    seed: int | None = None


class LabLlmIn(ApiModel):
    sweep_id: uuid.UUID | None = Field(
        default=None, description="The sweep to extend. Omitted: the newest completed one."
    )
    levels: list[float] | None = Field(
        default=None, max_length=10, description="Omitted: 0.3, 0.5 and 0.7."
    )
    sample: int | None = Field(
        default=None, ge=10, le=1_000, description="Records sampled per stratum. Omitted: 100."
    )


class LabRunOut(ApiModel):
    """One Lab experiment. `progress` is levels for a sweep, model calls for an LLM run."""

    id: uuid.UUID
    kind: str
    status: str
    parent_id: uuid.UUID | None
    job_id: int | None
    params: dict[str, Any]
    progress: dict[str, Any]
    summary: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ReliabilityBinOut(ApiModel):
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_frequency: float


class CalibrationMetricsOut(ApiModel):
    n: int
    ece: float | None
    mce: float | None
    brier: float | None
    bins: list[ReliabilityBinOut]


class LabCalibrationOut(ApiModel):
    """The fit's holdout, before and after isotonic calibration, at one level."""

    level: float | None
    model: str
    before: CalibrationMetricsOut
    after: CalibrationMetricsOut
    t_auto_accept: float | None
    t_auto_reject: float | None
    target_precision: float | None
    achieved_precision: float | None
    grey_band_fraction: float | None
    n_holdout: int | None


class LabScenarioOut(ApiModel):
    scenario: str
    n: int
    accuracy: float | None
    precision: float | None
    recall: float | None
    f1: float | None


class LabCellOut(ApiModel):
    """One strategy at one corruption level."""

    level: float | None
    strategy: str
    precision: float | None
    recall: float | None
    f1: float | None
    accuracy: float | None
    ambiguous_accuracy: float | None
    wrong_provider: int | None
    false_positives: int
    false_negatives: int
    ece: float | None
    brier: float | None
    blocking_recall: float | None
    grey_band_fraction: float | None
    records: int | None
    scenarios: list[LabScenarioOut]


class LlmIntervalOut(ApiModel):
    precision: list[float]
    recall: list[float]
    f1: list[float]


class LlmStrategyOut(ApiModel):
    precision: float
    recall: float
    f1: float
    review: float
    true_positives: float
    false_positives: float
    exact: bool = Field(description="True where nothing was estimated.")
    interval: LlmIntervalOut | None = Field(
        default=None, description="95% stratified-bootstrap interval."
    )


class LlmSpendOut(ApiModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    tokens: int
    usd: float


class LlmCostOut(ApiModel):
    price_model: str
    routed: LlmSpendOut
    everything: LlmSpendOut
    saving: dict[str, float | None]
    tokens_per_call: dict[str, dict[str, float]]


class LlmPopulationOut(ApiModel):
    records: int
    no_candidates: int
    grey: int
    decided: int
    expected_matches: int
    cells: dict[str, int] = Field(
        default_factory=dict, description="Records per sampling cell: stratum x truly matches."
    )


class LlmSampleOut(ApiModel):
    grey: int
    decided: int
    failed: int
    cells: dict[str, int] = Field(
        default_factory=dict, description="Answered calls per sampling cell."
    )
    min_answered: int | None = None
    withheld: list[str] = Field(
        default_factory=list,
        description="Strategies not estimated at this level: too few of their calls answered.",
    )
    live_calls: int
    cache_hits: int
    seconds: float


class LabLlmLevelOut(ApiModel):
    """Routed versus LLM-on-everything at one level, measured on a sample."""

    level: float | None
    sample_per_stratum: int
    population: LlmPopulationOut
    sample: LlmSampleOut
    strategies: dict[str, LlmStrategyOut]
    cost: LlmCostOut
    notes: list[str]
    config_id: str | None


class LabPriceOut(ApiModel):
    model: str
    prompt_per_million: float
    completion_per_million: float
    placeholder: bool


class LabResultsOut(ApiModel):
    sweep: LabRunOut | None = Field(description="The sweep drawn: the newest completed one by default.")
    llm_run: LabRunOut | None = Field(description="The LLM experiment extending that sweep, if any.")
    live: LabRunOut | None = Field(description="Whichever experiment is queued or running now.")
    cells: list[LabCellOut]
    calibration: list[LabCalibrationOut]
    llm: list[LabLlmLevelOut]
    price: LabPriceOut
    llm_enabled: bool


MatchDetailOut.model_rebuild()
BulkItemOut.model_rebuild()

__all__ = [name for name in dir() if name[:1].isupper()]
