"""The canonical sanction fields: what a column mapping may target (PLAN 11.1).

Every uploaded file is read through a mapping `{canonical_field: source_header}`
onto this set and nothing else. The set is the columns of `sanction_records`
that come from a document - no derived columns, no bookkeeping - so a field
that cannot be mapped cannot reach the table.

**Required is deliberately small: a name.** `last_name` or `organization_name`,
nothing more. A record key is not required because the real LEIE file has
none, and a mapping rule that the largest public exclusion list cannot satisfy
is a rule that gets worked around. When no key column is mapped, one is derived
from the row's content - see `ingest.derive_record_key`.

The synonyms are ordinary domain vocabulary - the headers that federal and state
exclusion lists actually use - not the synthetic generator's dialects. The
proposal is a starting point the analyst confirms; it is measured against the
generator's four dialects in the tests, and misses there are expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """How a cell is parsed on the way in."""

    TEXT = "text"
    NAME = "name"
    NPI = "npi"
    ZIP = "zip"
    STATE = "state"
    DATE = "date"
    DOB = "dob"


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    kind: Kind
    max_length: int
    label: str
    synonyms: tuple[str, ...]


FIELDS: tuple[Field, ...] = (
    Field(
        "record_id", Kind.TEXT, 64, "Record key",
        ("record id", "record key", "reckey", "case number", "docket", "docket number",
         "reference number", "exclusion id", "sam number", "uei", "key", "id"),
    ),
    Field(
        "npi", Kind.NPI, 20, "NPI",
        ("npi", "npi number", "national provider identifier", "national provider id",
         "provider identifier", "provider npi"),
    ),
    Field(
        "first_name", Kind.NAME, 100, "First name",
        ("first name", "given name", "forename", "first"),
    ),
    Field(
        "middle_name", Kind.NAME, 100, "Middle name",
        ("middle name", "middle initial", "middle", "mi"),
    ),
    Field(
        "last_name", Kind.NAME, 100, "Last name",
        ("last name", "surname", "family name", "last"),
    ),
    Field(
        "suffix", Kind.TEXT, 20, "Suffix",
        ("suffix", "name suffix", "credentials", "generational suffix"),
    ),
    Field(
        "organization_name", Kind.NAME, 200, "Organization name",
        ("business name", "organization name", "organisation name", "entity name",
         "legal name", "entity legal name", "facility name", "practice name",
         "company name", "firm name"),
    ),
    Field(
        "dba_name", Kind.NAME, 200, "Doing business as",
        ("doing business as", "dba", "dba name", "trade name", "alias"),
    ),
    Field(
        "dob", Kind.DOB, 64, "Date of birth",
        ("date of birth", "birth date", "birthdate", "dob"),
    ),
    Field(
        "address_line1", Kind.TEXT, 200, "Address",
        ("address", "address line 1", "address 1", "street", "street address",
         "practice address", "service address", "mailing address"),
    ),
    Field(
        "address_line2", Kind.TEXT, 200, "Address line 2",
        ("address line 2", "address 2", "unit", "suite", "apartment"),
    ),
    Field("city", Kind.TEXT, 100, "City", ("city", "city name", "town", "municipality")),
    Field(
        "state", Kind.STATE, 2, "State",
        ("state", "state province", "province", "state code"),
    ),
    Field(
        "zip", Kind.ZIP, 10, "ZIP",
        ("zip", "zip code", "postal code", "postcode"),
    ),
    Field(
        "specialty", Kind.TEXT, 100, "Specialty",
        ("specialty", "speciality", "provider type", "provider type code",
         "area of practice", "practice area", "general"),
    ),
    Field(
        "license_number", Kind.TEXT, 50, "License number",
        ("license number", "license", "licence number", "licence"),
    ),
    Field(
        "license_state", Kind.STATE, 2, "License state",
        ("license state", "license issuing state", "issuing state", "issued by",
         "licence state"),
    ),
    Field(
        "sanction_type", Kind.TEXT, 100, "Sanction type",
        ("exclusion type", "sanction type", "action type", "disciplinary action",
         "termination reason", "exclusion reason", "reason"),
    ),
    Field(
        "exclusion_date", Kind.DATE, 20, "Exclusion date",
        ("exclusion date", "effective date", "action effective date", "effective",
         "termination effective date", "sanction date", "start date"),
    ),
    Field(
        "reinstatement_date", Kind.DATE, 20, "Reinstatement date",
        ("reinstatement date", "reinstate date", "action termination date", "end date",
         "ends", "termination end date"),
    ),
    Field(
        "ein", Kind.TEXT, 20, "EIN",
        ("ein", "tax id", "taxpayer id", "tin", "federal tax id", "employer identification number"),
    ),
)

BY_NAME: dict[str, Field] = {f.name: f for f in FIELDS}
CANONICAL_FIELDS: tuple[str, ...] = tuple(BY_NAME)

#: At least one of these must be mapped, and every accepted row must have one.
NAME_FIELDS: tuple[str, ...] = ("last_name", "organization_name")

#: A row needs its name plus one of these, or there is nothing to match it by
#: except the name itself - which is how a truncated row gets past a parser.
CORROBORATING_FIELDS: tuple[str, ...] = (
    "npi", "dob", "state", "city", "zip", "address_line1", "license_number", "ein",
    "exclusion_date",
)

#: The fields a record's content is compared on to decide whether an upload
#: changed it. Everything that reaches the matcher or a reviewer; not `raw`,
#: which differs between files for reasons that are not the record's.
CONTENT_FIELDS: tuple[str, ...] = (
    *(f for f in CANONICAL_FIELDS if f != "record_id"),
    "is_organization",
)


__all__ = [
    "BY_NAME",
    "CANONICAL_FIELDS",
    "CONTENT_FIELDS",
    "CORROBORATING_FIELDS",
    "FIELDS",
    "NAME_FIELDS",
    "Field",
    "Kind",
]
