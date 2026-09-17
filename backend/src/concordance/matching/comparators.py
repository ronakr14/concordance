"""Comparison vectors: two records reduced to a fixed tuple of agreement levels.

Fellegi-Sunter needs a *discrete* observation per field. A raw similarity float
would make the EM step intractable - there is no finite table of levels to
estimate `m` and `u` over - so every field is collapsed to one of a handful of
ordinal levels, and the similarity functions live here, inside that collapse,
rather than being a stage of their own.

Three rules govern the level tables, and all three are load-bearing:

1. **`MISSING` is always its own level.** Never folded into `DISAGREE`. With
   90%-corrupt data most of the accuracy comes from that distinction: an absent
   date of birth says nothing, a *different* date of birth is strong evidence
   against the pair, and a model that cannot tell them apart produces confident
   false negatives.
2. **Levels are ordinal with stable integer codes**, ascending in agreement,
   `MISSING = 0`. The EM tables index on the integer, and a fitted config from
   last week has to keep meaning the same thing this week.
3. **Individuals and organizations have separate vectors** (PLAN 11.2). Running
   an organization through the individual vector would record
   `first_name: MISSING`, `dob: MISSING` for every one of them, teaching the fit
   that those levels are common and draining the weight of a real agreement for
   everybody else.

The identifier fields - NPI, EIN - have no `MISSING` level by design. Their
level table already distinguishes *uninformative* (`BOTH_INVALID`,
`ONE_INVALID`) from *contradictory* (`VALID_DISAGREE`), which is the same
distinction expressed in the vocabulary the identifier actually has.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from concordance.matching.normalization import (
    NormalizedAddress,
    NormalizedRecord,
    PartialDate,
    normalize_org_name,
    org_acronym,
    org_significant_tokens,
)
from concordance.matching.npi_validator import NpiStatus

JW_HIGH = 0.92
JW_LOW = 0.85
TOKEN_SET_HIGH = 0.90


class ModelKind(StrEnum):
    """Which of the two models a pair belongs to."""

    INDIVIDUAL = "individual"
    ORGANIZATION = "organization"


# --------------------------------------------------------------------------
# level tables
# --------------------------------------------------------------------------
#
# Ordinal, ascending in agreement, MISSING = 0. The spec's tables are written
# strongest-first; these are the same tables read from the other end, so that a
# larger integer always means more agreement and the reliability plots read the
# way a human expects.


class LastNameLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    JW_LOW = 2  # Jaro-Winkler >= 0.85
    JW_HIGH = 3  # Jaro-Winkler >= 0.92
    PHONETIC = 4  # different spelling, same Double Metaphone key
    EXACT = 5


class FirstNameLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    JW_LOW = 2
    INITIAL = 3  # one side reduced to an initial, consistent with the other
    NICKNAME = 4  # Bob/Robert - equal only after the nickname table
    EXACT = 5


class DobLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    YEAR_ONLY = 2
    YEAR_MONTH = 3
    TRANSPOSED = 4  # month and day exchanged: the US/ISO ambiguity
    EXACT = 5


class AddressLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    SAME_ZIP_ONLY = 2
    TOKEN_SET = 3  # token-set similarity >= 0.90
    SAME_STREET_DIFF_UNIT = 4  # same building, different suite
    EXACT = 5


class StateLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    EXACT = 2


class ZipLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    ZIP3 = 2  # same sectional centre, different delivery area
    ZIP5 = 3


class LicenseLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    EXACT_DIFF_STATE = 2  # multi-state licensure, or one side lost the state
    EXACT_SAME_STATE = 3


class IdentifierLevel(IntEnum):
    """NPI and EIN. No MISSING: BOTH_INVALID already means "no evidence"."""

    BOTH_INVALID = 0
    ONE_INVALID = 1
    VALID_DISAGREE = 2
    VALID_EXACT = 3


class LegalNameLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    JW_LOW = 2
    ACRONYM = 3  # RFPG against Riverside Family Practice Group
    TOKEN_SET = 4
    EXACT = 5


class DbaLevel(IntEnum):
    MISSING = 0
    DISAGREE = 1
    TOKEN_SET = 2
    EXACT = 3


# --------------------------------------------------------------------------
# similarity primitives
# --------------------------------------------------------------------------


def jaro_winkler(a: str, b: str) -> float:
    """Similarity in [0, 1]. Empty against anything is 0, not 1."""
    if not a or not b:
        return 0.0
    return float(JaroWinkler.similarity(a, b))


def token_set(a: str, b: str) -> float:
    """Order-insensitive, duplicate-insensitive token similarity in [0, 1]."""
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b)) / 100.0


# --------------------------------------------------------------------------
# per-field comparators
# --------------------------------------------------------------------------


def compare_last_name(a: NormalizedRecord, b: NormalizedRecord) -> LastNameLevel:
    left, right = a.last_norm, b.last_norm
    if not left or not right:
        return LastNameLevel.MISSING
    if left == right:
        return LastNameLevel.EXACT
    # The phonetic keys are a tuple because Double Metaphone returns a primary
    # and an alternate reading; a hit on either is a hit.
    if a.phonetic_keys and b.phonetic_keys and set(a.phonetic_keys) & set(b.phonetic_keys):
        return LastNameLevel.PHONETIC
    similarity = jaro_winkler(left, right)
    if similarity >= JW_HIGH:
        return LastNameLevel.JW_HIGH
    if similarity >= JW_LOW:
        return LastNameLevel.JW_LOW
    return LastNameLevel.DISAGREE


def compare_first_name(a: NormalizedRecord, b: NormalizedRecord) -> FirstNameLevel:
    raw_a, raw_b = a.first_raw or a.first_norm, b.first_raw or b.first_norm
    if not raw_a or not raw_b:
        return FirstNameLevel.MISSING
    if raw_a == raw_b:
        return FirstNameLevel.EXACT
    # first_norm has already been through the nickname table, so an agreement
    # here that was not an agreement above is exactly a nickname equivalence.
    if a.first_norm and a.first_norm == b.first_norm:
        return FirstNameLevel.NICKNAME
    if (len(raw_a) == 1 or len(raw_b) == 1) and raw_a[0] == raw_b[0]:
        return FirstNameLevel.INITIAL
    if jaro_winkler(raw_a, raw_b) >= JW_LOW:
        return FirstNameLevel.JW_LOW
    return FirstNameLevel.DISAGREE


def compare_partial_dates(left: PartialDate, right: PartialDate) -> DobLevel:
    """Graded date agreement. A year alone is weak evidence, not no evidence."""
    if not left or not right or left.year is None or right.year is None:
        return DobLevel.MISSING
    if left == right:
        return DobLevel.EXACT
    if left.complete and right.complete and left.swapped() == right:
        return DobLevel.TRANSPOSED
    if left.year != right.year:
        return DobLevel.DISAGREE
    if left.month is not None and left.month == right.month:
        # Same year and month. A day present on both sides and differing is a
        # real disagreement about the day, but the year-and-month agreement is
        # still evidence - which is why the level exists rather than collapsing
        # into DISAGREE.
        return DobLevel.YEAR_MONTH
    if left.month is None or right.month is None:
        return DobLevel.YEAR_ONLY
    return DobLevel.DISAGREE


def compare_dob(a: NormalizedRecord, b: NormalizedRecord) -> DobLevel:
    return compare_partial_dates(a.dob, b.dob)


def compare_addresses(left: NormalizedAddress, right: NormalizedAddress) -> AddressLevel:
    if not left or not right:
        return AddressLevel.MISSING
    if left.po_box and right.po_box:
        return AddressLevel.EXACT if left.po_box == right.po_box else AddressLevel.DISAGREE
    if not left.line or not right.line:
        # One side is a PO box and the other a street address, or a street line
        # went missing entirely. The ZIP is all that is left to go on.
        if left.zip5 and left.zip5 == right.zip5:
            return AddressLevel.SAME_ZIP_ONLY
        return AddressLevel.MISSING
    if left.line == right.line:
        if left.unit == right.unit:
            return AddressLevel.EXACT
        # A dropped suite number is the commonest address corruption there is
        # and says nothing about identity; a *different* suite says a little.
        return AddressLevel.SAME_STREET_DIFF_UNIT
    if token_set(left.line, right.line) >= TOKEN_SET_HIGH:
        return AddressLevel.TOKEN_SET
    if left.zip5 and left.zip5 == right.zip5:
        return AddressLevel.SAME_ZIP_ONLY
    return AddressLevel.DISAGREE


def compare_address(a: NormalizedRecord, b: NormalizedRecord) -> AddressLevel:
    return compare_addresses(a.address, b.address)


def compare_state(a: NormalizedRecord, b: NormalizedRecord) -> StateLevel:
    left, right = a.address.state, b.address.state
    if not left or not right:
        return StateLevel.MISSING
    return StateLevel.EXACT if left == right else StateLevel.DISAGREE


def compare_zip(a: NormalizedRecord, b: NormalizedRecord) -> ZipLevel:
    left, right = a.address.zip5, b.address.zip5
    if not left or not right:
        return ZipLevel.MISSING
    if left == right:
        return ZipLevel.ZIP5
    if left[:3] == right[:3]:
        return ZipLevel.ZIP3
    return ZipLevel.DISAGREE


def compare_license(a: NormalizedRecord, b: NormalizedRecord) -> LicenseLevel:
    left, right = a.license_number, b.license_number
    if not left or not right:
        return LicenseLevel.MISSING
    if left != right:
        return LicenseLevel.DISAGREE
    if a.license_state and b.license_state and a.license_state == b.license_state:
        return LicenseLevel.EXACT_SAME_STATE
    return LicenseLevel.EXACT_DIFF_STATE


def _compare_identifier(
    left_valid: bool, right_valid: bool, left: str, right: str
) -> IdentifierLevel:
    """Shared shape for NPI and EIN: validity first, equality second.

    A sentinel or a checksum failure is *not* a disagreement. Treating it as
    one is how a system produces a confident false negative, because a
    disagreeing identifier is the strongest single piece of evidence there is.
    """
    if left_valid and right_valid:
        return IdentifierLevel.VALID_EXACT if left == right else IdentifierLevel.VALID_DISAGREE
    if left_valid or right_valid:
        return IdentifierLevel.ONE_INVALID
    return IdentifierLevel.BOTH_INVALID


def compare_npi(a: NormalizedRecord, b: NormalizedRecord) -> IdentifierLevel:
    return _compare_identifier(
        a.npi_status is NpiStatus.VALID, b.npi_status is NpiStatus.VALID, a.npi, b.npi
    )


def compare_ein(a: NormalizedRecord, b: NormalizedRecord) -> IdentifierLevel:
    return _compare_identifier(bool(a.ein), bool(b.ein), a.ein, b.ein)


def _acronym_equivalent(a: NormalizedRecord, b: NormalizedRecord) -> bool:
    """RFPG against Riverside Family Practice Group, in either direction.

    Token-set similarity scores that pair near zero, so without this level the
    acronym scenario is unreachable however well the model is fitted.

    One side must actually be *written* as an acronym. Comparing the two sides'
    derived initials to each other was tried and is far too loose: "Riverside
    Family Practice Group" and "Redwood Family Physicians Group" both reduce to
    RFPG, as does any typo of either, so the level would fire on organizations
    that share nothing but their first letters. The blocking index may make that
    comparison - over-proposing candidates is cheap - but the comparator, which
    is evidence, may not.
    """
    for short, long in ((a, b), (b, a)):
        compact = short.org_name_norm.replace(" ", "")
        if not compact or len(compact) > 8 or " " in short.org_name_norm.strip():
            # Multi-token names are not acronyms, however short they are.
            continue
        if compact in (long.org_acronym_key, long.dba_acronym_key):
            return True
    return False


def compare_legal_name(a: NormalizedRecord, b: NormalizedRecord) -> LegalNameLevel:
    left, right = a.org_name_norm, b.org_name_norm
    if not left or not right:
        return LegalNameLevel.MISSING
    if left == right:
        return LegalNameLevel.EXACT
    if _acronym_equivalent(a, b):
        return LegalNameLevel.ACRONYM
    if token_set(left, right) >= TOKEN_SET_HIGH:
        return LegalNameLevel.TOKEN_SET
    if jaro_winkler(left, right) >= JW_LOW:
        return LegalNameLevel.JW_LOW
    return LegalNameLevel.DISAGREE


def compare_dba(a: NormalizedRecord, b: NormalizedRecord) -> DbaLevel:
    """Compare across both name slots: either side may file either name.

    An organization legitimately has a legal name and a trading name, and the
    source file picks one without saying which, so a trading name on either side
    is compared against both of the other side's names.

    What is deliberately *not* compared is legal name against legal name. That
    pairing is what `legal_name` already measures, and including it here would
    let a completely different trading name score `EXACT` on the strength of the
    legal names agreeing - one piece of evidence counted twice, and counted
    under the name of a field that never saw it.
    """
    if not a.dba_name_norm and not b.dba_name_norm:
        return DbaLevel.MISSING
    pairs: list[tuple[str, str]] = []
    if a.dba_name_norm:
        pairs.extend((a.dba_name_norm, n) for n in (b.dba_name_norm, b.org_name_norm) if n)
    if b.dba_name_norm:
        pairs.extend((n, b.dba_name_norm) for n in (a.dba_name_norm, a.org_name_norm) if n)
    if not pairs:
        return DbaLevel.MISSING
    best = DbaLevel.DISAGREE
    for left, right in pairs:
        if left == right:
            return DbaLevel.EXACT
        if token_set(left, right) >= TOKEN_SET_HIGH:
            best = DbaLevel.TOKEN_SET
    return best


# --------------------------------------------------------------------------
# vector assembly
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One column of the comparison vector."""

    name: str
    levels: type[IntEnum]
    compare: Callable[[NormalizedRecord, NormalizedRecord], IntEnum]

    @property
    def n_levels(self) -> int:
        return len(self.levels)

    def level_name(self, code: int) -> str:
        return self.levels(code).name


