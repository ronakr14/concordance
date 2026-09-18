"""Provider master reads. Written only by the loader, so there is no create.

A provider's compliance status is derived, never stored. It is a function of
the cases and results that name the provider, and a stored copy would be one
more thing a re-run, an expiry or an approval had to remember to update - and
the one it forgot would show a reviewer the wrong answer. Derived in SQL, it is
right by construction:

- `EXCLUDED` - the provider holds an `ACTIVE` case.
- `UNDER_REVIEW` - the provider is the engine's choice on a current result
  that proposes a match and that no reviewer has decided yet.
- `CLEAR` - neither.

Both tests are `EXISTS` probes on indexed columns (`ix_cases_provider_id`, the
partial `ix_match_results_current_chosen_provider`), so a page of fifty rows
costs a hundred index lookups rather than a join over every result ever made.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ColumnElement, case, exists, func, or_, select
from sqlalchemy.orm import Session

from concordance.db.enums import CaseStatus, Decision, ReviewStatus
from concordance.db.models import Case, MatchCandidate, MatchResult, Provider, SanctionRecord
from concordance.db.repositories.base import Page, paginate, paginate_rows
from concordance.matching.normalization import tokens

ComplianceStatus = Literal["EXCLUDED", "UNDER_REVIEW", "CLEAR"]

#: Results a reviewer still owes a verdict on.
OPEN_REVIEW = (str(ReviewStatus.PENDING), str(ReviewStatus.ESCALATED))
#: Decisions that propose a provider. `NO_MATCH` names nobody.
PROPOSING = (str(Decision.MATCH), str(Decision.AMBIGUOUS))

_NPI = re.compile(r"^\d{10}$")


def _excluded() -> ColumnElement[bool]:
    return exists().where(
        Case.provider_id == Provider.provider_id, Case.status == str(CaseStatus.ACTIVE)
    )


def _under_review() -> ColumnElement[bool]:
    return exists().where(
        MatchResult.chosen_provider_id == Provider.provider_id,
        MatchResult.superseded_by.is_(None),
        MatchResult.decision.in_(PROPOSING),
        MatchResult.review_status.in_(OPEN_REVIEW),
    )


def compliance_column() -> ColumnElement[str]:
    return case(
        (_excluded(), "EXCLUDED"),
        (_under_review(), "UNDER_REVIEW"),
        else_="CLEAR",
    ).label("compliance_status")


@dataclass(frozen=True, slots=True)
class ProviderFilter:
    query: str | None = None
    state: str | None = None
    specialty: str | None = None
    is_organization: bool | None = None
    compliance: ComplianceStatus | None = None
    sort: Literal["provider_id", "name", "state"] = "provider_id"
    descending: bool = False


class ProviderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, provider_id: str) -> Provider | None:
        return self.session.scalar(select(Provider).where(Provider.provider_id == provider_id))

    def get_many(self, provider_ids: list[str]) -> dict[str, Provider]:
        """One query for N ids. The Investigation view needs a whole candidate set."""
        if not provider_ids:
            return {}
        rows = self.session.scalars(select(Provider).where(Provider.provider_id.in_(provider_ids)))
        return {p.provider_id: p for p in rows}

    def by_npi(self, npi: str) -> list[Provider]:
        return list(self.session.scalars(select(Provider).where(Provider.npi == npi)))

    def search(
        self, query: str | None = None, *, limit: int | None = None, offset: int = 0
    ) -> Page[Provider]:
        """Name or id search, providers only. The directory uses `directory()`."""
        stmt = select(Provider).order_by(Provider.ordinal)
        if query:
            stmt = stmt.where(_matches(query))
        return paginate(self.session, stmt, limit, offset)

    def directory(
        self, where: ProviderFilter, *, limit: int | None = None, offset: int = 0
    ) -> Page[tuple[Provider, str]]:
        """The provider directory: each row beside its derived compliance status."""
        status = compliance_column()
        stmt = select(Provider, status)
        if where.query:
            stmt = stmt.where(_matches(where.query))
        if where.state:
            stmt = stmt.where(Provider.state == where.state.upper())
        if where.specialty:
            stmt = stmt.where(Provider.specialty == where.specialty)
        if where.is_organization is not None:
            stmt = stmt.where(Provider.is_organization.is_(where.is_organization))
        if where.compliance == "EXCLUDED":
            stmt = stmt.where(_excluded())
        elif where.compliance == "UNDER_REVIEW":
            stmt = stmt.where(~_excluded(), _under_review())
        elif where.compliance == "CLEAR":
            stmt = stmt.where(~_excluded(), ~_under_review())

        key: Any = {
            "provider_id": Provider.ordinal,
            "name": Provider.name_norm,
            "state": Provider.state,
        }[where.sort]
        ordered = key.desc().nulls_last() if where.descending else key.asc().nulls_last()
        # Ordinal breaks ties so a page boundary is stable across requests.
        stmt = stmt.order_by(ordered, Provider.ordinal)
        return paginate_rows(self.session, stmt, limit, offset)

    def compliance_of(self, provider_id: str) -> ComplianceStatus:
        status = self.session.scalar(
            select(compliance_column()).where(Provider.provider_id == provider_id)
        )
        return status or "CLEAR"  # type: ignore[return-value]

    def cases_for(self, provider_id: str) -> list[Case]:
        return list(
            self.session.scalars(
                select(Case)
                .where(Case.provider_id == provider_id)
                .order_by(Case.created_at.desc(), Case.id)
            )
        )

    def ranked_in(
        self, provider_id: str, *, limit: int = 50
    ) -> list[tuple[MatchResult, SanctionRecord, MatchCandidate]]:
        """Every result that ranked this provider, newest first, history included.

        Driven from `match_candidates.provider_id`, which is indexed, rather
        than from the results: a provider appears in a handful of candidate
        lists and there are tens of thousands of results.
        """
        rows = self.session.execute(
            select(MatchResult, SanctionRecord, MatchCandidate)
            .join(MatchCandidate, MatchCandidate.match_result_id == MatchResult.id)
            .join(SanctionRecord, SanctionRecord.id == MatchResult.sanction_record_id)
            .where(MatchCandidate.provider_id == provider_id)
            .order_by(MatchResult.created_at.desc(), MatchResult.id)
            .limit(limit)
        ).all()
        return [(r, s, c) for r, s, c in rows]

    def count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(Provider)) or 0)


def _matches(query: str) -> ColumnElement[bool]:
    """A directory search term, as an indexed predicate.

    Ten digits is an NPI and is looked up by equality. Anything else is tried
    as an exact provider id, and as a substring of the normalized name - the
    same folding the engine matches on, so "o'brien" finds "O'Brien", and a
    substring search the trigram GIN index on `name_norm` can serve. A leading
    wildcard on a b-tree column would read all fifty thousand rows.
    """
    term = query.strip()
    if _NPI.match(term):
        return Provider.npi == term
    folded = " ".join(tokens(term))
    clauses: list[ColumnElement[bool]] = [Provider.provider_id == term.upper()]
    if folded:
        # `_` survives folding and is a LIKE wildcard, hence autoescape.
        clauses.append(Provider.name_norm.contains(folded, autoescape=True))
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


__all__ = ["ComplianceStatus", "ProviderFilter", "ProviderRepository", "compliance_column"]
