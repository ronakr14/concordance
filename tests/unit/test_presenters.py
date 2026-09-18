"""Presenter shapes that need no database: the adjudicator's answer as shown."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from concordance.api.presenters import _adjudication

pytestmark = pytest.mark.unit

CANDIDATES = [
    SimpleNamespace(provider_id="P-1", field_levels={"npi": "EXACT", "dob": "DISAGREE"}),
    SimpleNamespace(provider_id="P-2", field_levels={"npi": "MISSING", "dob": "EXACT"}),
]


def _llm(payload: object) -> dict[str, object]:
    body = payload if isinstance(payload, str) else "```json\n" + json.dumps(payload) + "\n```"
    return {"response": {"content": body}}


def _answer(**overrides: object) -> dict[str, object]:
    return {
        "decision": "MATCH",
        "provider_id": "P-1",
        "confidence": 0.91,
        "evidence_cited": ["P-1.npi"],
        "reasoning": "NPI agrees exactly; the DOB disagreement is a transposition.",
        **overrides,
    }


def test_an_llm_decision_shows_the_validated_answer() -> None:
    result = SimpleNamespace(route="llm")
    shown = _adjudication(result, CANDIDATES, _llm(_answer()))  # type: ignore[arg-type]
    assert shown is not None
    assert shown.evidence_cited == ["P-1.npi"] and shown.provider_id == "P-1"


def test_an_answer_citing_unsupplied_evidence_is_not_shown() -> None:
    result = SimpleNamespace(route="llm")
    invented = _answer(evidence_cited=["P-1.ssn"])
    assert _adjudication(result, CANDIDATES, _llm(invented)) is None  # type: ignore[arg-type]


def test_only_llm_routed_results_have_an_adjudication() -> None:
    assert _adjudication(SimpleNamespace(route="probabilistic"), CANDIDATES, _llm(_answer())) is None  # type: ignore[arg-type]
    assert _adjudication(SimpleNamespace(route="llm"), CANDIDATES, None) is None  # type: ignore[arg-type]
    assert _adjudication(SimpleNamespace(route="llm"), CANDIDATES, _llm("not json at all")) is None  # type: ignore[arg-type]
