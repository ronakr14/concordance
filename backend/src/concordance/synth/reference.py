"""Loading the committed reference tables.

Every distribution the generator samples from lives in
`concordance/data/reference/*.csv`, built by `scripts/build_reference_data.py`.
Keeping them as data rather than literals means the tables can be swapped for
real frequency data later without touching generator code.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Any

import numpy as np

from concordance.synth.rng import cumulative, pick

PACKAGE = "concordance.data.reference"


def _rows(name: str) -> list[dict[str, str]]:
    text = (files(PACKAGE) / name).read_text(encoding="utf-8")
    return list(csv.DictReader(text.splitlines()))


def _normalized(weights: list[float]) -> list[float]:
    total = sum(weights)
    return [w / total for w in weights]


@dataclass
class WeightedTable:
    """Values with a probability vector and its cumulative form."""

    values: list[str]
    probs: list[float]

    def __post_init__(self) -> None:
        self.cum = cumulative(self.probs)

    def pick(self, rng: np.random.Generator) -> str:
        """One weighted draw."""
        return pick(rng, self.values, self.cum)

    def __len__(self) -> int:
        return len(self.values)


@dataclass
class Reference:
    """All reference tables, loaded once per process."""

    surnames: WeightedTable
    given_by_era: dict[tuple[str, str], WeightedTable]  # (sex, era) -> names
    eras: list[str]
    nickname_of: dict[str, list[str]]  # canonical -> nicknames
    canonical_of: dict[str, str]  # nickname -> canonical
    credentials: dict[str, list[str]]  # kind -> values
    states: WeightedTable
    cities_by_state: dict[str, list[tuple[str, str]]]  # state -> (city, zip3)
    street_names: list[str]
    street_types: list[str]
    specialties: list[str]
    org_components: dict[str, list[str]]
    sanction_types: WeightedTable
    source_authorities: list[tuple[str, str]] = field(default_factory=list)
    source_authority_probs: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.authority_cum = cumulative(self.source_authority_probs or [1.0])

    def pick_authority(self, rng: np.random.Generator) -> tuple[str, str]:
        """One weighted draw of (source authority, header dialect)."""
        idx = int(np.searchsorted(self.authority_cum, rng.random(), side="right"))
        return self.source_authorities[min(idx, len(self.source_authorities) - 1)]

    def all_credentials(self) -> list[str]:
        return [c for kind, values in self.credentials.items() if kind != "generational" for c in values]

    def generational_suffixes(self) -> list[str]:
        return self.credentials.get("generational", [])


@lru_cache(maxsize=1)
def load_reference() -> Reference:
    """Read every reference CSV. Cached; the tables are immutable."""
    surname_rows = _rows("surnames.csv")
    surnames = WeightedTable(
        [r["surname"] for r in surname_rows],
        _normalized([float(r["weight"]) for r in surname_rows]),
    )

    given: dict[tuple[str, str], tuple[list[str], list[float]]] = {}
    for r in _rows("given_names.csv"):
        key = (r["sex"], r["birth_era"])
        names, weights = given.setdefault(key, ([], []))
        names.append(r["given_name"])
        weights.append(float(r["weight"]))
    given_by_era = {k: WeightedTable(v[0], _normalized(v[1])) for k, v in given.items()}
    eras = sorted({era for _, era in given_by_era})

    nickname_of: dict[str, list[str]] = {}
    canonical_of: dict[str, str] = {}
    for r in _rows("nicknames.csv"):
        nickname_of.setdefault(r["canonical"], []).append(r["nickname"])
        canonical_of.setdefault(r["nickname"], r["canonical"])

    credentials: dict[str, list[str]] = {}
    for r in _rows("credentials.csv"):
        credentials.setdefault(r["kind"], []).append(r["credential"])

    state_rows = _rows("states.csv")
    states = WeightedTable(
        [r["state"] for r in state_rows],
        _normalized([float(r["weight"]) for r in state_rows]),
    )

    cities_by_state: dict[str, list[tuple[str, str]]] = {}
    for r in _rows("cities.csv"):
        cities_by_state.setdefault(r["state"], []).append((r["city"], r["zip3"]))

    org_components: dict[str, list[str]] = {}
    for r in _rows("org_components.csv"):
        org_components.setdefault(r["kind"], []).append(r["value"])

    st_rows = _rows("sanction_types.csv")
    sanction_types = WeightedTable(
        [r["sanction_type"] for r in st_rows],
        _normalized([float(r["weight"]) for r in st_rows]),
    )

    sa_rows = _rows("source_authorities.csv")

    return Reference(
        surnames=surnames,
        given_by_era=given_by_era,
        eras=eras,
        nickname_of=nickname_of,
        canonical_of=canonical_of,
        credentials=credentials,
        states=states,
        cities_by_state=cities_by_state,
        street_names=[r["street_name"] for r in _rows("street_names.csv")],
        street_types=[r["street_type"] for r in _rows("street_types.csv")],
        specialties=[r["specialty"] for r in _rows("specialties.csv")],
        org_components=org_components,
        sanction_types=sanction_types,
        source_authorities=[(r["source_authority"], r["dialect"]) for r in sa_rows],
        source_authority_probs=_normalized([float(r["weight"]) for r in sa_rows]),
    )


def reference_fingerprint() -> dict[str, Any]:
    """Table sizes, recorded in the dataset manifest for provenance."""
    ref = load_reference()
    return {
        "surnames": len(ref.surnames),
        "given_names": sum(len(t) for t in ref.given_by_era.values()),
        "nicknames": sum(len(v) for v in ref.nickname_of.values()),
        "states": len(ref.states),
        "cities": sum(len(v) for v in ref.cities_by_state.values()),
        "specialties": len(ref.specialties),
    }
