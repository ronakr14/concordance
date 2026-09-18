"""ORM rows to response models, where the mapping is more than field-for-field.

Most responses are `Model.model_validate(row)`. These are the ones that are not:
a sanction record whose `dob` is the text the file wrote rather than the parsed
date, a queue row that joins a result to its record, the Investigation detail
that gathers five tables. Kept here so each shape is built one way, wherever it
is returned from.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.api import schemas
from concordance.db.enums import CaseStatus
from concordance.db.models import (
    Case,
    ColumnMapping,
    LlmCall,
    MatchResult,
    Provider,
    ReconciliationRun,
    SanctionFile,
    SanctionRecord,
    ScoringConfig,
    User,
)
from concordance.db.repositories.matches import MatchRepository
from concordance.db.repositories.providers import ProviderRepository


def record_out(row: SanctionRecord) -> schemas.SanctionRecordOut:
    return schemas.SanctionRecordOut.model_validate(_record_fields(row, detail=False))


def record_detail(row: SanctionRecord) -> schemas.SanctionRecordDetailOut:
    return schemas.SanctionRecordDetailOut.model_validate(_record_fields(row, detail=True))


def _record_fields(row: SanctionRecord, *, detail: bool) -> dict[str, Any]:
    names = set(schemas.SanctionRecordOut.model_fields)
    if detail:
        names |= set(schemas.SanctionRecordDetailOut.model_fields)
    out = {name: getattr(row, name) for name in names if name not in ("dob", "raw")}
    # The date of birth as the file wrote it. The parsed DATE column is null
    # for `08-24-57`, and showing a reviewer nothing where the file said
    # something is showing them less than the evidence.
    out["dob"] = row.dob_raw or (row.dob.isoformat() if row.dob else None)
    if detail:
        out["raw"] = dict(row.raw or {})
    return out


def subject_name(row: SanctionRecord) -> str:
    if row.is_organization:
        return row.organization_name or ""
    parts = [row.first_name, row.middle_name, row.last_name, row.suffix]
    return " ".join(p for p in parts if p)


def match_list_item(result: MatchResult, record: SanctionRecord) -> schemas.MatchListItemOut:
    base = schemas.MatchOut.model_validate(result).model_dump()
    return schemas.MatchListItemOut(
        **base,
        record_id=record.record_id,
        source_authority=record.source_authority,
        subject_name=subject_name(record),
        is_organization=record.is_organization,
        state=record.state,
        sanction_type=record.sanction_type,
    )


def provider_brief(row: Provider) -> schemas.ProviderBriefOut:
    return schemas.ProviderBriefOut.model_validate(row)


def match_detail(session: Session, result: MatchResult) -> schemas.MatchDetailOut:
    candidates = MatchRepository(session).candidates_for(result.id)
    providers = ProviderRepository(session).get_many([c.provider_id for c in candidates])
    record = session.get(SanctionRecord, result.sanction_record_id)
    if record is None:  # the FK cascades, so a result always has its record
        raise LookupError(f"match {result.id} has no sanction record")
    cases = session.scalars(
        select(Case)
        .where(Case.match_result_id == result.id)
        .order_by(Case.created_at.desc())
    ).all()
    # Live cases opened on an earlier result that this one contradicts (Q5).
    conflicting = session.scalars(
        select(Case)
        .where(Case.conflict_match_result_id == result.id, Case.status == str(CaseStatus.ACTIVE))
        .order_by(Case.created_at.desc())
    ).all()

    llm: dict[str, Any] | None = None
    if result.llm_call_id is not None:
        call = session.get(LlmCall, result.llm_call_id)
        if call is not None:
            llm = {
                "provider": call.provider,
                "model": call.model,
                "prompt_version": call.prompt_version,
                "latency_ms": call.latency_ms,
                "prompt_tokens": call.prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "cost_usd": float(call.cost_usd or 0),
                "response": call.response,
            }

    base = schemas.MatchOut.model_validate(result).model_dump()
    return schemas.MatchDetailOut(
        **base,
        explanation=dict(result.explanation or {}),
        candidates=[
            schemas.CandidateOut(
                provider_id=c.provider_id,
                rank=c.rank,
                field_levels=dict(c.field_levels or {}),
                field_weights={k: float(v) for k, v in (c.field_weights or {}).items()},
                match_weight=c.match_weight,
                posterior=c.posterior,
                blocking_keys=c.blocking_keys if c.blocking_keys is not None else [],
                provider=provider_brief(providers[c.provider_id])
                if c.provider_id in providers
                else None,
            )
            for c in candidates
        ],
        sanction_record=record_detail(record),
        llm=llm,
        cases=[schemas.CaseOut.model_validate(c) for c in cases],
        conflicting_cases=[schemas.CaseOut.model_validate(c) for c in conflicting],
        band=_band(session, result),
        adjudication=_adjudication(result, candidates, llm),
        reviewed_by_email=user_emails(session, [result.reviewed_by]).get(result.reviewed_by)
        if result.reviewed_by
        else None,
    )


def _adjudication(
    result: MatchResult, candidates: list[Any], llm: dict[str, Any] | None
) -> schemas.AdjudicationOut | None:
    """The adjudicator's answer, parsed from the stored response and re-checked.

    The pipeline keeps the model's raw text, not a parsed copy, so this runs the
    same `validate()` the pipeline ran - against the candidates actually stored
    - rather than trusting a second parser. An answer that fails validation was
    discarded by the pipeline too, and is not shown as if it had counted.
    """
    if result.route != "llm" or llm is None:
        return None
    content = (llm.get("response") or {}).get("content")
    if not isinstance(content, str):
        return None
    from concordance.llm.schema import validate

    checked = validate(
        content,
        allowed_provider_ids={c.provider_id for c in candidates},
        supplied_evidence={f"{c.provider_id}.{name}" for c in candidates for name in (c.field_levels or {})},
    )
    if checked.adjudication is None:
        return None
    answer = checked.adjudication
    return schemas.AdjudicationOut(
        decision=answer.decision,
        provider_id=answer.provider_id,
        confidence=answer.confidence,
        evidence_cited=list(answer.evidence_cited),
        reasoning=answer.reasoning,
    )


def _band(session: Session, result: MatchResult) -> schemas.BandOut | None:
    """The thresholds of the config that decided this result - not today's config.

    A result is explained by the arithmetic that produced it. If the model has
    been retuned since, the current thresholds would place this confidence in a
    band it was never judged against.
    """
    run = session.get(ReconciliationRun, result.run_id)
    config = session.get(ScoringConfig, run.scoring_config_id) if run and run.scoring_config_id else None
    if config is None:
        return None
    return schemas.BandOut(
        scoring_config_id=config.id,
        version=config.version,
        t_auto_accept=config.t_auto_accept,
        t_auto_reject=config.t_auto_reject,
    )


def case_detail(session: Session, case: Case) -> schemas.CaseDetailOut:
    provider = ProviderRepository(session).get(case.provider_id)
    record = session.get(SanctionRecord, case.sanction_record_id)
    match = session.get(MatchResult, case.match_result_id) if case.match_result_id else None
    emails = user_emails(session, [case.created_by, case.closed_by])
    base = _case_item(case, provider, record, emails).model_dump()
    return schemas.CaseDetailOut(
        **base,
        provider=provider_brief(provider) if provider else None,
        sanction_record=record_out(record) if record else None,
        match=schemas.MatchOut.model_validate(match) if match else None,
    )


def file_out(session: Session, file: SanctionFile) -> schemas.SanctionFileOut:
    mapping = session.get(ColumnMapping, file.mapping_id) if file.mapping_id else None
    summary: dict[str, Any] | None = None
    if file.notes:
        try:
            summary = json.loads(file.notes)
        except ValueError:
            summary = {"notes": file.notes}
    return schemas.SanctionFileOut(
        id=file.id,
        filename=file.filename,
        sha256=file.sha256,
        status=file.status,
        source_authority=file.source_authority,
        row_count=file.row_count,
        uploaded_at=file.uploaded_at,
        uploaded_by=file.uploaded_by,
        mapping_id=file.mapping_id,
        mapping_name=mapping.name if mapping else None,
        summary=summary,
    )


def audit_out(row: Any, emails: dict[uuid.UUID, str] | None = None) -> schemas.AuditOut:
    return schemas.AuditOut(
        id=row.id,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        actor_user_id=row.actor_user_id,
        actor_email=(emails or {}).get(row.actor_user_id) if row.actor_user_id else None,
        actor_role=row.actor_role,
        before=row.before,
        after=row.after,
        request_id=row.request_id,
        ip=str(row.ip) if row.ip is not None else None,
        created_at=row.created_at,
    )


def audit_rows(session: Session, rows: list[Any]) -> list[schemas.AuditOut]:
    """A page of audit rows, each naming its actor. One user query per page."""
    emails = user_emails(session, [r.actor_user_id for r in rows])
    return [audit_out(r, emails) for r in rows]


def user_emails(session: Session, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """Email by user id, for the ids given. A deleted user is simply absent."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = session.execute(select(User.id, User.email).where(User.id.in_(wanted)))
    return dict(rows.tuples().all())


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------


