"""Every agreement level of every field, on records built to produce it.

Table-driven and exhaustive by construction: the parametrized cases are checked
against the level enums themselves, so adding a level to a field without adding
a case for it fails the suite rather than passing silently.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from concordance.domain import Provider, SanctionRecord
from concordance.matching.comparators import (
    FIELD_NAMES,
    INDIVIDUAL_FIELDS,
    ORGANIZATION_FIELDS,
    AddressLevel,
    DbaLevel,
    DobLevel,
    FirstNameLevel,
    IdentifierLevel,
    LastNameLevel,
    LegalNameLevel,
    LicenseLevel,
    ModelKind,
    StateLevel,
    ZipLevel,
    as_organization,
    compare,
    compare_address,
    compare_dba,
    compare_dob,
    compare_ein,
    compare_first_name,
    compare_last_name,
    compare_legal_name,
    compare_license,
    compare_npi,
    compare_state,
    compare_zip,
    describe,
    pair_kind,
)
from concordance.matching.normalization import normalize_provider, normalize_sanction
from concordance.matching.npi_validator import make_npi

NPI_A = make_npi("100000001")
NPI_B = make_npi("100000002")
ORG_NPI = make_npi("200000001")

BASE_PROVIDER = Provider(
    provider_id="P1",
    npi=NPI_A,
    first_name="Robert",
    last_name="Thompson",
    dob=date(1973, 10, 16),
    address_line1="14 Oak Street",
    address_line2="Suite 300",
    city="Austin",
    state="TX",
    zip="78701",
    license_number="A012345",
    license_state="TX",
)

BASE_SANCTION = SanctionRecord(
    record_id="S1",
    npi=NPI_A,
    first_name="Robert",
    last_name="Thompson",
    dob="1973-10-16",
    address_line1="14 Oak Street",
    address_line2="Suite 300",
    city="Austin",
    state="TX",
    zip="78701",
    license_number="A012345",
    license_state="TX",
)

ORG_PROVIDER = Provider(
    provider_id="O1",
    npi=ORG_NPI,
    organization_name="Riverside Family Practice Group LLC",
    dba_name="Riverside Clinic",
    ein="12-3456789",
    address_line1="900 Cedar Avenue",
    city="Seattle",
    state="WA",
    zip="98101",
    is_organization=True,
)

ORG_SANCTION = SanctionRecord(
    record_id="OS1",
    npi=ORG_NPI,
    organization_name="Riverside Family Practice Group LLC",
    dba_name="Riverside Clinic",
    ein="12-3456789",
    address_line1="900 Cedar Avenue",
    city="Seattle",
    state="WA",
    zip="98101",
    is_organization=True,
)


def pair(**sanction_overrides: object):
    """A normalized (sanction, provider) pair, the sanction side overridden."""
    record = replace(BASE_SANCTION, **sanction_overrides)  # type: ignore[arg-type]
    return normalize_sanction(record), normalize_provider(BASE_PROVIDER)


def org_pair(**sanction_overrides: object):
    record = replace(ORG_SANCTION, **sanction_overrides)  # type: ignore[arg-type]
    return normalize_sanction(record), normalize_provider(ORG_PROVIDER)


# --------------------------------------------------------------------------
# individual fields
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, LastNameLevel.EXACT),
        # Same Double Metaphone key, different spelling. Checked before the
        # string-similarity levels, which this pair would also clear.
        ({"last_name": "Thompsen"}, LastNameLevel.PHONETIC),
        ({"last_name": "Thomson"}, LastNameLevel.JW_HIGH),
        ({"last_name": "Thomlinson"}, LastNameLevel.JW_LOW),
        ({"last_name": "Nakamura"}, LastNameLevel.DISAGREE),
        # No surname at all. Must never be manufactured out of the given name.
        ({"last_name": None}, LastNameLevel.MISSING),
    ],
)
def test_last_name_levels(overrides: dict, expected: LastNameLevel) -> None:
    a, b = pair(**overrides)
    assert compare_last_name(a, b) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, FirstNameLevel.EXACT),
        ({"first_name": "Bob"}, FirstNameLevel.NICKNAME),
        ({"first_name": "R"}, FirstNameLevel.INITIAL),
        ({"first_name": "Robet"}, FirstNameLevel.JW_LOW),
        ({"first_name": "Margaret"}, FirstNameLevel.DISAGREE),
        ({"first_name": None}, FirstNameLevel.MISSING),
    ],
)
def test_first_name_levels(overrides: dict, expected: FirstNameLevel) -> None:
    a, b = pair(**overrides)
    assert compare_first_name(a, b) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, DobLevel.EXACT),
        ({"dob": "10/17/1973"}, DobLevel.YEAR_MONTH),
        ({"dob": "1973"}, DobLevel.YEAR_ONLY),
        ({"dob": "1974-10-16"}, DobLevel.DISAGREE),
        ({"dob": None}, DobLevel.MISSING),
    ],
)
def test_dob_levels(overrides: dict, expected: DobLevel) -> None:
    a, b = pair(**overrides)
    assert compare_dob(a, b) is expected


@pytest.mark.unit
def test_transposed_only_fires_where_the_swap_is_genuinely_ambiguous() -> None:
    """Normalization already resolves the unambiguous half of this.

    `16/10/1973` cannot be a US date, so `parse_date` reads it as 16 October and
    the comparator sees an exact agreement - no level needed. `TRANSPOSED` is
    for the case nothing can resolve: both parts are 12 or less, so 05/06 and
    06/05 are two readings of the same keystroke.
    """
    # The unambiguous half, resolved during normalization rather than scored.
    a, b = pair(dob="16/10/1973")
    assert compare_dob(a, b) is DobLevel.EXACT

    provider = normalize_provider(replace(BASE_PROVIDER, dob=date(1973, 5, 6)))
    same = normalize_sanction(replace(BASE_SANCTION, dob="05/06/1973"))
    assert compare_dob(same, provider) is DobLevel.EXACT

    swapped = normalize_sanction(replace(BASE_SANCTION, dob="06/05/1973"))
    assert compare_dob(swapped, provider) is DobLevel.TRANSPOSED

    different = normalize_sanction(replace(BASE_SANCTION, dob="11/06/1973"))
    assert compare_dob(different, provider) is DobLevel.DISAGREE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, AddressLevel.EXACT),
        ({"address_line1": "14 Oak St"}, AddressLevel.EXACT),  # abbreviation expanded
        ({"address_line2": "Suite 410"}, AddressLevel.SAME_STREET_DIFF_UNIT),
        ({"address_line2": None}, AddressLevel.SAME_STREET_DIFF_UNIT),
        ({"address_line1": "Oak Street 14", "address_line2": None}, AddressLevel.TOKEN_SET),
        (
            {"address_line1": "77 Pine Boulevard", "address_line2": None},
            AddressLevel.SAME_ZIP_ONLY,
        ),
        (
            {"address_line1": "77 Pine Boulevard", "address_line2": None, "zip": "99501"},
            AddressLevel.DISAGREE,
        ),
        ({"address_line1": None, "address_line2": None, "zip": "99501"}, AddressLevel.MISSING),
    ],
)
def test_address_levels(overrides: dict, expected: AddressLevel) -> None:
    a, b = pair(**overrides)
    assert compare_address(a, b) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, StateLevel.EXACT),
        ({"state": "WA"}, StateLevel.DISAGREE),
        ({"state": None}, StateLevel.MISSING),
    ],
)
def test_state_levels(overrides: dict, expected: StateLevel) -> None:
    a, b = pair(**overrides)
    assert compare_state(a, b) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ZipLevel.ZIP5),
        ({"zip": "78701-1234"}, ZipLevel.ZIP5),  # ZIP+4 must not disagree with ZIP5
        ({"zip": "78799"}, ZipLevel.ZIP3),
        ({"zip": "99501"}, ZipLevel.DISAGREE),
        ({"zip": None}, ZipLevel.MISSING),
    ],
)
def test_zip_levels(overrides: dict, expected: ZipLevel) -> None:
    a, b = pair(**overrides)
    assert compare_zip(a, b) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, LicenseLevel.EXACT_SAME_STATE),
        ({"license_state": "WA"}, LicenseLevel.EXACT_DIFF_STATE),
        ({"license_state": None}, LicenseLevel.EXACT_DIFF_STATE),
        ({"license_number": "B999"}, LicenseLevel.DISAGREE),
        ({"license_number": None}, LicenseLevel.MISSING),
    ],
)
def test_license_levels(overrides: dict, expected: LicenseLevel) -> None:
    a, b = pair(**overrides)
    assert compare_license(a, b) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, IdentifierLevel.VALID_EXACT),
        ({"npi": NPI_B}, IdentifierLevel.VALID_DISAGREE),
        ({"npi": None}, IdentifierLevel.ONE_INVALID),
        ({"npi": "0000000000"}, IdentifierLevel.ONE_INVALID),
        ({"npi": "UNKNOWN"}, IdentifierLevel.ONE_INVALID),
    ],
)
def test_npi_levels(overrides: dict, expected: IdentifierLevel) -> None:
    a, b = pair(**overrides)
    assert compare_npi(a, b) is expected


@pytest.mark.unit
def test_both_sides_lacking_a_valid_npi_is_not_a_disagreement() -> None:
    """The distinction the whole NPI level table exists for."""
    record = replace(BASE_SANCTION, npi="0000000000")
    provider = replace(BASE_PROVIDER, npi=None)
    level = compare_npi(normalize_sanction(record), normalize_provider(provider))
    assert level is IdentifierLevel.BOTH_INVALID
    assert level is not IdentifierLevel.VALID_DISAGREE


# --------------------------------------------------------------------------
# organization fields
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, LegalNameLevel.EXACT),
        ({"organization_name": "Riverside Family Practice Group, L.L.C."}, LegalNameLevel.EXACT),
        ({"organization_name": "RFPG"}, LegalNameLevel.ACRONYM),
        ({"organization_name": "Group Practice Family Riverside LLC"}, LegalNameLevel.TOKEN_SET),
        ({"organization_name": "Riversied Familly Practise Grope"}, LegalNameLevel.JW_LOW),
        ({"organization_name": "Cedar Ridge Hospice Services"}, LegalNameLevel.DISAGREE),
    ],
)
def test_legal_name_levels(overrides: dict, expected: LegalNameLevel) -> None:
    a, b = org_pair(**overrides)
    assert compare_legal_name(a, b) is expected


@pytest.mark.unit
def test_legal_name_missing_when_one_side_has_no_name() -> None:
    a, b = org_pair(organization_name="X")
    empty = replace(b, org_name_norm="")
    assert compare_legal_name(a, empty) is LegalNameLevel.MISSING


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, DbaLevel.EXACT),
        ({"dba_name": "Clinic Riverside"}, DbaLevel.TOKEN_SET),
        ({"dba_name": "Puget Sound Imaging"}, DbaLevel.DISAGREE),
        # One side files only the legal name: the other side's trading name is
        # still compared against it, because either name may be the one filed.
        ({"dba_name": None}, DbaLevel.DISAGREE),
    ],
)
def test_dba_levels(overrides: dict, expected: DbaLevel) -> None:
    a, b = org_pair(**overrides)
    assert compare_dba(a, b) is expected


@pytest.mark.unit
def test_dba_does_not_re_score_the_legal_name() -> None:
    """Two organizations sharing a legal name but not a trading name disagree.

    Scoring legal-against-legal here would report `EXACT` on evidence that
    `legal_name` has already counted - the same fact, counted twice, under a
    field that never saw it.
    """
    a, b = org_pair(dba_name="Puget Sound Imaging")
    assert compare_legal_name(a, b) is LegalNameLevel.EXACT
    assert compare_dba(a, b) is DbaLevel.DISAGREE


@pytest.mark.unit
def test_dba_crosses_the_two_name_slots() -> None:
    """The file carries the trading name where the master carries the legal one."""
    record = replace(ORG_SANCTION, organization_name="Riverside Clinic", dba_name=None)
    level = compare_dba(normalize_sanction(record), normalize_provider(ORG_PROVIDER))
    assert level is DbaLevel.EXACT


@pytest.mark.unit
def test_dba_missing_when_neither_side_files_a_trading_name() -> None:
    record = replace(ORG_SANCTION, dba_name=None)
    provider = replace(ORG_PROVIDER, dba_name=None)
    level = compare_dba(normalize_sanction(record), normalize_provider(provider))
    assert level is DbaLevel.MISSING


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, IdentifierLevel.VALID_EXACT),
        ({"ein": "98-7654321"}, IdentifierLevel.VALID_DISAGREE),
        ({"ein": None}, IdentifierLevel.ONE_INVALID),
        ({"ein": "not-an-ein"}, IdentifierLevel.ONE_INVALID),
    ],
)
def test_ein_levels(overrides: dict, expected: IdentifierLevel) -> None:
    a, b = org_pair(**overrides)
    assert compare_ein(a, b) is expected


@pytest.mark.unit
def test_acronym_matches_in_both_directions() -> None:
    """RFPG must meet the expansion whichever side of the pair it is on."""
    short = normalize_sanction(replace(ORG_SANCTION, organization_name="RFPG"))
    long = normalize_provider(ORG_PROVIDER)
    assert compare_legal_name(short, long) is LegalNameLevel.ACRONYM
    assert compare_legal_name(long, short) is LegalNameLevel.ACRONYM


@pytest.mark.unit
def test_shared_initials_are_not_an_acronym_match() -> None:
    """Two spelled-out organizations that happen to share initials are not equal.

    Redwood Family Physicians Group reduces to the same RFPG as Riverside Family
    Practice Group. Treating that as evidence would match half the directory.
    """
    other = normalize_sanction(
        replace(ORG_SANCTION, organization_name="Redwood Family Physicians Group LLC")
    )
    assert compare_legal_name(other, normalize_provider(ORG_PROVIDER)) is not (
        LegalNameLevel.ACRONYM
    )


# --------------------------------------------------------------------------
# vector assembly
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_vector_is_fixed_length_and_ordered() -> None:
    a, b = pair()
    vector = compare(a, b, ModelKind.INDIVIDUAL)
    assert len(vector) == len(INDIVIDUAL_FIELDS)
    assert list(describe(vector, ModelKind.INDIVIDUAL)) == list(FIELD_NAMES[ModelKind.INDIVIDUAL])
    # Repeated assembly must be identical: the EM tables index positionally.
    assert compare(a, b, ModelKind.INDIVIDUAL) == vector


@pytest.mark.unit
def test_organization_vector_is_its_own_shape() -> None:
    a, b = org_pair()
    vector = compare(a, b, ModelKind.ORGANIZATION)
    assert len(vector) == len(ORGANIZATION_FIELDS)
    assert FIELD_NAMES[ModelKind.ORGANIZATION] != FIELD_NAMES[ModelKind.INDIVIDUAL]


@pytest.mark.unit
@pytest.mark.parametrize("fields", [INDIVIDUAL_FIELDS, ORGANIZATION_FIELDS])
def test_every_level_code_is_stable_and_contiguous(fields: tuple) -> None:
    """A level table with a gap or a renumbering invalidates every fitted config."""
    for spec in fields:
        codes = sorted(int(level) for level in spec.levels)
        assert codes == list(range(len(codes))), spec.name


@pytest.mark.unit
def test_missing_is_its_own_level_wherever_a_field_can_be_absent() -> None:
    """The rule the module exists to enforce - except for the identifiers."""
    for spec in INDIVIDUAL_FIELDS + ORGANIZATION_FIELDS:
        names = {level.name for level in spec.levels}
        if spec.levels is IdentifierLevel:
            assert "MISSING" not in names
            assert {"BOTH_INVALID", "ONE_INVALID", "VALID_DISAGREE"} <= names
        else:
            assert "MISSING" in names and "DISAGREE" in names, spec.name
            assert spec.levels.MISSING != spec.levels.DISAGREE  # type: ignore[attr-defined]


@pytest.mark.unit
def test_cross_type_pairs_are_reported_not_scored() -> None:
    individual, _ = pair()
    organization = normalize_provider(ORG_PROVIDER)
    assert pair_kind(individual, organization) is None
    coerced = as_organization(individual)
    assert pair_kind(coerced, organization) is ModelKind.ORGANIZATION
    assert coerced.is_organization
    assert coerced.org_name_norm


@pytest.mark.unit
def test_as_organization_is_idempotent() -> None:
    organization = normalize_provider(ORG_PROVIDER)
    assert as_organization(organization) is organization
