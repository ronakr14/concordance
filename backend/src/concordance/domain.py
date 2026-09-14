"""Plain domain objects.

Storage-agnostic by design: no ORM, no pandas, no pyarrow. Every layer of the
system — the Parquet store today, the Postgres store at Stage 5 — hands these
same objects to the matching engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    """Expected or produced decision for a sanction record."""

    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True, slots=True)
class Provider:
    """A row of the provider master file.

    Individuals carry name and DOB; organizations (``is_organization``) carry
    ``organization_name``, ``dba_name`` and ``ein`` instead, and leave the
    person fields empty (PLAN 11.2).
    """

    provider_id: str
    npi: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    suffix: str | None = None
    dob: date | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    license_number: str | None = None
    license_state: str | None = None
    specialty: str | None = None
    organization_name: str | None = None
    dba_name: str | None = None
    ein: str | None = None
    is_organization: bool = False
    status: str = "ACTIVE"

    def full_name(self) -> str:
        if self.is_organization:
            return self.organization_name or ""
        parts = [self.first_name, self.middle_name, self.last_name, self.suffix]
        return " ".join(p for p in parts if p)


@dataclass(frozen=True, slots=True)
class SanctionRecord:
    """One exclusion/sanction row, after column mapping has been applied.

    ``raw`` keeps every source column verbatim, including ones the mapping did
    not claim (PLAN 11.1).
    """

    record_id: str
    source_authority: str | None = None
    npi: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    suffix: str | None = None
    dob: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    license_number: str | None = None
    license_state: str | None = None
    specialty: str | None = None
    organization_name: str | None = None
    dba_name: str | None = None
    ein: str | None = None
    is_organization: bool = False
    sanction_type: str | None = None
    exclusion_date: date | None = None
    reinstatement_date: date | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def full_name(self) -> str:
        if self.is_organization:
            return self.organization_name or ""
        parts = [self.first_name, self.middle_name, self.last_name, self.suffix]
        return " ".join(p for p in parts if p)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A provider proposed for comparison against one sanction record.

    ``blocking_keys`` records why the candidate surfaced, which is what makes a
    recall failure debuggable rather than mysterious.
    """

    provider_id: str
    blocking_keys: tuple[str, ...] = ()

    def merged_with(self, other: Candidate) -> Candidate:
        if other.provider_id != self.provider_id:
            raise ValueError("cannot merge candidates for different providers")
        keys = dict.fromkeys(self.blocking_keys + other.blocking_keys)
        return replace(self, blocking_keys=tuple(keys))


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """The answer key for one sanction record.

    ``corruption_profile`` holds every corruption the generator applied plus,
    for ambiguous records, the full set of plausible provider ids.
    """

    sanction_record_id: str
    expected_outcome: Outcome
    expected_provider_id: str | None = None
    scenario_tag: str = ""
    corruption_profile: dict[str, Any] = field(default_factory=dict)
