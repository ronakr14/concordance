"""Table-driven tests for the normalization functions.

These are pure functions, so they are cheap to test exhaustively - and every
comparator at Stage 3 is built on top of them, which makes this file the part
of the suite the rest of the numbers depend on.
"""

from __future__ import annotations

from datetime import date

import pytest

from concordance.domain import Provider, SanctionRecord
from concordance.matching.normalization import (
    NormalizedRecord,
    PartialDate,
    canonical_given_name,
    fold,
    is_valid_ein,
    normalize_address,
    normalize_ein,
    normalize_license,
    normalize_org_name,
    normalize_person_name,
    normalize_provider,
    normalize_sanction,
    normalize_state,
    normalize_zip,
    org_acronym,
    org_significant_tokens,
    parse_date,
    strip_name_affixes,
    strip_punctuation,
    tokens,
)
from concordance.matching.npi_validator import NpiStatus, make_npi

VALID_NPI = make_npi("123456789")


# --- primitives ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("  ", ""),
        ("Smith", "SMITH"),
        ("  Jose   Garcia ", "JOSE GARCIA"),
        ("Muñoz", "MUNOZ"),
        ("José", "JOSE"),
        ("Ångström", "ANGSTROM"),
        ("O'Brien", "O'BRIEN"),
        ("a\tb\nc", "A B C"),
        ("ﬁnance", "FINANCE"),  # ligature, decomposed by NFKD
    ],
)
def test_fold(raw: str | None, expected: str) -> None:
    assert fold(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("O'BRIEN", "O BRIEN"),
        ("SMITH-JONES", "SMITH JONES"),
        ("ST. LUKE'S", "ST LUKE S"),
        ("NO PUNCTUATION", "NO PUNCTUATION"),
    ],
)
def test_strip_punctuation(raw: str, expected: str) -> None:
    assert strip_punctuation(raw) == expected


@pytest.mark.unit
def test_tokens_folds_and_splits() -> None:
    assert tokens("  Mary-Jane  O'Neill ") == ("MARY", "JANE", "O", "NEILL")
    assert tokens(None) == ()


# --- dates ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1973-10-16", PartialDate(1973, 10, 16)),
        ("10/16/1973", PartialDate(1973, 10, 16)),
        ("16-OCT-1973", PartialDate(1973, 10, 16)),
        ("19731016", PartialDate(1973, 10, 16)),
        ("10-16-73", PartialDate(1973, 10, 16)),
        ("October 16, 1973", PartialDate(1973, 10, 16)),
        ("OCT 1973", PartialDate(1973, 10, None)),
        ("10/1973", PartialDate(1973, 10, None)),
        ("1973", PartialDate(1973, None, None)),
        ("16/10/1973", PartialDate(1973, 10, 16)),  # day-first, disambiguated
        ("", PartialDate()),
        (None, PartialDate()),
        ("not a date", PartialDate()),
        (date(1973, 10, 16), PartialDate(1973, 10, 16)),
    ],
)
def test_parse_date(raw: str | date | None, expected: PartialDate) -> None:
    assert parse_date(raw) == expected


@pytest.mark.unit
def test_two_digit_year_windowing() -> None:
    assert parse_date("01/02/99").year == 1999
    assert parse_date("01/02/05").year == 2005


@pytest.mark.unit
def test_partial_date_helpers() -> None:
    full = PartialDate(1973, 10, 16)
    assert full.complete and full.as_date() == date(1973, 10, 16)
    assert full.swapped() == PartialDate(1973, 16, 10)
    partial = PartialDate(1973, None, None)
    assert bool(partial) and not partial.complete and partial.as_date() is None
    assert not bool(PartialDate())
    assert PartialDate(1973, 2, 30).as_date() is None  # impossible day


# --- names ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bob", "ROBERT"),
        ("BOBBY", "ROBERT"),
        ("Robert", "ROBERT"),
        ("Peggy", "MARGARET"),
        ("Bill", "WILLIAM"),
        ("Liz", "ELIZABETH"),
        ("Xiomara", "XIOMARA"),  # unknown names pass through
        (None, ""),
    ],
)
def test_canonical_given_name(raw: str | None, expected: str) -> None:
    assert canonical_given_name(raw) == expected