def provider_name(row: Provider | None) -> str | None:
    if row is None:
        return None
    if row.is_organization:
        return row.organization_name
    parts = [row.first_name, row.middle_name, row.last_name, row.suffix]
    return " ".join(p for p in parts if p) or None


def case_items(session: Session, cases: list[Case]) -> list[schemas.CaseListItemOut]:
    """Case rows with names, in three queries whatever the page size."""
    providers = ProviderRepository(session).get_many(list({c.provider_id for c in cases}))
    record_ids = list({c.sanction_record_id for c in cases})
    records = (
        {r.id: r for r in session.scalars(select(SanctionRecord).where(SanctionRecord.id.in_(record_ids)))}
        if record_ids
        else {}
    )
    emails = user_emails(session, [u for c in cases for u in (c.created_by, c.closed_by)])
    return [_case_item(c, providers.get(c.provider_id), records.get(c.sanction_record_id), emails) for c in cases]


def _case_item(
    case: Case,
    provider: Provider | None,
    record: SanctionRecord | None,
    emails: dict[uuid.UUID, str],
) -> schemas.CaseListItemOut:
    base = schemas.CaseOut.model_validate(case).model_dump()
    return schemas.CaseListItemOut(
        **base,
        provider_name=provider_name(provider),
        subject_name=subject_name(record) if record else None,
        sanction_type=record.sanction_type if record else None,
        source_authority=record.source_authority if record else None,
        created_by_email=emails.get(case.created_by) if case.created_by else None,
        closed_by_email=emails.get(case.closed_by) if case.closed_by else None,
    )


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


def provider_item(row: Provider, compliance: str) -> schemas.ProviderListItemOut:
    brief = schemas.ProviderBriefOut.model_validate(row).model_dump()
    return schemas.ProviderListItemOut(**brief, compliance_status=compliance)  # type: ignore[arg-type]


def provider_detail(session: Session, row: Provider) -> schemas.ProviderDetailOut:
    repo = ProviderRepository(session)
    item = provider_item(row, repo.compliance_of(row.provider_id)).model_dump()
    ranked = repo.ranked_in(row.provider_id)
    return schemas.ProviderDetailOut(
        **item,
        cases=[schemas.CaseOut.model_validate(c) for c in repo.cases_for(row.provider_id)],
        matches=[
            schemas.ProviderMatchOut(
                **match_list_item(result, record).model_dump(),
                candidate_rank=candidate.rank,
                candidate_posterior=candidate.posterior,
            )
            for result, record, candidate in ranked
        ],
    )


__all__ = [
    "audit_out",
    "audit_rows",
    "case_detail",
    "case_items",
    "file_out",
    "match_detail",
    "match_list_item",
    "provider_brief",
    "provider_detail",
    "provider_item",
    "provider_name",
    "record_detail",
    "record_out",
    "subject_name",
    "user_emails",
]
