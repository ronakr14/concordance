"""Blocking on a fixture small enough that the right answer is known exactly."""

from __future__ import annotations

from datetime import date

import pytest

from concordance.domain import Provider, SanctionRecord
from concordance.matching.blocking import (
    BLOCK_EIN,
    BLOCK_LICENSE,
    BLOCK_NPI,
    BLOCK_ORG_ACRONYM,
    BLOCK_PHONETIC_STATE,
    BLOCK_STATE_DOB,
    BLOCK_TRIGRAM,
    BLOCK_ZIP_NAME3,
    InMemoryCandidateGenerator,
    TrigramIndex,
    trigrams,
)
from concordance.matching.npi_validator import make_npi
from concordance.protocols import CandidateGenerator

NPI_ALICE = make_npi("100000001")
NPI_BOB = make_npi("100000002")
NPI_TWIN = make_npi("100000003")
NPI_ORG = make_npi("200000001")
NPI_FAR = make_npi("100000009")

ALICE = Provider(
    provider_id="P-ALICE",
    npi=NPI_ALICE,
    first_name="Margaret",
    last_name="Thompson",
    dob=date(1972, 4, 11),
    address_line1="14 Oak Street",
    city="Austin",
    state="TX",
    zip="78701",
    license_number="A012345",
    license_state="TX",
)
BOB = Provider(
    provider_id="P-BOB",
    npi=NPI_BOB,
    first_name="Robert",
    last_name="Nakamura",
    dob=date(1965, 9, 2),
    address_line1="900 Cedar Avenue",
    city="Seattle",
    state="WA",
    zip="98101",
    license_number="RN998877",
    license_state="WA",
)
TWIN = Provider(  # same surname, state and DOB as Alice - the hard neighbour
    provider_id="P-TWIN",
    npi=NPI_TWIN,
    first_name="Michael",
    last_name="Thompson",
    dob=date(1972, 4, 11),
    address_line1="14 Oak Street",
    city="Austin",
    state="TX",
    zip="78701",
    license_number="A012999",
    license_state="TX",
)
FAR = Provider(  # shares nothing with anybody
    provider_id="P-FAR",
    npi=NPI_FAR,
    first_name="Ingrid",
    last_name="Solberg",
    dob=date(1988, 1, 30),
    address_line1="7 Fjord Lane",
    city="Fargo",
    state="ND",
    zip="58102",
    license_number="ND777111",
    license_state="ND",
)
ORG = Provider(
    provider_id="P-ORG",
    npi=NPI_ORG,
    is_organization=True,
    organization_name="Riverside Family Practice Group LLC",
    ein="123456789",
    address_line1="55 River Road",
    city="Austin",
    state="TX",
    zip="78704",
)

FIXTURE = [ALICE, BOB, TWIN, FAR, ORG]


def build(max_candidates: int = 50) -> InMemoryCandidateGenerator:
    generator = InMemoryCandidateGenerator(max_candidates=max_candidates)
    generator.build_from(FIXTURE)
    return generator


def ids(generator: InMemoryCandidateGenerator, record: SanctionRecord) -> list[str]:
    return [c.provider_id for c in generator.candidates(record)]


# --- trigrams ------------------------------------------------------------


@pytest.mark.unit
def test_trigrams_pad_and_cover() -> None:
    assert trigrams("SMITH") == frozenset({"  S", " SM", "SMI", "MIT", "ITH", "TH "})
    assert trigrams("") == frozenset()
    assert trigrams("A B") == trigrams("AB")  # spaces are not part of the key


@pytest.mark.unit
def test_trigram_index_respects_its_floor() -> None:
    index = TrigramIndex(floor=0.4)
    index.add(0, "MARGARET THOMPSON")
    index.add(1, "MICHAEL THOMPSON")
    index.add(2, "INGRID SOLBERG")
    hits = dict(index.query("MARGARET THOMPSON", limit=10))
    assert 0 in hits and hits[0] == pytest.approx(1.0)
    assert 2 not in hits


