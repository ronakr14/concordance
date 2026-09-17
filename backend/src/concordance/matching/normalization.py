"""Normalization: turning two differently-written records into comparable ones.

Every function here is pure - no I/O, no database, no mutable global state - so
the whole module is exhaustively testable with table-driven tests, and the same
code can run inside a Postgres-backed pipeline at Stage 5 without change.

Two paths, per PLAN 11.2. An individual is normalized by name, date of birth
and licence; an organization by legal name, trading name and EIN. Running
organizations through the individual path would teach the EM fit at Stage 3
that `first_name: missing` is common, draining the evidential weight of a real
first-name agreement for everybody else.

The output is a `NormalizedRecord`: one flat, hashable value carrying every
derived key the comparators and the blocking index need.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

from concordance.domain import Provider, SanctionRecord
from concordance.matching.lexicons import (
    CANONICAL_NAMES,
    CORPORATE_SUFFIXES,
    CREDENTIAL_SUFFIXES,
    GENERATIONAL_SUFFIXES,
    NAME_PREFIXES,
    NICKNAME_TO_CANONICAL,
    ORG_ACRONYM_EXPANSIONS,
    ORG_STOPWORDS,
    PO_BOX_MARKERS,
    STATE_ALIASES,
    STATE_CODES,
    STREET_ABBREVIATIONS,
    UNIT_DESIGNATORS,
    US_STATES,
)
from concordance.matching.npi_validator import NpiStatus, classify_npi
from concordance.matching.phonetics import phonetic_keys

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
# Cheap pre-check: most values need no whitespace collapsing at all.
_NEEDS_COLLAPSE = re.compile(r"\s\s|[\t\n\r\f\v]")
_DIGITS = re.compile(r"\d+")
_EIN = re.compile(r"^(\d{2})-?(\d{7})$")
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "SEPT": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def fold(value: str | None) -> str:
    """NFKD fold, strip combining marks, upper-case, collapse whitespace.

    Accents are dropped rather than preserved: one side of a pair routinely
    lost them in transit, so keeping them would manufacture disagreement where
    there is none.
    """
    if not value:
        return ""
    if value.isascii():
        # The overwhelming majority of fields are plain ASCII, and decomposing
        # them character by character was the single most expensive step of
        # building the blocking index over 50k providers.
        folded = value.upper()
    else:
        decomposed = unicodedata.normalize("NFKD", value)
        folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).upper()
    stripped = folded.strip()
    if not _NEEDS_COLLAPSE.search(stripped):
        return stripped
    return _WHITESPACE.sub(" ", stripped)


def strip_punctuation(value: str) -> str:
    """Punctuation to spaces, then collapse. Hyphens are separators, not letters."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value)).strip()


def tokens(value: str | None) -> tuple[str, ...]:
    """Folded, punctuation-free tokens."""
    cleaned = strip_punctuation(fold(value))
    return tuple(t for t in cleaned.split(" ") if t)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartialDate:
    """A date whose parts may individually be unknown.

    Sanction files carry `1973`, `10/1973` and `10/16/1973` in the same column.
    Keeping the parts separate lets a year-and-month agreement score as partial
    evidence instead of collapsing to "missing".
    """

    year: int | None = None
    month: int | None = None
    day: int | None = None

    def __bool__(self) -> bool:
        return self.year is not None or self.month is not None or self.day is not None

    @property
    def complete(self) -> bool:
        return None not in (self.year, self.month, self.day)

    def as_date(self) -> date | None:
        if not self.complete:
            return None
        try:
            return date(int(self.year or 0), int(self.month or 0), int(self.day or 0))
        except ValueError:
            return None

    def swapped(self) -> PartialDate:
        """Month and day exchanged - the US/ISO ambiguity, made explicit."""
        return PartialDate(self.year, self.day, self.month)


def _four_digit_year(value: int) -> int:
    """Windowing for two-digit years: 26 -> 2026, 74 -> 1974."""
    if value >= 100:
        return value
    return 2000 + value if value <= 26 else 1900 + value


