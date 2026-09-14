"""Generating the clean truth: providers before anything is corrupted.

The generator produces *entities*. What lands in the provider master and what
lands in a sanction file are two separately corrupted views of the same entity,
which is exactly the situation the matching engine faces in production.

The planted near-duplicate clusters matter more than the bulk of the file. A
random 50k providers is trivially matchable; twins, a father and son at one
address, and three Maria Garcias in one state are what separate a calibrated
engine from one that merely looks confident.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from concordance.matching.npi_validator import make_npi
from concordance.synth.dates import shift_years
from concordance.synth.reference import Reference, load_reference
from concordance.synth.rng import choice, cumulative, stream

# Licence-number shapes, picked per state so a state's licences look alike.
LICENCE_SHAPES = ("A######", "MD#####", "##-#####", "#######", "P######", "RN######")

EIN_PREFIXES = [
    "01", "02", "03", "04", "05", "06", "10", "11", "12", "13", "14", "15", "16",
    "20", "21", "22", "23", "25", "26", "27", "30", "31", "32", "33", "34", "35",
    "36", "37", "38", "39", "41", "42", "43", "44", "45", "46", "47", "48", "51",
    "52", "53", "54", "55", "56", "57", "58", "59", "61", "62", "63", "64", "65",
    "66", "68", "71", "72", "73", "74", "75", "76", "77", "81", "82", "83", "84",
    "85", "86", "87", "88", "91", "92", "93", "94", "95", "98", "99",
]

ORG_SHARE = 0.15
TODAY = date(2026, 9, 14)


def _digits(rng: np.random.Generator, n: int) -> str:
    """``n`` random digits from a single draw - one call per field, not per digit."""
    return f"{int(rng.integers(0, 10**n)):0{n}d}"


def _licence(rng: np.random.Generator, shape: str) -> str:
    return "".join(_digits(rng, 1) if ch == "#" else ch for ch in shape)


def _dob_for_era(rng: np.random.Generator, era: str) -> date:
    """Birth date inside the era's decade, uniform over the year."""
    year = int(era) + int(rng.integers(-4, 6))
    return date(year, 1, 1) + timedelta(days=int(rng.integers(0, 365)))


class _NpiMinter:
    """Checksum-valid, globally unique NPIs. Type 1 individual, type 2 organization."""

    def __init__(self, rng: np.random.Generator) -> None:
        self.rng = rng
        self.seen: set[str] = set()

    def mint(self, is_org: bool) -> str:
        lead = "2" if is_org else "1"
        while True:
            npi = make_npi(lead + _digits(self.rng, 8))
            if npi not in self.seen:
                self.seen.add(npi)
                return npi


