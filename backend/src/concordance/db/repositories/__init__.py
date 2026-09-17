"""One repository per aggregate. Each takes a `Session` and never opens one."""

from __future__ import annotations

from concordance.db.repositories.audit import AuditRepository
from concordance.db.repositories.base import Page, clamp_limit, paginate
from concordance.db.repositories.cases import CaseRepository
from concordance.db.repositories.evaluation import EvalRepository
from concordance.db.repositories.jobs import JobRepository
from concordance.db.repositories.matches import MatchRepository
from concordance.db.repositories.providers import ProviderRepository
from concordance.db.repositories.sanctions import SanctionRepository
from concordance.db.repositories.users import UserRepository

__all__ = [
    "AuditRepository",
    "CaseRepository",
    "EvalRepository",
    "JobRepository",
    "MatchRepository",
    "Page",
    "ProviderRepository",
    "SanctionRepository",
    "UserRepository",
    "clamp_limit",
    "paginate",
]