def parse_date(value: str | date | None) -> PartialDate:
    """Parse every format the generator emits, plus the obvious neighbours.

    Unrecognized input yields an empty `PartialDate` rather than raising: a
    malformed date is missing data, not a program error.
    """
    if value is None:
        return PartialDate()
    if isinstance(value, date):
        return PartialDate(value.year, value.month, value.day)

    text = fold(value)
    if not text:
        return PartialDate()

    year: int | None
    month: int | None
    day: int | None

    # Month name forms: 16-OCT-1973, October 16, 1973, OCT 1973
    named = re.search(r"[A-Z]{3,}", text)
    if named and named.group()[:3] in _MONTHS:
        month = (
            _MONTHS[named.group()[:4]]
            if named.group()[:4] in _MONTHS
            else _MONTHS[named.group()[:3]]
        )
        numbers = [int(n) for n in _DIGITS.findall(text)]
        year = next((n for n in numbers if n > 31), None)
        day = next((n for n in numbers if n <= 31), None)
        return PartialDate(_four_digit_year(year) if year is not None else None, month, day)

    digits = _DIGITS.findall(text)

    # Compact: 19731016
    if len(digits) == 1 and len(digits[0]) == 8:
        raw = digits[0]
        return PartialDate(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    if len(digits) == 1 and len(digits[0]) == 4:
        return PartialDate(int(digits[0]))

    if len(digits) < 2:
        return PartialDate()

    parts = [int(d) for d in digits[:3]]
    if len(digits[0]) == 4:  # ISO: 1973-10-16
        year, month, day = parts[0], parts[1], parts[2] if len(parts) > 2 else None
    elif len(parts) == 2:  # 10/1973
        month, year = parts[0], _four_digit_year(parts[1])
        day = None
    else:  # US: 10/16/1973 or 10-16-73
        month, day, year = parts[0], parts[1], _four_digit_year(parts[2])
        if month > 12 and day <= 12:  # clearly day-first
            month, day = day, month

    if month is not None and not 1 <= month <= 12:
        month = None
    if day is not None and not 1 <= day <= 31:
        day = None
    return PartialDate(year, month, day)


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


@lru_cache(maxsize=32_768)
def canonical_given_name(name: str | None) -> str:
    """Fold an informal given name to its canonical form. Idempotent."""
    folded = fold(name)
    if not folded:
        return ""
    head = strip_punctuation(folded).split(" ")[0]
    if head in CANONICAL_NAMES:
        return head
    return NICKNAME_TO_CANONICAL.get(head, head)


def strip_name_affixes(name_tokens: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split tokens into (name tokens, dropped credential/generational tokens)."""
    kept: list[str] = []
    dropped: list[str] = []
    for token in name_tokens:
        bare = token.replace("-", "")
        if bare in CREDENTIAL_SUFFIXES or bare in GENERATIONAL_SUFFIXES or bare in NAME_PREFIXES:
            dropped.append(bare)
        else:
            kept.append(token)
    return tuple(kept), tuple(dropped)


def affix_tokens(value: str | None) -> tuple[str, ...]:
    """Split a suffix field without breaking hyphenated credentials.

    `PA-C` is one credential, not a `PA` followed by a stray `C`, so the suffix
    field is tokenized on whitespace and commas only.
    """
    folded = fold(value).replace(".", "")
    if not folded:
        return ()
    parts = re.split(r"[,\s]+", folded)
    return tuple(p.replace("-", "") for p in parts if p)


def normalize_person_name(
    first: str | None, middle: str | None, last: str | None, suffix: str | None = None
) -> tuple[str, str, tuple[str, ...]]:
    """Return (ordered form, sorted form, dropped affixes).

    The sorted form is what makes a first/last swap free: `SMITH ROBERT` and
    `ROBERT SMITH` normalize to the same sorted string, so the comparator can
    charge nothing for token order while still scoring the ordered form.
    """
    raw = tokens(" ".join(p for p in (first, middle, last) if p))
    kept, dropped = strip_name_affixes(raw)

    # The suffix field is handled apart from the name fields: whatever it holds
    # is an affix by definition, and splitting it on punctuation would turn
    # PA-C into a name token.
    suffix_kept, suffix_dropped = strip_name_affixes(affix_tokens(suffix))
    dropped = dropped + suffix_dropped + suffix_kept
    canonical = tuple(NICKNAME_TO_CANONICAL.get(t, t) if i == 0 else t for i, t in enumerate(kept))
    # Hyphenated surnames are one token in some sources and two in others.
    expanded: list[str] = []
    for token in canonical:
        expanded.extend(t for t in token.split("-") if t)
    ordered = " ".join(expanded)
    return ordered, " ".join(sorted(expanded)), dropped


# --------------------------------------------------------------------------
# organizations
# --------------------------------------------------------------------------


def normalize_org_name(name: str | None) -> tuple[str, str]:
    """Return (normalized name, legal-form suffix).

    `&` becomes `AND`, known acronyms are expanded, and the corporate suffix is
    lifted out: `Cedar Ridge Health LLC` and `Cedar Ridge Health, L.L.C.` must
    agree on the name and agree separately on the legal form, because a branch
    that reincorporated is still the same organization.
    """
    if not name:
        return "", ""
    # Periods are dropped rather than turned into spaces, so "L.L.C." and
    # "P.A." survive as single tokens the suffix table can recognize.
    folded = fold(name).replace("&", " AND ").replace(".", "")
    raw = [t for t in strip_punctuation(folded).split(" ") if t]

    suffix = ""
    while raw:
        candidate = CORPORATE_SUFFIXES.get(raw[-1])
        if candidate is None:
            break
        # The outermost suffix wins: "Foo Inc LLC" is an LLC.
        suffix = suffix or candidate
        raw.pop()

    expanded: list[str] = []
    for token in raw:
        if token in ORG_ACRONYM_EXPANSIONS:
            expanded.extend(ORG_ACRONYM_EXPANSIONS[token].split(" "))
        else:
            expanded.append(token)
    return " ".join(expanded), suffix


def org_significant_tokens(normalized_name: str) -> tuple[str, ...]:
    """Name tokens that carry identity - stopwords and legal forms removed."""
    return tuple(t for t in normalized_name.split(" ") if t and t not in ORG_STOPWORDS)


def org_acronym(normalized_name: str) -> str:
    """Initials of the significant tokens, for acronym-vs-expanded matching."""
    significant = org_significant_tokens(normalized_name)
    if len(significant) < 2:
        return ""
    return "".join(t[0] for t in significant)


def normalize_ein(value: str | None) -> str:
    """Nine digits as `NN-NNNNNNN`, or `""` if it is not a well-formed EIN."""
    if not value:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) != 9:
        return ""
    match = _EIN.match(digits)
    return f"{match.group(1)}-{match.group(2)}" if match else ""


def is_valid_ein(value: str | None) -> bool:
    """An EIN has no checksum; the most that can be checked is its shape."""
    return bool(normalize_ein(value))


# --------------------------------------------------------------------------
# addresses
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedAddress:
    line: str = ""  # street line, abbreviations expanded, unit removed
    unit: str = ""  # secondary unit, e.g. "STE 300"
    house_number: str = ""
    city: str = ""
    state: str = ""
    zip5: str = ""
    po_box: str = ""

    def __bool__(self) -> bool:
        return bool(self.line or self.po_box or self.zip5)


@lru_cache(maxsize=4_096)
def normalize_state(value: str | None) -> str:
    """Two-letter code, from a code, a full name, or a common misspelling."""
    folded = strip_punctuation(fold(value))
    if not folded:
        return ""
    if folded in STATE_CODES:
        return folded
    if folded in US_STATES:
        return US_STATES[folded]
    alias = STATE_ALIASES.get(folded) or STATE_ALIASES.get(fold(value))
    if alias:
        return alias
    # "CALIFORNIA, USA" and similar trailing noise.
    head = folded.split(" ")[0]
    return head if head in STATE_CODES else ""


def normalize_zip(value: str | None) -> str:
    """First five digits. ZIP+4 and a plain ZIP5 must not disagree."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 5:
        return ""
    return digits[:5]


def normalize_address(
    line1: str | None,
    line2: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
) -> NormalizedAddress:
    """Expand abbreviations, split the unit out, truncate the ZIP."""
    combined = " ".join(p for p in (line1, line2) if p)
    folded = strip_punctuation(fold(combined))

    po_box = ""
    for marker in PO_BOX_MARKERS:
        if folded.startswith(marker) or f" {marker}" in folded:
            digits = _DIGITS.search(folded)
            po_box = digits.group() if digits else "UNKNOWN"
            break

    parts = [t for t in folded.split(" ") if t]
    street: list[str] = []
    unit: list[str] = []
    in_unit = False
    for token in parts:
        if token in UNIT_DESIGNATORS:
            in_unit = True
            unit.append(token)
            continue
        if in_unit:
            unit.append(token)
            in_unit = False  # designator plus one value
            continue
        street.append(STREET_ABBREVIATIONS.get(token, token))

    house_number = street[0] if street and street[0].isdigit() else ""
    return NormalizedAddress(
        line=" ".join(street),
        unit=" ".join(unit),
        house_number=house_number,
        city=strip_punctuation(fold(city)),
        state=normalize_state(state),
        zip5=normalize_zip(zip_code),
        po_box=po_box,
    )


@lru_cache(maxsize=65_536)
def normalize_license(number: str | None) -> str:
    """Upper-cased alphanumerics, leading zeros dropped.

    `A-012345`, `A012345` and `12345` with a state prefix are the same licence
    written by three systems.
    """
    folded = "".join(ch for ch in fold(number) if ch.isalnum())
    if not folded:
        return ""
    letters = "".join(ch for ch in folded if ch.isalpha())
    digits = "".join(ch for ch in folded if ch.isdigit()).lstrip("0")
    return f"{letters}{digits}" if digits else folded


# --------------------------------------------------------------------------
# record-level normalization
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    """Everything the comparators and the blocking index need, precomputed."""

    record_id: str
    is_organization: bool = False

    # identifiers
    npi: str = ""
    npi_status: NpiStatus = NpiStatus.MISSING
    ein: str = ""

    # individual identity
    name_norm: str = ""
    name_sorted_norm: str = ""
    first_norm: str = ""
    # The given name as written, folded but *not* nickname-canonicalized. The
    # comparator needs it to tell an exact first-name agreement from a
    # nickname equivalence; `first_norm` alone has already collapsed the two.
    first_raw: str = ""
    last_norm: str = ""
    middle_initial: str = ""
    phonetic_keys: tuple[str, ...] = ()
    dropped_affixes: tuple[str, ...] = ()
    dob: PartialDate = field(default_factory=PartialDate)

    # organization identity
    org_name_norm: str = ""
    org_legal_suffix: str = ""
    org_tokens: tuple[str, ...] = ()
    org_acronym_key: str = ""
    dba_name_norm: str = ""
    dba_acronym_key: str = ""

    # shared
    address: NormalizedAddress = field(default_factory=NormalizedAddress)
    license_number: str = ""
    license_state: str = ""
    specialty: str = ""

    @property
    def has_valid_npi(self) -> bool:
        """Only a `VALID` NPI may drive a deterministic match."""
        return self.npi_status is NpiStatus.VALID


def _shared(
    record_id: str,
    npi: str | None,
    address: NormalizedAddress,
    license_number: str | None,
    license_state: str | None,
    specialty: str | None,
) -> dict[str, object]:
    status, cleaned = classify_npi(npi)
    return {
        "record_id": record_id,
        "npi": cleaned if status is NpiStatus.VALID else "",
        "npi_status": status,
        "address": address,
        "license_number": normalize_license(license_number),
        "license_state": normalize_state(license_state),
        "specialty": strip_punctuation(fold(specialty)),
    }


def _normalize_parts(
    record_id: str,
    *,
    is_organization: bool,
    npi: str | None,
    first: str | None,
    middle: str | None,
    last: str | None,
    suffix: str | None,
    dob: str | date | None,
    organization_name: str | None,
    dba_name: str | None,
    ein: str | None,
    line1: str | None,
    line2: str | None,
    city: str | None,
    state: str | None,
    zip_code: str | None,
    license_number: str | None,
    license_state: str | None,
    specialty: str | None,
) -> NormalizedRecord:
    address = normalize_address(line1, line2, city, state, zip_code)
    shared = _shared(record_id, npi, address, license_number, license_state, specialty)

    if is_organization:
        org_norm, legal_suffix = normalize_org_name(organization_name)
        dba_norm, _ = normalize_org_name(dba_name)
        return NormalizedRecord(
            is_organization=True,
            ein=normalize_ein(ein),
            org_name_norm=org_norm,
            org_legal_suffix=legal_suffix,
            org_tokens=org_significant_tokens(org_norm),
            org_acronym_key=org_acronym(org_norm),
            dba_name_norm=dba_norm,
            dba_acronym_key=org_acronym(dba_norm),
            phonetic_keys=phonetic_keys(org_norm.split(" ")[0]) if org_norm else (),
            **shared,  # type: ignore[arg-type]
        )

    ordered, sorted_form, dropped = normalize_person_name(first, middle, last, suffix)
    first_raw = tokens(first)[0] if tokens(first) else ""
    first_norm = canonical_given_name(first)
    # The fallback exists for sources that put the whole name in one box: the
    # last token is then the surname. It must not fire on a single token -
    # calling a lone given name a surname manufactures a DISAGREE where the
    # truth is MISSING, which is exactly the error the level table exists to
    # prevent.
    name_parts = ordered.split(" ") if ordered else []
    last_norm = (
        tokens(last)[-1] if tokens(last) else (name_parts[-1] if len(name_parts) > 1 else "")
    )
    middle_tokens = tokens(middle)
    return NormalizedRecord(
        is_organization=False,
        name_norm=ordered,
        name_sorted_norm=sorted_form,
        first_norm=first_norm,
        first_raw=first_raw,
        last_norm=last_norm,
        middle_initial=middle_tokens[0][0] if middle_tokens else "",
        phonetic_keys=phonetic_keys(last_norm),
        dropped_affixes=dropped,
        dob=parse_date(dob),
        ein=normalize_ein(ein),
        **shared,  # type: ignore[arg-type]
    )


def normalize_provider(provider: Provider) -> NormalizedRecord:
    """Normalize a provider-master row."""
    return _normalize_parts(
        provider.provider_id,
        is_organization=provider.is_organization,
        npi=provider.npi,
        first=provider.first_name,
        middle=provider.middle_name,
        last=provider.last_name,
        suffix=provider.suffix,
        dob=provider.dob,
        organization_name=provider.organization_name,
        dba_name=provider.dba_name,
        ein=provider.ein,
        line1=provider.address_line1,
        line2=provider.address_line2,
        city=provider.city,
        state=provider.state,
        zip_code=provider.zip,
        license_number=provider.license_number,
        license_state=provider.license_state,
        specialty=provider.specialty,
    )


def normalize_sanction(record: SanctionRecord) -> NormalizedRecord:
    """Normalize an inbound sanction record.

    `is_organization` is treated as a claim by the source, not a fact: a record
    whose surname field holds something that parses as an organization name is
    normalized as an organization as well, so the scenario where a practice is
    filed as a person still finds its provider (PLAN 11.2).
    """
    is_org = record.is_organization or _looks_like_organization(record)
    org_name: str | None = record.organization_name
    if is_org and not org_name:
        # The organization was filed in the person columns; recover the name.
        org_name = " ".join(
            p for p in (record.first_name, record.middle_name, record.last_name) if p
        )

    normalized = _normalize_parts(
        record.record_id,
        is_organization=is_org,
        npi=record.npi,
        first=record.first_name,
        middle=record.middle_name,
        last=record.last_name,
        suffix=record.suffix,
        dob=record.dob,
        organization_name=org_name,
        dba_name=record.dba_name,
        ein=record.ein,
        line1=record.address_line1,
        line2=record.address_line2,
        city=record.city,
        state=record.state,
        zip_code=record.zip,
        license_number=record.license_number,
        license_state=record.license_state,
        specialty=record.specialty,
    )
    return normalized


_ORG_HINTS = frozenset(
    {
        "CENTER",
        "CENTERS",
        "CENTRE",
        "CLINIC",
        "GROUP",
        "ASSOCIATES",
        "PARTNERS",
        "SERVICES",
        "SPECIALISTS",
        "INSTITUTE",
        "PRACTICE",
        "NETWORK",
        "SYSTEMS",
        "HOSPITAL",
        "HEALTH",
        "HEALTHCARE",
        "MEDICAL",
        "PHARMACY",
        "LABORATORY",
        "AGENCY",
        "HOSPICE",
        "AMBULANCE",
        "NURSING",
        "CARE",
        "THERAPY",
    }
)


def _looks_like_organization(record: SanctionRecord) -> bool:
    """Detect an organization filed in the person columns."""
    if record.organization_name:
        return True
    name_tokens = set(tokens(" ".join(p for p in (record.first_name, record.last_name) if p)))
    if name_tokens & _ORG_HINTS:
        return True
    return bool(name_tokens & set(CORPORATE_SUFFIXES))