class EntityFactory:
    """Builds individual and organization entities from the reference tables."""

    def __init__(self, seed: int, ref: Reference | None = None) -> None:
        self.seed = seed
        self.ref = ref or load_reference()
        self.r_name = stream(seed, "entity", "name")
        self.r_geo = stream(seed, "entity", "geo")
        self.r_prof = stream(seed, "entity", "professional")
        self.r_org = stream(seed, "entity", "organization")
        self.r_cluster = stream(seed, "entity", "clusters")
        self.npi = _NpiMinter(stream(seed, "entity", "npi"))
        self._licence_shape = {
            s: LICENCE_SHAPES[i % len(LICENCE_SHAPES)] for i, s in enumerate(self.ref.states.values)
        }
        self._counter = 0
        # Practising clinicians skew to the middle decades, not uniformly.
        self._era_cum = cumulative([0.08, 0.15, 0.22, 0.24, 0.20, 0.11])
        self._creds = self.ref.all_credentials()

    # -- helpers ---------------------------------------------------------
    def next_id(self) -> str:
        self._counter += 1
        return f"P{self._counter:07d}"

    def address(self, state: str | None = None) -> dict[str, Any]:
        ref = self.ref
        st = state or ref.states.pick(self.r_geo)
        city, zip3 = ref.cities_by_state[st][int(self.r_geo.integers(0, len(ref.cities_by_state[st])))]
        return {
            "address_line1": (
                f"{int(self.r_geo.integers(1, 9999))} "
                f"{choice(self.r_geo, ref.street_names)} {choice(self.r_geo, ref.street_types)}"
            ),
            "address_line2": (
                f"Suite {int(self.r_geo.integers(100, 999))}" if self.r_geo.random() < 0.25 else None
            ),
            "city": city,
            "state": st,
            "zip": zip3 + _digits(self.r_geo, 2),
        }

    def _professional(self, state: str, is_org: bool) -> dict[str, Any]:
        ref = self.ref
        # Licences are usually issued by the state the provider practises in.
        lic_state = state if self.r_prof.random() < 0.9 else ref.states.pick(self.r_prof)
        return {
            "license_number": _licence(self.r_prof, self._licence_shape[lic_state]),
            "license_state": lic_state,
            "specialty": choice(self.r_prof, ref.specialties),
            "status": "ACTIVE" if self.r_prof.random() < 0.93 else "INACTIVE",
            "is_organization": is_org,
        }

    # -- individuals -----------------------------------------------------
    def individual(self, **overrides: Any) -> dict[str, Any]:
        ref = self.ref
        sex = "M" if self.r_name.random() < 0.52 else "F"
        era = ref.eras[int(np.searchsorted(self._era_cum, self.r_name.random(), side="right"))]
        given = ref.given_by_era[(sex, era)]
        first = given.pick(self.r_name)
        middle = given.pick(self.r_name) if self.r_name.random() < 0.7 else None
        last = ref.surnames.pick(self.r_name)
        suffix = choice(self.r_name, self._creds) if self.r_name.random() < 0.75 else None

        addr = self.address()
        entity: dict[str, Any] = {
            "provider_id": self.next_id(),
            "npi": self.npi.mint(False),
            "first_name": first,
            "middle_name": middle,
            "last_name": last,
            "suffix": suffix,
            "dob": _dob_for_era(self.r_name, era),
            "organization_name": None,
            "dba_name": None,
            "ein": None,
            **addr,
            **self._professional(addr["state"], is_org=False),
            "_sex": sex,
            "_era": era,
            "_cluster": None,
            "_cluster_role": None,
        }
        entity.update(overrides)
        return entity

    # -- organizations ---------------------------------------------------
    def organization_name(self) -> tuple[str, str | None]:
        """A legal name and, sometimes, a DBA that differs from it."""
        ref = self.ref
        head = choice(self.r_org, ref.org_components["head"])
        core = choice(self.r_org, ref.org_components["core"])
        tail = choice(self.r_org, ref.org_components["tail"])
        suffix = choice(self.r_org, ref.org_components["suffix"])
        legal = " ".join(p for p in (head, core, tail, suffix) if p)
        dba = None
        if self.r_org.random() < 0.35:
            dba = f"{head} {tail}" if self.r_org.random() < 0.5 else f"{head} {core}"
        return legal, dba

    def organization(self, **overrides: Any) -> dict[str, Any]:
        legal, dba = self.organization_name()
        addr = self.address()
        entity: dict[str, Any] = {
            "provider_id": self.next_id(),
            "npi": self.npi.mint(True),
            "first_name": None,
            "middle_name": None,
            "last_name": None,
            "suffix": None,
            "dob": None,
            "organization_name": legal,
            "dba_name": dba,
            "ein": f"{choice(self.r_org, EIN_PREFIXES)}-{_digits(self.r_org, 7)}",
            **addr,
            **self._professional(addr["state"], is_org=True),
            "_sex": None,
            "_era": None,
            "_cluster": None,
            "_cluster_role": None,
        }
        entity.update(overrides)
        return entity


def _acronym(name: str) -> str:
    """Initials of the substantive words - how an org abbreviates itself."""
    skip = {"of", "and", "the", "for", "at", "LLC", "Inc", "PC", "PA", "LLP", "Corp", "PLLC"}
    initials = [w[0] for w in name.split() if w not in skip and w[0].isalpha()]
    return "".join(initials).upper()