@pytest.mark.unit
def test_canonical_given_name_is_idempotent() -> None:
    for name in ("Bob", "Robert", "Peggy", "Unknownname"):
        once = canonical_given_name(name)
        assert canonical_given_name(once) == once


@pytest.mark.unit
def test_nickname_round_trip_matches_the_formal_name() -> None:
    """The point of the table: Bob Smith and Robert Smith normalize alike."""
    informal, _, _ = normalize_person_name("Bob", None, "Smith", None)
    formal, _, _ = normalize_person_name("Robert", None, "Smith", None)
    assert informal == formal == "ROBERT SMITH"


@pytest.mark.unit
def test_credentials_and_generational_suffixes_are_stripped() -> None:
    ordered, _, dropped = normalize_person_name("John", None, "Doe", "MD")
    assert ordered == "JOHN DOE"
    assert dropped == ("MD",)
    _, _, dropped_jr = normalize_person_name("John", None, "Doe", "Jr")
    assert dropped_jr == ("JR",)
    _, _, dropped_pac = normalize_person_name("Ann", None, "Lee", "PA-C")
    assert dropped_pac == ("PAC",)


@pytest.mark.unit
def test_sorted_form_makes_a_token_swap_free() -> None:
    _, forward, _ = normalize_person_name("Theresa", None, "Smith", None)
    _, swapped, _ = normalize_person_name("Smith", None, "Theresa", None)
    assert forward == swapped


@pytest.mark.unit
def test_hyphenated_surnames_split_into_tokens() -> None:
    ordered, sorted_form, _ = normalize_person_name("Ann", None, "Smith-Jones", None)
    assert ordered == "ANN SMITH JONES"
    assert sorted_form == "ANN JONES SMITH"


@pytest.mark.unit
def test_strip_name_affixes_keeps_real_tokens() -> None:
    kept, dropped = strip_name_affixes(("DR", "JANE", "ROE", "PHD", "III"))
    assert kept == ("JANE", "ROE")
    assert set(dropped) == {"DR", "PHD", "III"}


# --- organizations -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "name", "suffix"),
    [
        ("Cedar Ridge Health LLC", "CEDAR RIDGE HEALTH", "LLC"),
        ("Cedar Ridge Health, L.L.C.", "CEDAR RIDGE HEALTH", "LLC"),
        ("Smith & Jones Associates", "SMITH AND JONES ASSOCIATES", ""),
        ("Mercy Clinic, Incorporated", "MERCY CLINIC", "INC"),
        ("Oak P.A.", "OAK", "PA"),
        ("Valley SNF", "VALLEY SKILLED NURSING FACILITY", ""),
        (None, "", ""),
    ],
)
def test_normalize_org_name(raw: str | None, name: str, suffix: str) -> None:
    assert normalize_org_name(raw) == (name, suffix)


@pytest.mark.unit
def test_org_acronym_and_significant_tokens() -> None:
    normalized, _ = normalize_org_name("The Riverside Family Practice Group LLC")
    assert org_significant_tokens(normalized) == ("RIVERSIDE", "FAMILY", "PRACTICE", "GROUP")
    assert org_acronym(normalized) == "RFPG"
    assert org_acronym("SOLO") == ""  # a single token has no acronym


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12-3456789", "12-3456789"),
        ("123456789", "12-3456789"),
        ("12 3456789", "12-3456789"),
        ("1234567", ""),
        (None, ""),
        ("not-an-ein", ""),
    ],
)
def test_normalize_ein(raw: str | None, expected: str) -> None:
    assert normalize_ein(raw) == expected
    assert is_valid_ein(raw) is bool(expected)


# --- addresses -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TX", "TX"),
        ("tx", "TX"),
        ("Texas", "TX"),
        ("Calif", "CA"),
        ("new york", "NY"),
        ("N Carolina", "NC"),
        ("D.C.", "DC"),
        ("Narnia", ""),
        (None, ""),
    ],
)
def test_normalize_state(raw: str | None, expected: str) -> None:
    assert normalize_state(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("78701", "78701"), ("78701-1234", "78701"), ("787011234", "78701"), ("787", ""), (None, "")],
)
def test_normalize_zip(raw: str | None, expected: str) -> None:
    assert normalize_zip(raw) == expected


