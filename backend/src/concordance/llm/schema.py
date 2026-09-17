"""What a model is allowed to say, and how a bad answer is refused.

Everything in this module exists because a language model's output is a
*claim*, not a result. Four independent checks stand between the claim and a
decision that reaches a reviewer, and each one catches a different failure that
was observed rather than imagined:

1. **Extraction.** Models wrap JSON in prose and code fences even when asked
   not to. Refusing those responses would throw away good answers over
   formatting, so the JSON is dug out first.
2. **Shape.** JSON Schema validation, and on failure exactly *one* repair round
   that feeds the validation error back. One, not a loop: a model that cannot
   produce the shape twice will not produce it on the fifth attempt, and a
   retry loop on a paid model is an unbounded bill.
3. **Referential honesty.** `provider_id` must be one of the candidates that
   was supplied. A model naming a provider that was never in the prompt has
   hallucinated an identity, and the answer is void no matter how confident.
4. **Evidence honesty.** Every entry in `evidence_cited` must resolve to a
   field that was actually in the prompt. This is the guard worth the most: it
   is a mechanical, model-free check that catches invented justification, and
   it costs one set intersection.

A response that fails any check becomes `AMBIGUOUS` with the reason recorded.
Never an exception. The system's job is to reconcile providers; an adjudicator
having a bad day is a record for a human, not an outage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

# The vocabulary the model answers in. Deliberately not the engine's `Outcome`:
# `NO_CONFIDENT_MATCH` says "none of these candidates", which is a different
# claim from the engine's `NO_MATCH`, and mapping the two happens explicitly in
# `ai_matcher` rather than by sharing an enum and hoping.
DECISION_MATCH = "MATCH"
DECISION_NO_CONFIDENT_MATCH = "NO_CONFIDENT_MATCH"
DECISION_AMBIGUOUS = "AMBIGUOUS"
DECISIONS = (DECISION_MATCH, DECISION_NO_CONFIDENT_MATCH, DECISION_AMBIGUOUS)

MAX_REASONING_CHARS = 1200
MAX_EVIDENCE_ITEMS = 24

ADJUDICATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "adjudication",
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "provider_id", "confidence", "evidence_cited", "reasoning"],
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "provider_id": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "evidence_cited": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_EVIDENCE_ITEMS,
        },
        "reasoning": {"type": "string", "maxLength": MAX_REASONING_CHARS},
    },
}

_VALIDATOR = Draft202012Validator(ADJUDICATION_SCHEMA)

# ```json ... ``` or ``` ... ```
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class Rejection:
    """Why a response was refused. Recorded on the result, shown in the UI."""

    NO_JSON = "no_json_found"
    INVALID_JSON = "json_did_not_parse"
    SCHEMA = "schema_validation_failed"
    UNKNOWN_PROVIDER = "provider_id_not_in_candidate_set"
    UNSUPPLIED_EVIDENCE = "cited_evidence_not_supplied"
    CONFIDENCE_RANGE = "confidence_out_of_range"
    MATCH_WITHOUT_PROVIDER = "decision_match_without_provider_id"


@dataclass(frozen=True, slots=True)
class Adjudication:
    """A model answer that passed every check."""

    decision: str
    provider_id: str | None
    confidence: float
    evidence_cited: tuple[str, ...]
    reasoning: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "provider_id": self.provider_id,
            "confidence": self.confidence,
            "evidence_cited": list(self.evidence_cited),
            "reasoning": self.reasoning,
        }


@dataclass
class ValidationResult:
    """Either an `Adjudication` or the reason there is not one."""

    adjudication: Adjudication | None = None
    rejection: str | None = None
    detail: str = ""
    repaired: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.adjudication is not None

    @classmethod
    def reject(cls, rejection: str, detail: str = "") -> ValidationResult:
        return cls(rejection=rejection, detail=detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rejection": self.rejection,
            "detail": self.detail,
            "repaired": self.repaired,
            "adjudication": self.adjudication.as_dict() if self.adjudication else None,
        }


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def extract_json(content: str) -> dict[str, Any] | None:
    """Pull one JSON object out of a response that may be wrapped in anything.

    Tried in order of how likely each is to be the answer rather than an
    example: the whole body, then a fenced block, then the outermost
    brace-balanced span. Brace balancing rather than a greedy regex, because a
    nested object inside the reasoning string would truncate a regex match at
    the wrong closing brace.
    """
    text = (content or "").strip()
    if not text:
        return None

    direct = _loads_object(text)
    if direct is not None:
        return direct

    for block in _FENCE.findall(text):
        found = _loads_object(block.strip())
        if found is not None:
            return found

    span = _balanced_object(text)
    if span is not None:
        return _loads_object(span)
    return None


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _balanced_object(text: str) -> str | None:
    """The first brace-balanced span, ignoring braces inside string literals."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def schema_errors(payload: dict[str, Any]) -> list[str]:
    """Every schema violation, shortest path first - this is what repair sees."""
    errors = sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]