@pytest.mark.unit
def test_trigram_index_skips_overlong_postings() -> None:
    """A trigram shared by half the file carries no information and costs a lot."""
    index = TrigramIndex(floor=0.1, max_posting=2)
    for doc_id in range(5):
        index.add(doc_id, "SAME")
    assert index.query("SAME", limit=10) == []


@pytest.mark.unit
def test_trigram_results_are_ordered_deterministically() -> None:
    index = TrigramIndex(floor=0.0)
    for doc_id, name in enumerate(["SMITH", "SMITHE", "SMYTH", "SMITH"]):
        index.add(doc_id, name)
    first = index.query("SMITH", limit=4)
    assert first == index.query("SMITH", limit=4)
    assert [d for d, _ in first][:2] == [0, 3]  # exact matches first, by doc order


# --- protocol ------------------------------------------------------------


@pytest.mark.unit
def test_generator_satisfies_the_candidate_generator_protocol() -> None:
    generator: CandidateGenerator = build()
    assert isinstance(generator, CandidateGenerator)
    assert len(build()) == len(FIXTURE)


@pytest.mark.unit
def test_querying_before_building_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="build"):
        InMemoryCandidateGenerator().candidates(SanctionRecord(record_id="S", last_name="X"))


# --- individual blocks ---------------------------------------------------


@pytest.mark.unit
def test_valid_npi_finds_exactly_the_right_provider() -> None:
    generator = build()
    record = SanctionRecord(record_id="S1", npi=NPI_ALICE)
    candidates = generator.candidates(record)
    assert [c.provider_id for c in candidates] == ["P-ALICE"]
    assert candidates[0].blocking_keys == (BLOCK_NPI,)


@pytest.mark.unit
def test_an_invalid_npi_does_not_block() -> None:
    """A sentinel must not act as an identifier - that is the whole point."""
    generator = build()
    assert ids(generator, SanctionRecord(record_id="S2", npi="0000000000")) == []


@pytest.mark.unit
def test_state_and_dob_block_tolerates_an_off_by_one_year() -> None:
    generator = build()
    record = SanctionRecord(record_id="S3", state="TX", dob="1973-04-11", last_name="Zzzzz")
    found = ids(generator, record)
    assert "P-ALICE" in found and "P-TWIN" in found


@pytest.mark.unit
def test_phonetic_block_survives_a_misspelled_surname() -> None:
    generator = build()
    record = SanctionRecord(record_id="S4", last_name="Tompson", state="TX")
    candidates = generator.candidates(record)
    found = {c.provider_id for c in candidates}
    assert {"P-ALICE", "P-TWIN"} <= found
    blocks = {b for c in candidates for b in c.blocking_keys}
    assert BLOCK_PHONETIC_STATE in blocks


@pytest.mark.unit
def test_zip_and_name_prefix_block_works_without_a_dob() -> None:
    generator = build()
    record = SanctionRecord(record_id="S5", last_name="Thompson", zip="78701-9999")
    found = ids(generator, record)
    assert "P-ALICE" in found


@pytest.mark.unit
def test_license_block_matches_across_formatting_variants() -> None:
    generator = build()
    record = SanctionRecord(record_id="S6", license_number="A-012345", license_state="Texas")
    candidates = generator.candidates(record)
    assert [c.provider_id for c in candidates] == ["P-ALICE"]
    assert BLOCK_LICENSE in candidates[0].blocking_keys


@pytest.mark.unit
def test_trigram_block_carries_a_name_with_no_other_key() -> None:
    """No NPI, no DOB, no state, no ZIP - only a slightly wrong name."""
    generator = build()
    record = SanctionRecord(record_id="S7", first_name="Margret", last_name="Thomson")
    candidates = generator.candidates(record)
    assert "P-ALICE" in {c.provider_id for c in candidates}
    assert BLOCK_TRIGRAM in {b for c in candidates for b in c.blocking_keys}


@pytest.mark.unit
def test_a_nickname_still_blocks() -> None:
    generator = build()
    record = SanctionRecord(record_id="S8", first_name="Bob", last_name="Nakamura", state="WA")
    assert "P-BOB" in ids(generator, record)


