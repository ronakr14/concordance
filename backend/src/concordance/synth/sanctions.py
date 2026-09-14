"""Sanction records and their ground truth.

Every record is generated *for* a scenario, and the scenario is recorded. That
is what turns an evaluation from one aggregate F1 into a per-scenario table
where a regression says which kind of record broke - a missing NPI, an
organization acronym, a father/son pair - rather than merely that something did.

The eight scenarios the source spec names are all present, plus the four
organization scenarios that PLAN 11.2 added.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from concordance.domain import Outcome
from concordance.synth.corruption import OP_BY_NAME, CorruptionEngine
from concordance.synth.dates import shift_years
from concordance.synth.entities import TODAY, EntityFactory, _acronym
from concordance.synth.reference import load_reference
from concordance.synth.rng import choice, stream

# Scenario name -> (share of the file, expected outcome). Shares sum to 1.0.
SCENARIOS: dict[str, tuple[float, Outcome]] = {
    "exact_npi": (0.16, Outcome.MATCH),
    "missing_npi": (0.14, Outcome.MATCH),
    "sentinel_npi": (0.08, Outcome.MATCH),
    "name_variation": (0.14, Outcome.MATCH),
    "address_variation": (0.10, Outcome.MATCH),
    "ambiguous": (0.08, Outcome.AMBIGUOUS),
    "false_positive_bait": (0.08, Outcome.NO_MATCH),
    "unmatched": (0.08, Outcome.NO_MATCH),
    "org_exact": (0.05, Outcome.MATCH),
    "org_acronym": (0.04, Outcome.MATCH),
    "org_dba": (0.03, Outcome.MATCH),
    "org_type_disagreement": (0.02, Outcome.MATCH),
}

IDENTITY_FIELDS = (
    "npi", "first_name", "middle_name", "last_name", "suffix", "dob",
    "address_line1", "address_line2", "city", "state", "zip",
    "license_number", "license_state", "specialty",
    "organization_name", "dba_name", "ein", "is_organization",
)


@dataclass
class SanctionBuild:
    """What the sanction generator hands back to the pipeline."""

    records: list[dict[str, Any]]
    truth: list[dict[str, Any]]
    scenario_counts: dict[str, int]


def _allocate(total: int) -> list[tuple[str, int]]:
    """Split ``total`` across scenarios, giving the remainder to the largest."""
    counts = {name: int(total * share) for name, (share, _) in SCENARIOS.items()}
    shortfall = total - sum(counts.values())
    for name in sorted(counts, key=lambda n: -SCENARIOS[n][0])[: max(0, shortfall)]:
        counts[name] += 1
    return [(name, counts[name]) for name in SCENARIOS]


def _exclusion_dates(rng: np.random.Generator) -> tuple[date, date | None]:
    """An exclusion date in the last eight years; sometimes a reinstatement."""
    excl = TODAY - timedelta(days=int(rng.integers(30, 8 * 365)))
    if rng.random() < 0.18:
        # Deliberately mostly in the past: a reinstated provider is no longer
        # excluded, which the case workflow at Stage 7 has to handle.
        reinstated = excl + timedelta(days=int(rng.integers(180, 5 * 365)))
        return excl, reinstated
    return excl, None


class SanctionGenerator:
    def __init__(self, entities: list[dict[str, Any]], corruption: float, seed: int) -> None:
        self.entities = entities
        self.corruption = corruption
        self.seed = seed
        self.ref = load_reference()
        self.r_pick = stream(seed, "sanction", "pick")
        self.r_meta = stream(seed, "sanction", "metadata")
        self.r_force = stream(seed, "sanction", "forced_ops")
        self.engine = CorruptionEngine(level=corruption, seed=seed, side="sanction")
        self.phantoms = EntityFactory(seed ^ 0x5EED, self.ref)
        self.used_npis = {e["npi"] for e in entities if e.get("npi")}

        self.individuals = [e for e in entities if not e["is_organization"]]
        self.orgs = [e for e in entities if e["is_organization"]]
        self.by_cluster: dict[str, list[dict[str, Any]]] = {}
        for e in entities:
            if e.get("_cluster"):
                self.by_cluster.setdefault(e["_cluster"], []).append(e)
        self.ambiguous_clusters = [
            members
            for cid, members in self.by_cluster.items()
            if len(members) > 1 and ("twins" in cid or "common_name" in cid)
        ]
        self.acronym_clusters = [m for cid, m in self.by_cluster.items() if "acronym" in cid]
        self.dba_clusters = [m for cid, m in self.by_cluster.items() if "dba" in cid]

    # -- helpers ---------------------------------------------------------
    def _pick(self, pool: list[dict[str, Any]]) -> dict[str, Any]:
        return pool[int(self.r_pick.integers(0, len(pool)))]

    def _fresh_npi(self, is_org: bool) -> str:
        while True:
            npi = self.phantoms.npi.mint(is_org)
            if npi not in self.used_npis:
                self.used_npis.add(npi)
                return npi

    def _view(self, entity: dict[str, Any]) -> dict[str, Any]:
        return {f: entity.get(f) for f in IDENTITY_FIELDS}

    # -- scenario builders ------------------------------------------------
    def _scenario_record(self, scenario: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build one sanction view plus its ground-truth row body."""
        truth: dict[str, Any] = {"scenario_tag": scenario, "plausible_provider_ids": []}

        if scenario == "unmatched":
            # Nobody in the master. A valid-looking record with no counterpart.
            phantom = (
                self.phantoms.organization()
                if self.r_pick.random() < 0.15
                else self.phantoms.individual()
            )
            phantom["npi"] = self._fresh_npi(bool(phantom["is_organization"]))
            truth["expected_provider_id"] = None
            return self._view(phantom), truth

        if scenario == "false_positive_bait":
            # Close enough to be tempting: same name and state as a real
            # provider, different person - different DOB, different NPI.
            anchor = self._pick(self.individuals)
            bait = self.phantoms.individual(
                first_name=anchor["first_name"],
                last_name=anchor["last_name"],
                state=anchor["state"],
                city=anchor["city"],
                zip=anchor["zip"],
            )
            bait["npi"] = self._fresh_npi(False)
            if anchor.get("dob"):
                # Far enough apart that DOB settles it, close enough that both
                # are plausibly practising: shift away from the era edges.
                years = int(self.r_pick.integers(9, 25))
                if anchor["dob"].year - years < 1945:
                    bait["dob"] = shift_years(anchor["dob"], years)
                else:
                    bait["dob"] = shift_years(anchor["dob"], -years)
            truth["expected_provider_id"] = None
            truth["near_provider_id"] = anchor["provider_id"]
            return self._view(bait), truth

        if scenario == "ambiguous":
            members = self.ambiguous_clusters[
                int(self.r_pick.integers(0, len(self.ambiguous_clusters)))
            ]
            anchor = members[0]
            view = self._view(anchor)
            # Strip what would disambiguate: no NPI, no DOB, no street address.
            view["npi"] = None
            view["dob"] = None
            view["address_line1"] = None
            view["address_line2"] = None
            truth["expected_provider_id"] = anchor["provider_id"]
            truth["plausible_provider_ids"] = [m["provider_id"] for m in members]
            truth["cluster"] = anchor["_cluster"]
            return view, truth

        if scenario == "org_acronym" and self.acronym_clusters:
            members = self.acronym_clusters[int(self.r_pick.integers(0, len(self.acronym_clusters)))]
            anchor = members[0]
            view = self._view(anchor)
            view["organization_name"] = _acronym(anchor["organization_name"] or "") or view[
                "organization_name"
            ]
            truth["expected_provider_id"] = anchor["provider_id"]
            truth["cluster"] = anchor["_cluster"]
            return view, truth

        if scenario == "org_dba":
            pool = [e for e in self.orgs if e.get("dba_name")] or self.orgs
            anchor = self._pick(pool)
            view = self._view(anchor)
            # The file carries the trading name; the master carries the legal one.
            if anchor.get("dba_name"):
                view["organization_name"] = anchor["dba_name"]
                view["dba_name"] = None
            truth["expected_provider_id"] = anchor["provider_id"]
            return view, truth

        if scenario == "org_type_disagreement":
            # The file files an organization as though it were a person, using
            # the org name in the surname column - common with small practices.
            anchor = self._pick(self.orgs)
            view = self._view(anchor)
            view["is_organization"] = False
            view["last_name"] = anchor["organization_name"]
            view["organization_name"] = None
            truth["expected_provider_id"] = anchor["provider_id"]
            return view, truth

        if scenario == "org_exact":
            anchor = self._pick(self.orgs)
            truth["expected_provider_id"] = anchor["provider_id"]
            return self._view(anchor), truth

        # Remaining individual scenarios all point at a real provider.
        anchor = self._pick(self.individuals)
        truth["expected_provider_id"] = anchor["provider_id"]
        if anchor.get("_cluster"):
            truth["cluster"] = anchor["_cluster"]
        return self._view(anchor), truth

    def _force_scenario(self, scenario: str, view: dict[str, Any]) -> list[dict[str, Any]]:
        """Guarantee the defining feature survives generic corruption."""
        forced: list[dict[str, Any]] = []

        def record(op: str, change: dict[str, Any]) -> None:
            before = {k: view.get(k) for k in change}
            view.update(change)
            forced.append({"family": "scenario", "op": op, "side": "sanction",
                           "before": _jsonable_map(before), "after": _jsonable_map(change)})

        if scenario == "exact_npi":
            pass  # NPI is restored by the caller after corruption.
        elif scenario == "missing_npi":
            record("force_missing_npi", {"npi": None})
        elif scenario == "sentinel_npi":
            sentinel = choice(
                self.r_force,
                ["0000000000", "9999999999", "1111111111", "UNKNOWN", "N/A", "NONE"],
            )
            record("force_sentinel_npi", {"npi": sentinel})
        elif scenario == "name_variation":
            record("force_missing_npi", {"npi": None})
            for name in ("nickname", "keyboard_typo", "first_initial"):
                change = OP_BY_NAME[name].fn(view, self.r_force, self.ref)
                if change:
                    record(f"force_{name}", change)
                    break
        elif scenario == "address_variation":
            record("force_missing_npi", {"npi": None})
            for name in ("usps_abbreviation", "wrong_zip"):
                change = OP_BY_NAME[name].fn(view, self.r_force, self.ref)
                if change:
                    record(f"force_{name}", change)
                    break
        return forced

    # -- driver -----------------------------------------------------------
    def build(self, count: int) -> SanctionBuild:
        records: list[dict[str, Any]] = []
        truths: list[dict[str, Any]] = []
        scenario_counts: dict[str, int] = {}
        idx = 0

        for scenario, n in _allocate(count):
            scenario_counts[scenario] = n
            for _ in range(n):
                idx += 1
                record_id = f"S{idx:06d}"
                view, truth_body = self._scenario_record(scenario)
                clean_npi = view.get("npi")

                corrupted, profile = self.engine.apply(view)
                forced = self._force_scenario(scenario, corrupted)
                if scenario == "exact_npi":
                    corrupted["npi"] = clean_npi

                authority, _dialect = self.ref.pick_authority(self.r_meta)
                excl, reinstated = _exclusion_dates(self.r_meta)
                record = {
                    "record_id": record_id,
                    "source_authority": authority,
                    **corrupted,
                    "sanction_type": self.ref.sanction_types.pick(self.r_meta),
                    "exclusion_date": excl,
                    "reinstatement_date": reinstated,
                }
                records.append(record)
                truths.append(
                    {
                        "sanction_record_id": record_id,
                        "expected_outcome": str(SCENARIOS[scenario][1]),
                        "expected_provider_id": truth_body.get("expected_provider_id"),
                        "scenario_tag": scenario,
                        "corruption_profile": {
                            "applied": profile + forced,
                            "plausible_provider_ids": truth_body.get("plausible_provider_ids", []),
                            "near_provider_id": truth_body.get("near_provider_id"),
                            "cluster": truth_body.get("cluster"),
                            "corruption_level": self.corruption,
                        },
                    }
                )

        return SanctionBuild(records=records, truth=truths, scenario_counts=scenario_counts)


def _jsonable_map(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}