INDIVIDUAL_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("npi", IdentifierLevel, compare_npi),
    FieldSpec("last_name", LastNameLevel, compare_last_name),
    FieldSpec("first_name", FirstNameLevel, compare_first_name),
    FieldSpec("dob", DobLevel, compare_dob),
    FieldSpec("address", AddressLevel, compare_address),
    FieldSpec("state", StateLevel, compare_state),
    FieldSpec("zip", ZipLevel, compare_zip),
    FieldSpec("license", LicenseLevel, compare_license),
)

ORGANIZATION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("npi", IdentifierLevel, compare_npi),
    FieldSpec("ein", IdentifierLevel, compare_ein),
    FieldSpec("legal_name", LegalNameLevel, compare_legal_name),
    FieldSpec("dba_alias", DbaLevel, compare_dba),
    FieldSpec("address", AddressLevel, compare_address),
    FieldSpec("state", StateLevel, compare_state),
    FieldSpec("zip", ZipLevel, compare_zip),
)

FIELDS_BY_KIND: dict[ModelKind, tuple[FieldSpec, ...]] = {
    ModelKind.INDIVIDUAL: INDIVIDUAL_FIELDS,
    ModelKind.ORGANIZATION: ORGANIZATION_FIELDS,
}

# A comparison vector is a tuple of level codes in exactly this order: fixed
# length, fixed order, every time. The EM tables and the serialized config both
# index positionally, so a reordering here silently invalidates every config
# ever fitted - which is why the order lives in one place.
FIELD_NAMES: dict[ModelKind, tuple[str, ...]] = {
    kind: tuple(f.name for f in fields) for kind, fields in FIELDS_BY_KIND.items()
}

