"""Proposing a column mapping, and checking a confirmed one (PLAN 11.1).

**Proposal is a suggestion, confirmation is the contract.** The analyst sees the
proposed mapping beside sample values and fixes what is wrong before anything is
ingested. That is why the heuristic can afford to be simple and legible - a
header vocabulary and a scoring rule someone can read - rather than clever: its
misses are caught by a person, its hits save that person typing.

**A stored mapping beats a guess.** If the source authority is named, or its
headers match a mapping already saved for some authority, that mapping is
proposed verbatim. The second upload from a source should need no mapping step
at all, which is the whole reason mappings are persisted.

**Scoring.** A header is folded to tokens - lower-cased, split on punctuation,
case changes and digit boundaries, with the abbreviations exclusion lists
actually use expanded (`prov_last_nm` becomes `provider last name`). Against
each field's synonyms it then scores:

- `1.0` when the folded header *is* a synonym;
- `0.75` to `0.95` when every token of a synonym appears in the header, higher
  the more of the header the synonym explains (`Individual Last Name` contains
  `last name`);
- up to `0.8` on character similarity otherwise, which is what catches a typo.

Pairs are then assigned greedily from the highest score down, one field per
header and one header per field, above a floor of `0.6`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from concordance.sanctions.canonical import BY_NAME, CANONICAL_FIELDS, FIELDS, NAME_FIELDS

#: Below this a proposal is noise, and an empty cell is more honest.
PROPOSAL_FLOOR = 0.6

#: Whole-token rewrites. Keys are what exclusion-list headers abbreviate to.
ABBREVIATIONS: dict[str, str] = {
    "nm": "name",
    "nme": "name",
    "dt": "date",
    "st": "state",
    "num": "number",
    "no": "number",
    "nbr": "number",
    "addr": "address",
    "prov": "provider",
    "lic": "license",
    "svc": "service",
    "sfx": "suffix",
    "mid": "middle",
    "init": "initial",
    "cd": "code",
    "excl": "exclusion",
    "eff": "effective",
    "term": "termination",
    "rein": "reinstatement",
    "reinstate": "reinstatement",
    "fname": "first name",
    "lname": "last name",
    "firstname": "first name",
    "lastname": "last name",
    "midname": "middle name",
    "busname": "business name",
    "reckey": "record key",
    "excltype": "exclusion type",
    "excldate": "exclusion date",
    "reindate": "reinstatement date",
    "dob": "date of birth",
    "zipcode": "zip code",
    "birthdate": "birth date",
    "fein": "ein",
    "id": "id",
}

#: Tokens that carry no meaning in a header.
STOPWORDS = frozenset({"of", "the", "a", "an", "for"})

_SPLIT = re.compile(r"[^0-9a-zA-Z#]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def fold(header: str) -> tuple[str, ...]:
    """A header as the tokens it is scored on."""
    spaced = _CAMEL.sub(" ", header)
    tokens: list[str] = []
    for raw in _SPLIT.split(spaced):
        if not raw:
            continue
        token = raw.lower().replace("#", "number") or raw
        tokens.extend(ABBREVIATIONS.get(token, token).split())
    return tuple(t for t in tokens if t not in STOPWORDS)


_FOLDED_SYNONYMS: dict[str, tuple[tuple[str, ...], ...]] = {
    f.name: tuple(fold(s) for s in f.synonyms) for f in FIELDS
}


def score(header: str, field_name: str) -> float:
    """How well `header` reads as `field_name`, in [0, 1]."""
    tokens = fold(header)
    if not tokens:
        return 0.0
    joined = " ".join(tokens)
    best = 0.0
    for synonym in _FOLDED_SYNONYMS[field_name]:
        if tokens == synonym:
            return 1.0
        if set(synonym) <= set(tokens):
            best = max(best, 0.75 + 0.2 * len(synonym) / len(tokens))
        else:
            best = max(best, 0.8 * fuzz.ratio(joined, " ".join(synonym)) / 100.0)
    return round(best, 4)


@dataclass
class Proposal:
    """A proposed mapping and where it came from."""

    mapping: dict[str, str]
    confidence: dict[str, float]
    #: `stored` when a saved mapping was reused, `heuristic` otherwise.
    source: str = "heuristic"
    mapping_id: Any = None
    source_authority: str | None = None
    unmapped_required: list[str] = field(default_factory=list)

    def field_for(self, header: str) -> tuple[str | None, float]:
        for name, mapped in self.mapping.items():
            if mapped == header:
                return name, self.confidence.get(name, 0.0)
        return None, 0.0


def propose(headers: list[str]) -> Proposal:
    """The heuristic proposal for a set of headers."""
    pairs = sorted(
        (
            (score(header, name), position, header, name)
            for position, header in enumerate(headers)
            for name in CANONICAL_FIELDS
        ),
        # Highest score first; on a tie, the header further left, which is
        # where exclusion lists put the primary column of a pair (`Address`
        # before `Address 2`).
        key=lambda p: (-p[0], p[1]),
    )
    mapping: dict[str, str] = {}
    confidence: dict[str, float] = {}
    used: set[str] = set()
    for value, _, header, name in pairs:
        if value < PROPOSAL_FLOOR:
            break
        if name in mapping or header in used:
            continue
        mapping[name] = header
        confidence[name] = value
        used.add(header)
    ordered = {name: mapping[name] for name in CANONICAL_FIELDS if name in mapping}
    return Proposal(
        mapping=ordered,
        confidence=confidence,
        unmapped_required=missing_required(ordered),
    )


def reuse(stored: dict[str, str], headers: list[str]) -> dict[str, str] | None:
    """A stored mapping, if every header it names is in this file."""
    if not stored:
        return None
    present = set(headers)
    if all(header in present for header in stored.values()):
        return dict(stored)
    return None


def missing_required(mapping: dict[str, str]) -> list[str]:
    return [] if any(name in mapping for name in NAME_FIELDS) else list(NAME_FIELDS)


def validate(mapping: dict[str, str], headers: list[str]) -> dict[str, str]:
    """Every reason this mapping cannot be applied, keyed by the field at fault.

    Empty means it can. The keys are what the upload wizard highlights, so each
    message names one field and says what to do about it.
    """
    errors: dict[str, str] = {}
    present = set(headers)
    claimed: dict[str, str] = {}
    for name, header in mapping.items():
        if name not in BY_NAME:
            errors[f"mapping.{name}"] = "not a canonical field"
            continue
        if not isinstance(header, str) or not header:
            errors[f"mapping.{name}"] = "map it to a column, or leave it out"
            continue
        if header not in present:
            errors[f"mapping.{name}"] = f"column {header!r} is not in this file"
            continue
        if header in claimed:
            errors[f"mapping.{name}"] = (
                f"column {header!r} is already mapped to {claimed[header]}; "
                "one column feeds one field"
            )
            continue
        claimed[header] = name
    for name in missing_required(mapping):
        errors[f"mapping.{name}"] = "map last_name or organization_name - a record needs a name"
    return errors


__all__ = [
    "ABBREVIATIONS",
    "PROPOSAL_FLOOR",
    "Proposal",
    "fold",
    "missing_required",
    "propose",
    "reuse",
    "score",
    "validate",
]