def validate(
    content: str,
    *,
    allowed_provider_ids: frozenset[str] | set[str],
    supplied_evidence: frozenset[str] | set[str],
) -> ValidationResult:
    """Run every check against one raw response body.

    `allowed_provider_ids` and `supplied_evidence` come from the
    `AdjudicationRequest` that produced the prompt, so the guard is checking the
    answer against what was actually sent rather than against what the caller
    believes was sent.
    """
    payload = extract_json(content)
    if payload is None:
        return ValidationResult.reject(Rejection.NO_JSON, "no JSON object in the response body")

    errors = schema_errors(payload)
    if errors:
        return ValidationResult.reject(Rejection.SCHEMA, "; ".join(errors[:4]))

    decision = str(payload["decision"])
    provider_id = payload["provider_id"]
    provider_id = None if provider_id is None else str(provider_id)
    confidence = float(payload["confidence"])
    evidence = tuple(str(e) for e in payload.get("evidence_cited") or ())
    reasoning = str(payload.get("reasoning") or "").strip()

    # An out-of-range confidence is not a formatting slip to be clamped. It
    # means the model is not answering on the scale it was given, so its number
    # carries no information and the answer is downgraded rather than rescued.
    if not 0.0 <= confidence <= 1.0:
        return ValidationResult.reject(
            Rejection.CONFIDENCE_RANGE, f"confidence {confidence} outside [0,1]"
        )

    if decision == DECISION_MATCH and not provider_id:
        return ValidationResult.reject(
            Rejection.MATCH_WITHOUT_PROVIDER, "decision MATCH names no provider"
        )

    if provider_id is not None and provider_id not in allowed_provider_ids:
        return ValidationResult.reject(
            Rejection.UNKNOWN_PROVIDER,
            f"{provider_id!r} was not among the candidates supplied",
        )

    unsupplied = sorted(set(evidence) - set(supplied_evidence))
    if unsupplied:
        return ValidationResult.reject(
            Rejection.UNSUPPLIED_EVIDENCE, f"cited {unsupplied[:5]} which were never supplied"
        )

    return ValidationResult(
        adjudication=Adjudication(
            decision=decision,
            provider_id=provider_id,
            confidence=confidence,
            evidence_cited=evidence,
            reasoning=reasoning[:MAX_REASONING_CHARS],
        )
    )


def repair_instruction(result: ValidationResult, raw: str) -> str:
    """The follow-up turn sent after a failed validation.

    Quotes the offending output back and states the violation in the schema's
    own words. Telling a model only "that was invalid" reliably produces the
    same invalid answer again; telling it which property failed and why
    produces a corrected one most of the time.
    """
    excerpt = (raw or "").strip()
    if len(excerpt) > 600:
        excerpt = excerpt[:600] + " ... [truncated]"
    return (
        "Your previous response was rejected.\n\n"
        f"Reason: {result.rejection}\nDetail: {result.detail}\n\n"
        "Your previous response was:\n"
        f"<<<\n{excerpt}\n>>>\n\n"
        "Reply again with a single JSON object and nothing else - no prose, no code fence. "
        "It must have exactly these keys: decision, provider_id, confidence, evidence_cited, "
        f"reasoning. decision must be one of {list(DECISIONS)}. provider_id must be one of the "
        "candidate provider_id values given in the request, or null. confidence must be a number "
        "between 0 and 1. Every evidence_cited entry must be one of the evidence keys listed in "
        "the request, copied exactly."
    )


__all__ = [
    "ADJUDICATION_SCHEMA",
    "DECISIONS",
    "DECISION_AMBIGUOUS",
    "DECISION_MATCH",
    "DECISION_NO_CONFIDENT_MATCH",
    "Adjudication",
    "Rejection",
    "ValidationResult",
    "extract_json",
    "repair_instruction",
    "schema_errors",
    "validate",
]
