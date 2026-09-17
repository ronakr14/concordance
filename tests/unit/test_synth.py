"""Stage 1: NPIs, corruption families, entities and scenario coverage."""

from __future__ import annotations

from datetime import date

import pytest

from concordance.matching.npi_validator import (
    is_placeholder,
    is_valid_npi,
    make_npi,
    npi_check_digit,
)
from concordance.synth.corruption import OP_BY_NAME, OPS, CorruptionEngine, op_catalogue
from concordance.synth.dates import shift_years, with_day
from concordance.synth.entities import _acronym, generate_entities
from concordance.synth.reference import load_reference
from concordance.synth.rng import stream
from concordance.synth.sanctions import SCENARIOS, SanctionGenerator

SPEC_SCENARIOS = {
    "exact_npi", "missing_npi", "sentinel_npi", "name_variation", "address_variation",
    "ambiguous", "false_positive_bait", "unmatched",
}


# --- NPI -----------------------------------------------------------------


@pytest.mark.unit
def test_npi_check_digit_matches_published_example() -> None:
    # 1234567893 is the worked example in the CMS NPI check-digit guidance.
    assert npi_check_digit("123456789") == 3
    assert make_npi("123456789") == "1234567893"


@pytest.mark.unit
def test_valid_npi_rejects_sentinels_and_bad_checksums() -> None:
    good = make_npi("100000000")
    assert is_valid_npi(good)
    assert not is_valid_npi(good[:9] + str((int(good[9]) + 1) % 10))
    assert not is_valid_npi("1111111111")
    assert not is_valid_npi("12345")
    assert not is_valid_npi(None)


@pytest.mark.unit
def test_placeholder_detection() -> None:
    for value in ("", "  ", "N/A", "unknown", "-", "0000000000"):
        assert is_placeholder(value), value
    assert not is_placeholder(make_npi("100000001"))


# --- dates ---------------------------------------------------------------


@pytest.mark.unit
def test_leap_day_shifts_do_not_raise() -> None:
    assert shift_years(date(2024, 2, 29), 1) == date(2025, 2, 28)
    assert with_day(date(2023, 2, 28), 31) == date(2023, 2, 28)


# --- corruption ----------------------------------------------------------


@pytest.mark.unit
def test_every_documented_family_is_implemented() -> None:
    families = {op["family"] for op in op_catalogue()}
    assert families == {"name", "npi", "dob", "address", "license"}
    # The checklist names 28 distinct operations across the five families.
    assert len(OPS) >= 28


@pytest.mark.unit
def test_provider_side_excludes_spreadsheet_only_operations() -> None:
    engine = CorruptionEngine(level=0.5, seed=1, side="provider")
    available = {op.name for ops in engine._ops_by_family.values() for op in ops}
    assert "alt_format" not in available
    assert "placeholder_text" not in available


@pytest.mark.unit
def test_zero_level_is_a_no_op() -> None:
    engine = CorruptionEngine(level=0.0, seed=1)
    record = {"first_name": "Robert", "last_name": "Smith", "npi": "1234567893"}
    out, profile = engine.apply(record)
    assert out == record
    assert profile == []


@pytest.mark.unit
def test_level_outside_range_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"\[0.0, 0.9\]"):
        CorruptionEngine(level=1.0, seed=1)


@pytest.mark.unit
def test_profile_records_what_changed() -> None:
    engine = CorruptionEngine(level=0.9, seed=3)
    record = {
        "first_name": "Robert", "last_name": "Smith", "npi": "1234567893",
        "dob": date(1970, 5, 4), "address_line1": "12 Oak Street", "address_line2": "Suite 4",
        "city": "Austin", "state": "TX", "zip": "78701",
        "license_number": "MD12345", "license_state": "TX", "is_organization": False,
    }
    out, profile = engine.apply(record)
    assert profile, "at level 0.9 at least one family should fire"
    for entry in profile:
        for field, after in entry["after"].items():
            expected = out[field].isoformat() if isinstance(out[field], date) else out[field]
            assert after == expected


@pytest.mark.unit
def test_nickname_operation_uses_the_reference_table() -> None:
    ref = load_reference()
    rng = stream(11, "test", "nickname")
    change = OP_BY_NAME["nickname"].fn({"first_name": "Robert"}, rng, ref)
    assert change is not None
    assert change["first_name"] in ref.nickname_of["Robert"]


@pytest.mark.unit
def test_corruption_is_reproducible_under_a_seed() -> None:
    record = {"first_name": "Margaret", "last_name": "Nguyen", "npi": "1234567893",
              "dob": date(1965, 3, 2), "state": "CA", "zip": "90001",
              "license_number": "A123456", "license_state": "CA", "is_organization": False}
    a = CorruptionEngine(level=0.6, seed=42).apply(dict(record))
    b = CorruptionEngine(level=0.6, seed=42).apply(dict(record))
    assert a == b


