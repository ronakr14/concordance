"""Every mapped table, imported so `Base.metadata` is complete.

Alembic's autogenerate compares the database against `Base.metadata`. A model
module that nothing imports is a table that autogenerate proposes dropping, so
the import list here is load-bearing rather than tidy.
"""

from __future__ import annotations

from concordance.db.base import Base, metadata
from concordance.db.models.evaluation import EvalRun, FeedbackEvent, GroundTruth, LabSweep
from concordance.db.models.identity import RefreshToken, User
from concordance.db.models.jobs import Job
from concordance.db.models.master import (
    ColumnMapping,
    Provider,
    ProviderBlockKey,
    SanctionFile,
    SanctionRecord,
)
from concordance.db.models.matching import (
    ConfigActivation,
    LlmCall,
    MatchCandidate,
    MatchResult,
    ReconciliationRun,
    RunPattern,
    ScoringConfig,
)
from concordance.db.models.workflow import AuditLog, Case

__all__ = [
    "AuditLog",
    "Base",
    "Case",
    "ColumnMapping",
    "ConfigActivation",
    "EvalRun",
    "FeedbackEvent",
    "GroundTruth",
    "Job",
    "LabSweep",
    "LlmCall",
    "MatchCandidate",
    "MatchResult",
    "Provider",
    "ProviderBlockKey",
    "ReconciliationRun",
    "RefreshToken",
    "RunPattern",
    "SanctionFile",
    "SanctionRecord",
    "ScoringConfig",
    "User",
    "metadata",
]
