"""The guard. Every check that stands between a model's claim and a decision."""

from __future__ import annotations

import json

import pytest

from concordance.llm.schema import (
    DECISION_AMBIGUOUS,
    DECISION_MATCH,
    Rejection,
    extract_json,
    repair_instruction,
    validate,
)

pytestmark = pytest.mark.unit

ALLOWED = frozenset({"PRV-1", "PRV-2"})
SUPPLIED = frozenset({"PRV-1.last_name", "PRV-1.dob", "PRV-2.last_name"})


def body(**overrides: object) -> str:
    payload = {
        "decision": DECISION_MATCH,
        "provider_id": "PRV-1",
        "confidence": 0.9,
        "evidence_cited": ["PRV-1.last_name"],
        "reasoning": "names and date of birth agree exactly",
    }
    payload.update(overrides)
    return json.dumps(payload)


# -- extraction ------------------------------------------------------------


def test_extracts_bare_json() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extracts_from_a_code_fence() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extracts_from_surrounding_prose() -> None:
    assert extract_json('Sure! Here is my answer:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_brace_balancing_survives_a_nested_object() -> None:
    assert extract_json('text {"a": {"b": 2}} tail') == {"a": {"b": 2}}


def test_brace_balancing_ignores_braces_inside_strings() -> None:
    assert extract_json('{"r": "a } brace"}') == {"r": "a } brace"}


@pytest.mark.parametrize("content", ["", "   ", "no json at all", "[1, 2, 3]"])
def test_no_object_returns_none(content: str) -> None:
    assert extract_json(content) is None


# -- validation ------------------------------------------------------------


def test_a_good_response_validates() -> None:
    result = validate(body(), allowed_provider_ids=ALLOWED, supplied_evidence=SUPPLIED)
    assert result.ok
    assert result.adjudication is not None
    assert result.adjudication.provider_id == "PRV-1"
    assert result.adjudication.confidence == pytest.approx(0.9)


def test_unparseable_body_is_rejected_not_raised() -> None:
    result = validate(
        "I decline to answer.", allowed_provider_ids=ALLOWED, supplied_evidence=SUPPLIED
    )
    assert not result.ok
    assert result.rejection == Rejection.NO_JSON


def test_unknown_decision_fails_the_schema() -> None:
    result = validate(
        body(decision="DEFINITELY"), allowed_provider_ids=ALLOWED, supplied_evidence=SUPPLIED
    )
    assert result.rejection == Rejection.SCHEMA


def test_missing_key_fails_the_schema() -> None:
    result = validate(
        json.dumps({"decision": DECISION_MATCH}),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.rejection == Rejection.SCHEMA


def test_extra_key_fails_the_schema() -> None:
    result = validate(
        body(sources=["the internet"]),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.rejection == Rejection.SCHEMA


def test_provider_outside_the_candidate_set_is_rejected() -> None:
    result = validate(
        body(provider_id="PRV-999", evidence_cited=[]),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.rejection == Rejection.UNKNOWN_PROVIDER


def test_citing_unsupplied_evidence_is_rejected() -> None:
    """The anti-hallucination guard: invented justification voids the answer."""
    result = validate(
        body(evidence_cited=["PRV-1.last_name", "PRV-1.medical_license_board_record"]),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.rejection == Rejection.UNSUPPLIED_EVIDENCE
    assert "medical_license_board_record" in result.detail


def test_evidence_for_a_candidate_that_was_not_supplied_is_rejected() -> None:
    result = validate(
        body(evidence_cited=["PRV-2.dob"]),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.rejection == Rejection.UNSUPPLIED_EVIDENCE


@pytest.mark.parametrize("confidence", [-0.1, 1.5, 42])
def test_out_of_range_confidence_is_rejected(confidence: float) -> None:
    result = validate(
        body(confidence=confidence), allowed_provider_ids=ALLOWED, supplied_evidence=SUPPLIED
    )
    assert result.rejection == Rejection.CONFIDENCE_RANGE


def test_match_without_a_provider_is_rejected() -> None:
    result = validate(
        body(provider_id=None, evidence_cited=[]),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.rejection == Rejection.MATCH_WITHOUT_PROVIDER


def test_ambiguous_with_a_null_provider_is_valid() -> None:
    """Abstention is a correct answer, so it must pass every check."""
    result = validate(
        body(decision=DECISION_AMBIGUOUS, provider_id=None, confidence=0.5, evidence_cited=[]),
        allowed_provider_ids=ALLOWED,
        supplied_evidence=SUPPLIED,
    )
    assert result.ok
    assert result.adjudication is not None
    assert result.adjudication.provider_id is None


# -- repair ----------------------------------------------------------------


def test_repair_instruction_names_the_violation_and_quotes_the_output() -> None:
    result = validate("nonsense", allowed_provider_ids=ALLOWED, supplied_evidence=SUPPLIED)
    text = repair_instruction(result, "nonsense")
    assert Rejection.NO_JSON in text
    assert "nonsense" in text
    assert "evidence_cited" in text


def test_repair_instruction_truncates_a_huge_body() -> None:
    result = validate("x" * 5000, allowed_provider_ids=ALLOWED, supplied_evidence=SUPPLIED)
    text = repair_instruction(result, "x" * 5000)
    assert "[truncated]" in text
    assert len(text) < 2000