LEVEL_COUNTS: dict[ModelKind, tuple[int, ...]] = {
    kind: tuple(f.n_levels for f in fields) for kind, fields in FIELDS_BY_KIND.items()
}

ComparisonVector = tuple[int, ...]


def compare(a: NormalizedRecord, b: NormalizedRecord, kind: ModelKind) -> ComparisonVector:
    """The comparison vector for one pair under one model."""
    return tuple(int(spec.compare(a, b)) for spec in FIELDS_BY_KIND[kind])


def describe(vector: ComparisonVector, kind: ModelKind) -> dict[str, str]:
    """A vector as `{field: LEVEL_NAME}` - for logs, prompts and the UI."""
    return {
        spec.name: spec.level_name(code)
        for spec, code in zip(FIELDS_BY_KIND[kind], vector, strict=True)
    }


def pair_kind(a: NormalizedRecord, b: NormalizedRecord) -> ModelKind | None:
    """Which model scores this pair, or `None` when the two sides disagree.

    A `None` is never scored silently: `scorer.py` coerces the pair to the
    organization model and records that it did, because a source calling an
    organization a person is a scenario in its own right (PLAN 11.2) and
    dropping the pair would make that scenario unmatchable by construction.
    """
    if a.is_organization and b.is_organization:
        return ModelKind.ORGANIZATION
    if not a.is_organization and not b.is_organization:
        return ModelKind.INDIVIDUAL
    return None


def as_organization(record: NormalizedRecord) -> NormalizedRecord:
    """An individual-shaped record re-read as an organization.

    Used only for cross-type pairs. The person name becomes the legal name, so
    "Riverside Family Practice" filed in the surname column can still meet the
    provider master's organization row.
    """
    if record.is_organization:
        return record
    org_norm, legal_suffix = normalize_org_name(record.name_norm)
    return replace(
        record,
        is_organization=True,
        org_name_norm=org_norm,
        org_legal_suffix=legal_suffix,
        org_tokens=org_significant_tokens(org_norm),
        org_acronym_key=org_acronym(org_norm),
    )
