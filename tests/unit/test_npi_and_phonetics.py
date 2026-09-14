"""NPI classification and the Double Metaphone key."""

from __future__ import annotations

import pytest

from concordance.matching.npi_validator import (
    NpiStatus,
    classify_npi,
    clean_npi,
    is_placeholder,
    is_valid_npi,
    luhn_check_digit,
    make_npi,
    npi_check_digit,
)
from concordance.matching.phonetics import double_metaphone, phonetic_key, phonetic_keys

# A checksum-valid NPI and its neighbours, computed by hand from the 80840 rule.
VALID = "1234567893"
CHECKSUM_FAIL = "1234567890"  # correct body, wrong final digit (also a sentinel)


# --- Luhn ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("7992739871", 3),  # the classic worked example from the Luhn literature
        ("80840123456789", 3),  # the NPI body 123456789 with its prefix
        ("0", 0),
    ],
)
def test_luhn_check_digit_against_hand_computed_values(payload: str, expected: int) -> None:
    assert luhn_check_digit(payload) == expected


@pytest.mark.unit
def test_npi_check_digit_uses_the_80840_prefix() -> None:
    # 1234567893 is the worked example in the CMS check-digit guidance.
    assert npi_check_digit("123456789") == 3
    assert make_npi("123456789") == VALID
    # The prefix matters: dropping it would give a different digit.
    assert npi_check_digit("123456789") != luhn_check_digit("123456789")


@pytest.mark.unit
def test_npi_check_digit_rejects_wrong_length_input() -> None:
    with pytest.raises(ValueError, match="nine digits"):
        npi_check_digit("12345")


@pytest.mark.unit
def test_every_generated_npi_validates() -> None:
    for body in ("100000000", "199999999", "287654321", "100000001"):
        assert is_valid_npi(make_npi(body))


# --- classification ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (VALID, NpiStatus.VALID),
        (" 1234567893 ", NpiStatus.VALID),
        ("1234-567-893", NpiStatus.VALID),
        (None, NpiStatus.MISSING),
        ("", NpiStatus.MISSING),
        ("   ", NpiStatus.MISSING),
        ("0000000000", NpiStatus.SENTINEL),
        ("9999999999", NpiStatus.SENTINEL),
        ("1111111111", NpiStatus.SENTINEL),
        ("UNKNOWN", NpiStatus.PLACEHOLDER_TEXT),
        ("N/A", NpiStatus.PLACEHOLDER_TEXT),
        ("n/a", NpiStatus.PLACEHOLDER_TEXT),
        ("NONE", NpiStatus.PLACEHOLDER_TEXT),
        ("TBD", NpiStatus.PLACEHOLDER_TEXT),
        ("-", NpiStatus.PLACEHOLDER_TEXT),
        ("PENDING", NpiStatus.PLACEHOLDER_TEXT),
        ("12345", NpiStatus.MALFORMED),
        ("12345678901", NpiStatus.MALFORMED),
        ("12345678AB", NpiStatus.MALFORMED),
        ("1234567894", NpiStatus.CHECKSUM_FAIL),
        ("1999999999", NpiStatus.CHECKSUM_FAIL),
    ],
)
def test_classify_npi(raw: str | None, status: NpiStatus) -> None:
    assert classify_npi(raw)[0] is status


@pytest.mark.unit
def test_classification_is_exhaustive_and_exclusive() -> None:
    """Every value lands in exactly one class - that is the whole contract."""
    samples = [None, "UNKNOWN", "0000000000", "12345", "1234567894", VALID]
    statuses = [classify_npi(s)[0] for s in samples]
    assert len(set(statuses)) == len(statuses) == len(NpiStatus)
    assert set(statuses) == set(NpiStatus)


@pytest.mark.unit
def test_only_valid_is_informative_for_a_deterministic_match() -> None:
    assert NpiStatus.VALID.is_informative
    assert NpiStatus.CHECKSUM_FAIL.is_informative  # a wrong number is evidence
    assert not NpiStatus.MISSING.is_informative
    assert not NpiStatus.SENTINEL.is_informative
    assert not NpiStatus.PLACEHOLDER_TEXT.is_informative


@pytest.mark.unit
def test_sentinel_and_placeholder_lists_are_configurable() -> None:
    status, _ = classify_npi("5551234567", sentinels=frozenset({"5551234567"}))
    assert status is NpiStatus.SENTINEL
    status, _ = classify_npi("XX", placeholders=frozenset({"XX"}))
    assert status is NpiStatus.PLACEHOLDER_TEXT


@pytest.mark.unit
def test_clean_npi_strips_separators() -> None:
    assert clean_npi(" 1234-567 893 ") == "1234567893"
    assert clean_npi(None) == ""


@pytest.mark.unit
def test_is_placeholder_covers_the_uninformative_classes() -> None:
    for value in ("", None, "N/A", "0000000000", "unknown"):
        assert is_placeholder(value)
    assert not is_placeholder(VALID)
    assert not is_placeholder("1234567894")  # wrong, but not a placeholder


@pytest.mark.unit
def test_checksum_fail_keeps_the_digits_for_reporting() -> None:
    status, cleaned = classify_npi("1234567894")
    assert status is NpiStatus.CHECKSUM_FAIL
    assert cleaned == "1234567894"


@pytest.mark.unit
def test_sentinel_is_checked_before_the_checksum() -> None:
    """1234567890 fails the checksum too; it must classify as the sentinel."""
    assert classify_npi(CHECKSUM_FAIL)[0] is NpiStatus.SENTINEL


