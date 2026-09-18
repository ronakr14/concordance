"""The case lifecycle: opening one, and letting one expire (Q3).

A case is a confirmed match under review for a fixed window - three months by
default. Two transitions live here.

**Opening.** A case is opened against the match result that justified it, and
that result is never edited afterwards, so "what did we see when we opened
this" stays answerable even after a later run disagrees.

**Expiring.** The window closing is a state change, and a state change nobody
recorded is indistinguishable from someone quietly editing the row. So every
transition writes an audit entry with `actor_user_id = NULL` and
`actor_role = 'system'`: the log says the scheduler did it, not a person, and
the two can never be confused when the audit trail is read back.

The job is idempotent by construction - it selects `ACTIVE` cases whose
`end_date` has passed, and a second run in the same day finds none, because the
first run left them `EXPIRED`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from concordance.audit import service as audit
from concordance.audit.service import Actor
from concordance.db.enums import CaseStatus
from concordance.db.models import Case
from concordance.db.repositories.audit import AuditRepository
from concordance.db.repositories.cases import CaseRepository
from concordance.errors import ConflictError, InvalidError, NotFoundError
from concordance.logging_setup import get_logger

log = get_logger("cases.lifecycle")

#: The actor recorded for anything the scheduler did. Not a user id, because
#: there is no user, and inventing one would make the audit trail lie.
SYSTEM_ACTOR = "system"


@dataclass
class ExpiryReport:
    expired: int = 0
    case_ids: list[str] = field(default_factory=list)
    today: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"expired": self.expired, "today": self.today, "case_ids": self.case_ids[:50]}


def expire_cases(session: Session, *, today: date | None = None) -> ExpiryReport:
    """Move every `ACTIVE` case past its `end_date` to `EXPIRED`, with an audit row each."""
    day = today or datetime.now(UTC).date()
    cases = CaseRepository(session)
    audit = AuditRepository(session)
    report = ExpiryReport(today=day.isoformat())

    for case in cases.due_for_expiry(day):
        before = {"status": case.status, "end_date": case.end_date.isoformat()}
        case.status = str(CaseStatus.EXPIRED)
        audit.record(
            action="case.expired",
            entity_type="case",
            entity_id=str(case.id),
            actor_user_id=None,
            actor_role=SYSTEM_ACTOR,
            before=before,
            after={"status": str(CaseStatus.EXPIRED), "expired_on": day.isoformat()},
        )
        report.expired += 1
        report.case_ids.append(str(case.id))

    if report.expired:
        log.info("cases.expired", count=report.expired, today=report.today)
    return report


def next_case_number(session: Session, *, today: date | None = None) -> str:
    """`CASE-2026-000123`. Sequential within the year, which is what a reviewer quotes."""
    day = today or datetime.now(UTC).date()
    prefix = f"CASE-{day.year}-"
    used = int(
        session.scalar(select(func.count()).select_from(Case).where(Case.case_number.like(f"{prefix}%")))
        or 0
    )
    return f"{prefix}{used + 1:06d}"


def open_case(
    session: Session,
    *,
    provider_id: str,
    sanction_record_id: uuid.UUID,
    match_result_id: uuid.UUID | None = None,
    duration_months: int = 3,
    created_by: uuid.UUID | None = None,
    start: date | None = None,
    actor: Actor | None = None,
    strict: bool = False,
) -> Case:
    """Open a review case, or return the active one that already exists.

    The partial unique index allows one active case per provider and record, so
    a duplicate approval must find the existing case rather than collide with
    it - the idempotency Stage 7 needs on `approve`. `strict` refuses instead,
    for `POST /cases`, where asking for a second live case is the caller's
    mistake and should be reported as one.
    """
    if duration_months < 1:
        raise InvalidError(
            "a case lasts at least one month", details={"duration_months": "must be >= 1"}
        )
    if actor is not None:
        created_by = actor.user_id
    cases = CaseRepository(session)
    existing = cases.active_for(provider_id, sanction_record_id)
    if existing is not None:
        if strict:
            raise ConflictError(
                f"case {existing.case_number} is already active for this provider and record",
                code="case_already_active",
                details={"case_id": str(existing.id), "case_number": existing.case_number},
            )
        return existing

    begin = start or datetime.now(UTC).date()
    case = Case(
        case_number=next_case_number(session, today=begin),
        provider_id=provider_id,
        sanction_record_id=sanction_record_id,
        match_result_id=match_result_id,
        status=str(CaseStatus.ACTIVE),
        start_date=begin,
        end_date=_add_months(begin, duration_months),
        duration_months=duration_months,
        created_by=created_by,
    )
    session.add(case)
    session.flush()
    if actor is None:
        # A caller that passed only a user id: the user did it, in a role this
        # function was not told. A caller that passed nothing: the system did.
        actor = Actor(user_id=created_by, role="user") if created_by else Actor.system()
    audit.record(
        session,
        actor,
        "case.opened",
        entity_type="case",
        entity_id=case.id,
        after={
            "case_number": case.case_number,
            "provider_id": provider_id,
            "sanction_record_id": sanction_record_id,
            "match_result_id": match_result_id,
            "start_date": case.start_date,
            "end_date": case.end_date,
            "duration_months": duration_months,
        },
    )
    return case


def close_case(session: Session, actor: Actor, case_id: uuid.UUID, *, reason: str) -> Case:
    """Close an active case, with a reason. Admin-only is enforced by the caller."""
    case = session.get(Case, case_id, with_for_update=True)
    if case is None:
        raise NotFoundError(f"no case {case_id}")
    if case.status != CaseStatus.ACTIVE:
        raise ConflictError(
            f"case {case.case_number} is {case.status}; only an active case can be closed",
            code="case_not_active",
            details={"status": case.status},
        )
    reason = reason.strip()
    if not reason:
        raise InvalidError("say why the case is being closed", details={"reason": "required"})

    before = {"status": case.status, "conflict_flag": case.conflict_flag}
    case.status = str(CaseStatus.CLOSED)
    case.closed_by = actor.user_id
    case.close_reason = reason
    audit.record(
        session,
        actor,
        "case.closed",
        entity_type="case",
        entity_id=case.id,
        before=before,
        after={"status": case.status, "reason": reason},
    )
    return case


def _add_months(start: date, months: int) -> date:
    """Calendar months, clamped to the end of the target month.

    Three months from 31 May is 31 August, and three months from 30 November is
    28 or 29 February - not a crash, which is what naive day arithmetic gives.
    """
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


__all__ = [
    "SYSTEM_ACTOR",
    "ExpiryReport",
    "close_case",
    "expire_cases",
    "next_case_number",
    "open_case",
]