def plant_individual_clusters(f: EntityFactory, entities: list[dict[str, Any]], n_groups: int) -> None:
    """Twins, father/son pairs and common-name collisions.

    These are the hard cases. Each group is tagged so the scenario catalogue and
    the evaluation breakdown can find them again.
    """
    rng = f.r_cluster
    for g in range(n_groups):
        kind = ("twins", "father_son", "common_name")[g % 3]
        cid = f"C{g:05d}-{kind}"
        base = f.individual()
        base["_cluster"], base["_cluster_role"] = cid, "anchor"
        entities.append(base)

        if kind == "twins":
            # Same surname, same DOB, same address; different given name.
            twin = f.individual(
                last_name=base["last_name"],
                dob=base["dob"],
                address_line1=base["address_line1"],
                address_line2=base["address_line2"],
                city=base["city"],
                state=base["state"],
                zip=base["zip"],
            )
            twin["_cluster"], twin["_cluster_role"] = cid, "twin"
            entities.append(twin)

        elif kind == "father_son":
            # Identical name, one address, roughly thirty years apart. The
            # anchor is pushed back into an early era first, or the son ends up
            # born after the dataset's own present day.
            base["suffix"] = "Sr"
            base["dob"] = date(
                1948 + int(rng.integers(0, 12)), base["dob"].month, min(base["dob"].day, 28)
            )
            son = f.individual(
                first_name=base["first_name"],
                middle_name=base["middle_name"],
                last_name=base["last_name"],
                suffix="Jr",
                address_line1=base["address_line1"],
                address_line2=base["address_line2"],
                city=base["city"],
                state=base["state"],
                zip=base["zip"],
                dob=shift_years(base["dob"], 28),
            )
            son["_cluster"], son["_cluster_role"] = cid, "son"
            entities.append(son)

        else:
            # Two or three people sharing a name inside one state.
            for i in range(int(rng.integers(1, 3))):
                twin = f.individual(
                    first_name=base["first_name"],
                    last_name=base["last_name"],
                    state=base["state"],
                )
                # Re-home into the anchor's state so the collision is real.
                addr = f.address(state=base["state"])
                twin.update(addr)
                twin["_cluster"], twin["_cluster_role"] = cid, f"namesake{i + 1}"
                entities.append(twin)


def plant_organization_clusters(f: EntityFactory, entities: list[dict[str, Any]], n_groups: int) -> None:
    """Near-duplicate organizations: branch, acronym, DBA-vs-legal."""
    for g in range(n_groups):
        kind = ("branch", "acronym", "dba")[g % 3]
        cid = f"O{g:05d}-{kind}"
        base = f.organization()
        base["_cluster"], base["_cluster_role"] = cid, "anchor"
        entities.append(base)

        if kind == "branch":
            # Same legal name, another city in the same state.
            other = f.organization(organization_name=base["organization_name"])
            other.update(f.address(state=base["state"]))
            other["_cluster"], other["_cluster_role"] = cid, "branch"
            entities.append(other)
        elif kind == "acronym":
            other = f.organization(
                organization_name=_acronym(base["organization_name"]) or base["organization_name"],
                dba_name=base["organization_name"],
            )
            other["_cluster"], other["_cluster_role"] = cid, "acronym"
            entities.append(other)
        else:
            other = f.organization(
                organization_name=base["dba_name"] or f"{base['organization_name']} Holdings",
                dba_name=base["organization_name"],
            )
            other["_cluster"], other["_cluster_role"] = cid, "dba"
            entities.append(other)


def generate_entities(count: int, seed: int, org_share: float = ORG_SHARE) -> list[dict[str, Any]]:
    """``count`` entities: individuals, organizations, and planted clusters.

    Cluster members are part of ``count``, not additional to it, so the caller
    always gets the size it asked for.
    """
    f = EntityFactory(seed)
    entities: list[dict[str, Any]] = []

    # Clusters first: they are the reason the dataset is interesting, and
    # sizing them off the total keeps their density constant across runs.
    individual_groups = max(1, int(count * 0.006))
    org_groups = max(1, int(count * org_share * 0.02))
    plant_individual_clusters(f, entities, individual_groups)
    plant_organization_clusters(f, entities, org_groups)

    n_orgs = int(count * org_share)
    # Counted, not recomputed: scanning the list for organizations on every
    # iteration made generation quadratic and dominated a 50k run.
    org_count = sum(1 for e in entities if e["is_organization"])
    while len(entities) < count:
        remaining_org_slots = n_orgs - org_count
        remaining = count - len(entities)
        make_org = remaining_org_slots > 0 and f.r_org.random() < (remaining_org_slots / remaining)
        entities.append(f.organization() if make_org else f.individual())
        org_count += int(make_org)

    return entities[:count]
