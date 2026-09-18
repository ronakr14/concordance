"""Request and response models. No endpoint returns a bare dict.

Two reasons this file exists rather than each router shaping its own JSON. The
OpenAPI schema is the contract the Stage 8 client is generated from, and a
response typed as `dict` documents nothing. And a response model is also a
filter: `UserOut` cannot leak `password_hash`, because it has nowhere to put it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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


class RefreshIn(ApiModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenOut(ApiModel):
    access_token: str
    refresh_token: str
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
    organization_name: str | None
    is_organization: bool
    dob: date | None
    address_line1: str | None
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


class CaseDetailOut(CaseOut):
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


class HealthOut(ApiModel):
    status: str
    version: str
    database: bool


MatchDetailOut.model_rebuild()

__all__ = [name for name in dir() if name[:1].isupper()]