# --- entities ------------------------------------------------------------


@pytest.mark.unit
def test_entities_are_deterministic_and_well_formed() -> None:
    a = generate_entities(400, seed=5)
    b = generate_entities(400, seed=5)
    assert a == b
    assert len(a) == 400
    assert len({e["provider_id"] for e in a}) == 400
    assert len({e["npi"] for e in a}) == 400
    assert all(is_valid_npi(e["npi"]) for e in a)


@pytest.mark.unit
def test_a_different_seed_gives_a_different_dataset() -> None:
    assert generate_entities(200, seed=5) != generate_entities(200, seed=6)


@pytest.mark.unit
def test_organizations_carry_org_fields_and_no_person_fields() -> None:
    orgs = [e for e in generate_entities(600, seed=8) if e["is_organization"]]
    assert orgs, "organizations must be present"
    for org in orgs:
        assert org["organization_name"]
        assert org["ein"] and org["ein"][2] == "-"
        assert org["npi"].startswith("2")  # type 2 NPI
        assert org["dob"] is None
        assert org["first_name"] is None and org["last_name"] is None


@pytest.mark.unit
def test_addresses_are_internally_consistent() -> None:
    ref = load_reference()
    for entity in generate_entities(500, seed=9):
        zips = {z for _, z in ref.cities_by_state[entity["state"]]}
        cities = {c for c, _ in ref.cities_by_state[entity["state"]]}
        assert entity["city"] in cities
        assert entity["zip"][:3] in zips


@pytest.mark.unit
def test_planted_clusters_exist_with_their_defining_property() -> None:
    entities = generate_entities(1500, seed=10)
    clusters: dict[str, list[dict]] = {}
    for e in entities:
        if e["_cluster"]:
            clusters.setdefault(e["_cluster"], []).append(e)

    kinds = {cid.split("-", 1)[1] for cid in clusters}
    assert {"twins", "father_son", "common_name"} <= kinds

    for cid, members in clusters.items():
        if len(members) < 2:
            continue
        if cid.endswith("twins"):
            assert len({m["last_name"] for m in members}) == 1
            assert len({m["dob"] for m in members}) == 1
        elif cid.endswith("father_son"):
            assert {m["suffix"] for m in members} == {"Sr", "Jr"}
            assert len({m["address_line1"] for m in members}) == 1
            years = sorted(m["dob"].year for m in members)
            assert years[1] - years[0] == 28
            assert years[1] <= 2000, "the son must still be of working age"
        elif cid.endswith("common_name"):
            assert len({(m["first_name"], m["last_name"], m["state"]) for m in members}) == 1