@pytest.mark.unit
def test_an_unrelated_record_returns_nothing() -> None:
    generator = build()
    record = SanctionRecord(record_id="S9", first_name="Quentin", last_name="Xylophone", state="VT")
    assert ids(generator, record) == []


# --- organization blocks -------------------------------------------------


@pytest.mark.unit
def test_ein_blocks_an_organization() -> None:
    generator = build()
    record = SanctionRecord(record_id="S10", is_organization=True, ein="12-3456789")
    candidates = generator.candidates(record)
    assert [c.provider_id for c in candidates] == ["P-ORG"]
    assert BLOCK_EIN in candidates[0].blocking_keys


@pytest.mark.unit
def test_acronym_blocks_against_the_expanded_name() -> None:
    generator = build()
    record = SanctionRecord(record_id="S11", is_organization=True, organization_name="RFPG")
    candidates = generator.candidates(record)
    assert "P-ORG" in {c.provider_id for c in candidates}
    assert BLOCK_ORG_ACRONYM in {b for c in candidates for b in c.blocking_keys}


@pytest.mark.unit
def test_an_organization_filed_as_a_person_still_blocks() -> None:
    generator = build()
    record = SanctionRecord(
        record_id="S12",
        is_organization=False,
        last_name="Riverside Family Practice Group LLC",
        state="TX",
    )
    assert "P-ORG" in ids(generator, record)


# --- union, cap, determinism --------------------------------------------


@pytest.mark.unit
def test_candidates_are_deduped_and_report_every_block_that_found_them() -> None:
    generator = build()
    record = SanctionRecord(
        record_id="S13",
        npi=NPI_ALICE,
        first_name="Margaret",
        last_name="Thompson",
        dob="1972-04-11",
        state="TX",
        zip="78701",
        license_number="A012345",
        license_state="TX",
    )
    candidates = generator.candidates(record)
    assert len({c.provider_id for c in candidates}) == len(candidates)
    alice = next(c for c in candidates if c.provider_id == "P-ALICE")
    assert {BLOCK_NPI, BLOCK_STATE_DOB, BLOCK_PHONETIC_STATE, BLOCK_ZIP_NAME3, BLOCK_LICENSE} <= set(
        alice.blocking_keys
    )
    # The twin shares the name block but not the identifier ones.
    twin = next(c for c in candidates if c.provider_id == "P-TWIN")
    assert BLOCK_NPI not in twin.blocking_keys


@pytest.mark.unit
def test_the_best_blocked_candidate_ranks_first() -> None:
    generator = build()
    record = SanctionRecord(
        record_id="S14",
        npi=NPI_ALICE,
        last_name="Thompson",
        dob="1972-04-11",
        state="TX",
        zip="78701",
    )
    assert generator.candidates(record)[0].provider_id == "P-ALICE"


@pytest.mark.unit
def test_the_cap_is_respected() -> None:
    generator = build(max_candidates=2)
    record = SanctionRecord(record_id="S15", last_name="Thompson", state="TX", zip="78701")
    assert len(generator.candidates(record)) <= 2


@pytest.mark.unit
def test_candidate_sets_are_identical_across_repeated_runs() -> None:
    record = SanctionRecord(record_id="S16", last_name="Thompson", state="TX", dob="1972-04-11")
    first = [(c.provider_id, c.blocking_keys) for c in build().candidates(record)]
    second = [(c.provider_id, c.blocking_keys) for c in build().candidates(record)]
    assert first == second


@pytest.mark.unit
def test_index_statistics_are_reported() -> None:
    generator = build()
    stats = generator.stats.as_dict()
    assert stats["providers"] == len(FIXTURE)
    assert stats["individuals"] == 4
    assert stats["organizations"] == 1
    assert stats["build_seconds"] >= 0
    assert generator.block_key_counts()[BLOCK_NPI] == len(FIXTURE)


@pytest.mark.unit
def test_normalized_providers_are_retrievable_for_debugging() -> None:
    generator = build()
    assert generator.normalized_provider("P-ALICE") is not None
    assert generator.normalized_provider("nobody") is None