# --- phonetics -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Smith", "Smyth"),
        ("Wright", "Rite"),
        ("Knight", "Night"),
        ("Philips", "Filips"),
        ("Catherine", "Katherine"),
        ("Gonzalez", "Gonzales"),
        ("Nguyen", "Nguyen"),
        ("Jackson", "Jaxon"),
    ],
)
def test_homophones_share_a_key(a: str, b: str) -> None:
    assert set(phonetic_keys(a)) & set(phonetic_keys(b)), (phonetic_keys(a), phonetic_keys(b))


@pytest.mark.unit
@pytest.mark.parametrize(("a", "b"), [("Smith", "Jones"), ("Garcia", "Nguyen"), ("Baker", "Walsh")])
def test_unrelated_names_do_not_share_a_key(a: str, b: str) -> None:
    assert not set(phonetic_keys(a)) & set(phonetic_keys(b))


@pytest.mark.unit
def test_alternate_key_captures_the_second_reading() -> None:
    primary, alternate = double_metaphone("Schmidt")
    assert primary != alternate
    assert primary and alternate
    # Schmidt and Smith meet on Smith's alternate, Germanic reading.
    assert set(double_metaphone("Schmidt")) & set(double_metaphone("Smith"))


@pytest.mark.unit
def test_keys_are_bounded_and_uppercase() -> None:
    for name in ("Featherstonehaugh", "Rodriguez", "O'Brien"):
        primary, alternate = double_metaphone(name)
        assert len(primary) <= 4 and len(alternate) <= 4
        assert primary == primary.upper()


@pytest.mark.unit
def test_non_alphabetic_input_yields_no_key() -> None:
    assert double_metaphone("") == ("", "")
    assert double_metaphone("12345") == ("", "")
    assert phonetic_keys("") == ()


@pytest.mark.unit
def test_phonetic_key_returns_the_primary_only() -> None:
    assert phonetic_key("Smith") == double_metaphone("Smith")[0]


@pytest.mark.unit
def test_keys_are_stable_across_calls() -> None:
    """The index and the query must agree; caching must not change answers."""
    assert double_metaphone("Nakamura") == double_metaphone("Nakamura")
    assert phonetic_keys("Nakamura") == phonetic_keys("Nakamura")


# A deliberately varied corpus: Germanic, Slavic, Romance, Greek, Celtic and
# East Asian surnames, plus the English spellings that trip the letter rules.
# It exists to exercise the algorithm's branches, not to pin exact codes.
PHONETIC_CORPUS = [
    "Bach", "Bacher", "Caesar", "Chianti", "Michael", "Charac", "Chorus", "McHugh",
    "Wachtler", "Czerny", "Focaccia", "Mccarthy", "Accident", "Bocca", "Bacci",
    "Ecclesiastes", "Knack", "Edge", "Ledger", "Rodgers", "Wieder", "Schmidt",
    "Ghislane", "Ghent", "Laugh", "Cough", "McLaughlin", "Gnome", "Agnes", "Tagliaro",
    "Gerber", "Gilbert", "Danger", "Manager", "Hierarchy", "Josephine", "Jose",
    "San Jacinto", "Jankowski", "Cabrillo", "Villa", "Sanchez", "Thumb", "Dumber",
    "Palma", "Nunez", "Campbell", "Shepherd", "Rousseau", "Isle", "Island", "Sugar",
    "Schoenberg", "Schenker", "Rascal", "Science", "Wicz", "Filipowicz", "Horowitz",
    "Thomas", "Thompson", "Matthew", "Tchaikovsky", "Nation", "Martial", "Vivian",
    "Wright", "Whalen", "Dowell", "Snowski", "Xavier", "Beaux", "Zhao", "Zorro",
    "Pizza", "Nakamura", "Oyelaran", "Petrova", "Almeida", "Kaur", "Singh", "Ngo",
]


@pytest.mark.unit
@pytest.mark.parametrize("name", PHONETIC_CORPUS)
def test_corpus_produces_bounded_stable_keys(name: str) -> None:
    primary, alternate = double_metaphone(name)
    assert len(primary) <= 4 and len(alternate) <= 4
    assert (primary, alternate) == double_metaphone(name)
    assert set(phonetic_keys(name)) <= {primary, alternate}
    assert all(key for key in phonetic_keys(name))


@pytest.mark.unit
def test_spelling_variants_across_the_corpus_meet() -> None:
    pairs = [
        ("Schmidt", "Schmit"),
        ("Rodgers", "Rogers"),
        ("Meyer", "Meier"),
        ("Carlson", "Karlson"),
        ("Sanchez", "Sanchz"),
        ("Almeida", "Almeda"),
    ]
    for a, b in pairs:
        assert set(phonetic_keys(a)) & set(phonetic_keys(b)), (a, b, phonetic_keys(a), phonetic_keys(b))


@pytest.mark.unit
def test_known_limits_of_the_phonetic_block_are_covered_elsewhere() -> None:
    """Double Metaphone is faithful, which means it does miss some pairs.

    `Thompson` keeps its P and `Thomson` does not, so the two do not share a
    key. This is not a bug to hand-tune away - the algorithm is a published
    one, and quietly diverging from it would break the promise that the Stage 5
    Postgres implementation computes the same keys. The trigram block is what
    carries pairs like this, which is precisely why blocking is a union.
    """
    assert not set(phonetic_keys("Thompson")) & set(phonetic_keys("Thomson"))

    from concordance.matching.blocking import trigrams

    shared = trigrams("THOMPSON") & trigrams("THOMSON")
    assert len(shared) / len(trigrams("THOMPSON") | trigrams("THOMSON")) > 0.4
