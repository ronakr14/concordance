"""ORM rows to response models, where the mapping is more than field-for-field.

Most responses are `Model.model_validate(row)`. These are the ones that are not:
a sanction record whose `dob` is the text the file wrote rather than the parsed
date, a queue row that joins a result to its record, the Investigation detail
that gathers five tables. Kept here so each shape is built one way, wherever it
is returned from.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from concordance.api import schemas
from concordance.db.models import (
    Case,
    ColumnMapping,
    LlmCall,
    MatchResult,
    Provider,
    SanctionFile,
    SanctionRecord,
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
    )


def case_detail(session: Session, case: Case) -> schemas.CaseDetailOut:
    provider = ProviderRepository(session).get(case.provider_id)
    record = session.get(SanctionRecord, case.sanction_record_id)
    match = session.get(MatchResult, case.match_result_id) if case.match_result_id else None
    base = schemas.CaseOut.model_validate(case).model_dump()
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


def audit_out(row: Any) -> schemas.AuditOut:
    return schemas.AuditOut(
        id=row.id,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        actor_user_id=row.actor_user_id,
        actor_role=row.actor_role,
        before=row.before,
        after=row.after,
        request_id=row.request_id,
        ip=str(row.ip) if row.ip is not None else None,
        created_at=row.created_at,
    )


__all__ = [
    "audit_out",
    "case_detail",
    "file_out",
    "match_detail",
    "match_list_item",
    "provider_brief",
    "record_detail",
    "record_out",
    "subject_name",
]