@pytest.mark.unit
def test_surname_distribution_has_a_real_long_tail() -> None:
    """Rare names must be rare, or the EM fit learns nothing from a surname."""
    entities = [e for e in generate_entities(3000, seed=12) if not e["is_organization"]]
    counts: dict[str, int] = {}
    for e in entities:
        counts[e["last_name"]] = counts.get(e["last_name"], 0) + 1
    ordered = sorted(counts.values(), reverse=True)
    assert ordered[0] >= 3 * ordered[len(ordered) // 2]


@pytest.mark.unit
def test_acronym_helper_skips_corporate_noise() -> None:
    assert _acronym("Riverside Family Practice Group LLC") == "RFPG"


# --- sanction records ----------------------------------------------------


@pytest.mark.unit
def test_scenario_shares_sum_to_one() -> None:
    assert round(sum(share for share, _ in SCENARIOS.values()), 6) == 1.0


@pytest.mark.unit
def test_all_eight_spec_scenarios_are_generated() -> None:
    entities = generate_entities(2000, seed=13)
    build = SanctionGenerator(entities, corruption=0.5, seed=13).build(600)
    assert len(build.records) == 600
    assert len(build.truth) == 600
    assert set(build.scenario_counts) >= SPEC_SCENARIOS
    assert {t["sanction_record_id"] for t in build.truth} == {r["record_id"] for r in build.records}


@pytest.mark.unit
def test_scenarios_hold_their_defining_property() -> None:
    entities = generate_entities(2000, seed=14)
    build = SanctionGenerator(entities, corruption=0.6, seed=14).build(600)
    by_id = {r["record_id"]: r for r in build.records}
    providers = {e["provider_id"]: e for e in entities}

    for truth in build.truth:
        rec = by_id[truth["sanction_record_id"]]
        tag = truth["scenario_tag"]
        if tag == "exact_npi":
            expected = providers[truth["expected_provider_id"]]["npi"]
            assert rec["npi"] == expected
        elif tag == "missing_npi":
            assert rec["npi"] is None
        elif tag == "sentinel_npi":
            assert is_placeholder(rec["npi"])
        elif tag in {
            "unmatched",
            "false_positive_bait",
            "org_unmatched",
            "org_false_positive_bait",
        }:
            assert truth["expected_outcome"] == "NO_MATCH"
            assert truth["expected_provider_id"] is None
        elif tag == "ambiguous":
            assert truth["expected_outcome"] == "AMBIGUOUS"
            assert len(truth["corruption_profile"]["plausible_provider_ids"]) > 1
        elif tag == "org_type_disagreement":
            provider = providers[truth["expected_provider_id"]]
            assert provider["is_organization"] and not rec["is_organization"]


@pytest.mark.unit
def test_ambiguous_records_keep_nothing_that_separates_their_cluster() -> None:
    """The whole point of the scenario: no field in the record may decide it.

    A `common_name` cluster shares first name, last name and state and differs
    in everything else, so every differing field has to be absent or the record
    resolves to one member and AMBIGUOUS becomes the wrong answer.
    """
    entities = generate_entities(2000, seed=21)
    build = SanctionGenerator(entities, corruption=0.5, seed=21).build(600)
    by_id = {r["record_id"]: r for r in build.records}
    providers = {e["provider_id"]: e for e in entities}

    seen = 0
    for truth in build.truth:
        if truth["scenario_tag"] != "ambiguous":
            continue
        seen += 1
        rec = by_id[truth["sanction_record_id"]]
        for field in ("dob", "address_line1", "license_number"):
            # Blank rather than `is None`: the corruption engine can render a
            # stripped field as an empty string, which normalization reads as
            # missing just the same.
            assert not rec[field], f"{field} survived into an ambiguous record"
        # A placeholder NPI is allowed and realistic - a real file writes "N/A"
        # rather than leaving the column empty - because the NPI validator
        # classifies it as missing rather than as a value to match on.
        assert not rec["npi"] or is_placeholder(rec["npi"])
        plausible = truth["corruption_profile"]["plausible_provider_ids"]
        assert len(plausible) > 1
        assert truth["corruption_profile"]["cluster"].endswith("common_name")
        # Every plausible provider is equally consistent with what is left.
        # Everything the record still carries is shared by every member, so
        # none of it can separate them.
        for shared in ("first_name", "last_name", "city", "state", "zip"):
            assert len({providers[pid][shared] for pid in plausible}) == 1, shared
    assert seen, "no ambiguous records were generated"


@pytest.mark.unit
def test_organizations_have_negatives_of_their_own() -> None:
    """Without these the organization accept threshold is not identifiable."""
    entities = generate_entities(2000, seed=22)
    build = SanctionGenerator(entities, corruption=0.5, seed=22).build(600)
    by_id = {r["record_id"]: r for r in build.records}
    known_npis = {e["npi"] for e in entities}
    known_eins = {e["ein"] for e in entities if e.get("ein")}

    negatives = 0
    for truth in build.truth:
        tag = truth["scenario_tag"]
        if tag not in {"org_unmatched", "org_false_positive_bait"}:
            continue
        negatives += 1
        rec = by_id[truth["sanction_record_id"]]
        assert truth["expected_provider_id"] is None
        assert rec["is_organization"] is True
        if rec["npi"]:
            assert rec["npi"] not in known_npis
        if rec["ein"]:
            assert rec["ein"] not in known_eins
    assert negatives, "the organization model needs negatives to learn a cut"


@pytest.mark.unit
def test_individual_unmatched_scenario_is_individual_only() -> None:
    entities = generate_entities(1500, seed=23)
    build = SanctionGenerator(entities, corruption=0.4, seed=23).build(400)
    by_id = {r["record_id"]: r for r in build.records}
    for truth in build.truth:
        if truth["scenario_tag"] == "unmatched":
            assert by_id[truth["sanction_record_id"]]["is_organization"] is False


@pytest.mark.unit
def test_unmatched_records_point_at_nobody_in_the_master() -> None:
    entities = generate_entities(1500, seed=15)
    build = SanctionGenerator(entities, corruption=0.5, seed=15).build(400)
    known_npis = {e["npi"] for e in entities}
    for truth, rec in zip(build.truth, build.records, strict=True):
        if truth["scenario_tag"] == "unmatched" and rec["npi"]:
            assert rec["npi"] not in known_npis


@pytest.mark.unit
def test_some_records_are_reinstated_in_the_past() -> None:
    entities = generate_entities(1500, seed=16)
    build = SanctionGenerator(entities, corruption=0.4, seed=16).build(500)
    reinstated = [r for r in build.records if r["reinstatement_date"]]
    assert reinstated, "the workflow needs providers who are no longer excluded"
    assert any(r["reinstatement_date"] < date(2026, 9, 14) for r in reinstated)