@pytest.mark.unit
def test_normalize_address_expands_and_splits() -> None:
    addr = normalize_address("123 N Oak St", "Suite 300", "Austin", "Texas", "78701-1234")
    assert addr.line == "123 NORTH OAK STREET"
    assert addr.unit == "SUITE 300"
    assert addr.house_number == "123"
    assert addr.city == "AUSTIN"
    assert addr.state == "TX"
    assert addr.zip5 == "78701"
    assert not addr.po_box


@pytest.mark.unit
def test_abbreviated_and_expanded_addresses_agree() -> None:
    """The USPS-abbreviation corruption must cost the comparator nothing."""
    a = normalize_address("678 Hickory Pkwy", None, "Denver", "CO", "80236")
    b = normalize_address("678 Hickory Parkway", None, "Denver", "CO", "80236-4759")
    assert a.line == b.line
    assert a.zip5 == b.zip5


@pytest.mark.unit
def test_po_box_is_recognized() -> None:
    addr = normalize_address("PO Box 4471", None, "Reno", "NV", "89501")
    assert addr.po_box == "4471"


@pytest.mark.unit
def test_empty_address_is_falsey() -> None:
    assert not normalize_address(None, None, None, None, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("A-012345", "A12345"), ("A012345", "A12345"), ("  a012345 ", "A12345"), ("38-01388", "3801388"), (None, "")],
)
def test_normalize_license(raw: str | None, expected: str) -> None:
    assert normalize_license(raw) == expected


# --- record level --------------------------------------------------------


def _provider(**kwargs: object) -> Provider:
    base: dict[str, object] = {
        "provider_id": "P1",
        "npi": VALID_NPI,
        "first_name": "Robert",
        "last_name": "Smith",
        "dob": date(1970, 5, 4),
        "address_line1": "12 Oak St",
        "city": "Austin",
        "state": "TX",
        "zip": "78701",
        "license_number": "A012345",
        "license_state": "TX",
    }
    base.update(kwargs)
    return Provider(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_normalize_provider_individual() -> None:
    norm = normalize_provider(_provider())
    assert isinstance(norm, NormalizedRecord)
    assert norm.record_id == "P1"
    assert norm.name_norm == "ROBERT SMITH"
    assert norm.first_norm == "ROBERT"
    assert norm.last_norm == "SMITH"
    assert norm.npi_status is NpiStatus.VALID and norm.has_valid_npi
    assert norm.dob == PartialDate(1970, 5, 4)
    assert norm.phonetic_keys and norm.phonetic_keys[0].startswith("SM")
    assert norm.address.state == "TX"


@pytest.mark.unit
def test_normalize_provider_organization_uses_the_org_path() -> None:
    norm = normalize_provider(
        _provider(
            is_organization=True,
            first_name=None,
            last_name=None,
            dob=None,
            organization_name="Cedar Ridge Health LLC",
            dba_name="Cedar Ridge Clinic",
            ein="123456789",
        )
    )
    assert norm.is_organization
    assert norm.org_name_norm == "CEDAR RIDGE HEALTH"
    assert norm.org_legal_suffix == "LLC"
    assert norm.org_acronym_key == "CRH"
    assert norm.dba_acronym_key == "CRC"
    assert norm.ein == "12-3456789"
    assert norm.name_norm == ""  # the individual fields stay empty


@pytest.mark.unit
def test_sanction_with_invalid_npi_carries_no_identifier() -> None:
    record = SanctionRecord(record_id="S1", npi="0000000000", first_name="Bob", last_name="Smith")
    norm = normalize_sanction(record)
    assert norm.npi_status is NpiStatus.SENTINEL
    assert norm.npi == ""
    assert not norm.has_valid_npi


@pytest.mark.unit
def test_organization_filed_as_a_person_is_still_normalized_as_one() -> None:
    """PLAN 11.2: `is_organization` is a claim by the source, not a fact."""
    record = SanctionRecord(
        record_id="S2",
        is_organization=False,
        last_name="Silver Creek Healthcare Institute Inc",
        state="WI",
    )
    norm = normalize_sanction(record)
    assert norm.is_organization
    assert norm.org_name_norm == "SILVER CREEK HEALTHCARE INSTITUTE"
    assert norm.org_acronym_key == "SCHI"


@pytest.mark.unit
def test_a_real_person_is_not_mistaken_for_an_organization() -> None:
    record = SanctionRecord(record_id="S3", first_name="Robert", last_name="Smith", state="TX")
    assert not normalize_sanction(record).is_organization
