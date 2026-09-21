"""Build the demo state, in one command.

    python tasks.py demo              # the whole thing, from an empty database
    python tasks.py demo KEEP=1       # reuse the dataset already loaded
    python tasks.py demo YES=1        # no confirmation before emptying

Ten minutes of demo needs about eight things to already be true, and setting
them up by hand in front of an audience is how a demo goes wrong. This script
makes them true and then prints where to click.

What it leaves behind:

- a seeded 50,000 x 5,000 dataset at a fixed seed, so every run of this script
  produces the same records with the same ids;
- two users, an admin and an analyst, with known passwords;
- a fitted, activated scoring config, and a second config whose accept
  threshold is lower, so two runs of the same records disagree on purpose;
- two completed runs to diff, and one of them to replay;
- a completed corruption sweep for the Lab page;
- a review queue with real ambiguous records in it;
- a workbook of records under unfamiliar headers, for the column-mapping
  screen to have work to do.

Every id it produces is printed at the end, and written to `.run/demo.json`,
so the walkthrough in `docs/demo_script.md` can be followed without hunting
through the UI for a run id.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
STATE = ROOT / ".run" / "demo.json"
WORKBOOK = ROOT / ".run" / "demo_upload.xlsx"

#: Fixed, so the demo is the same demo every time it is rebuilt.
SEED = 20260914
CORRUPTION = 0.5
PROVIDERS = 50_000
SANCTIONS = 5_000

DEMO_USERS = [
    ("[REDACTED_EMAIL_ADDRESS_5]", "demo-admin-password", "admin"),
    ("[REDACTED_EMAIL_ADDRESS_6]", "demo-analyst-password", "analyst"),
]


def _step(number: int, of: int, what: str) -> None:
    print(f"\n[{number}/{of}] {what}", flush=True)


def _cli(*args: str, check: bool = True) -> int:
    print(f"    + concordance {' '.join(args)}", flush=True)
    rc = subprocess.call([PY, "-m", "concordance.cli", *args], cwd=str(ROOT))
    if rc and check:
        raise SystemExit(f"`concordance {' '.join(args)}` failed with {rc}")
    return rc


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------


def ensure_users() -> dict[str, str]:
    """Create the demo users if they are not already there, and return their ids.

    Through the service rather than the API, so the demo can be built without
    a server running - `make demo` is meant to be the step *before* `make up`
    is interesting.
    """
    from concordance.auth import service as auth
    from concordance.config import get_settings
    from concordance.db.session import session_scope

    settings = get_settings()
    ids: dict[str, str] = {}
    with session_scope(settings) as session:
        for email, password, role in DEMO_USERS:
            try:
                user = auth.register(session, settings, email=email, password=password, role=role)
                ids[role] = str(user.id)
                print(f"    created {role} {email}")
            except auth.EmailTakenError:
                session.rollback()
                from sqlalchemy import text

                found = session.execute(
                    text("SELECT id FROM users WHERE email = :e"), {"e": email}
                ).scalar_one()
                ids[role] = str(found)
                print(f"    {role} {email} already exists")
    return ids


def second_config(delta: float = 0.06) -> dict[str, Any]:
    """A copy of the active config with each model's accept threshold lowered.

    The point of the diff scenario is a config change whose effect is visible
    but not catastrophic: move the accept threshold down and some records that
    were left to a human become automatic accepts. A change nobody can see
    proves nothing, and a change that flips everything proves the wrong thing.

    Built through `ScoringConfig` and `import_scoring_config`, the same path a
    fitted config takes, so the row is shaped exactly as a run reads it. The
    thresholds a run applies are the per-model ones inside `params`; the
    top-level columns are a summary.
    """
    from concordance.config import get_settings
    from concordance.db.repositories.configs import ConfigRepository
    from concordance.db.session import session_scope
    from concordance.jobs.reconcile import import_scoring_config
    from concordance.matching.scoring_config import ScoringConfig

    with session_scope(get_settings()) as session:
        active = ConfigRepository(session).active()
        if active is None:
            raise SystemExit("no active scoring config - the first run activates one")

        payload = json.loads(json.dumps(active.params))
        version = f"{active.version}-demo-looser"
        payload["config_id"] = version
        moved: dict[str, tuple[float, float]] = {}
        for kind, thresholds in payload["thresholds"].items():
            before = float(thresholds["t_auto_accept"])
            # Never at or below the reject threshold: that would empty the grey
            # band and turn a threshold move into a different policy.
            after = round(max(float(thresholds["t_auto_reject"]) + 0.01, before - delta), 6)
            thresholds["t_auto_accept"] = after
            moved[kind] = (before, after)

        row = import_scoring_config(
            session,
            ScoringConfig.from_dict(payload),
            notes=(
                "Demo scenario 5. The active config with each accept threshold lowered by up "
                f"to {delta}, so a second run of the same records decides some of them "
                "differently and the diff has something real to show."
            ),
        )
        row.parent_id = active.id
        return {"id": str(row.id), "version": version, "moved": moved}


def newest_runs(limit: int = 2) -> list[dict[str, str]]:
    from sqlalchemy import text

    from concordance.config import get_settings
    from concordance.db.session import session_scope

    with session_scope(get_settings()) as session:
        rows = session.execute(
            text(
                "SELECT r.id, r.status, r.matched_count, r.ambiguous_count, sc.version "
                "FROM reconciliation_runs r "
                "LEFT JOIN scoring_configs sc ON sc.id = r.scoring_config_id "
                "WHERE r.status = 'COMPLETED' ORDER BY r.created_at DESC LIMIT :n"
            ),
            {"n": limit},
        ).mappings().all()
    return [{k: str(v) for k, v in row.items()} for row in rows]


def queue_depth() -> int:
    from sqlalchemy import text

    from concordance.config import get_settings
    from concordance.db.session import session_scope

    with session_scope(get_settings()) as session:
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM match_results "
                    "WHERE superseded_by IS NULL AND decision = 'AMBIGUOUS'"
                )
            ).scalar_one()
        )


def sample_records() -> dict[str, Any]:
    """One record per demo scenario, chosen by what the engine actually decided.

    Picked from the data rather than hardcoded, because the ids change with
    every reseed and a demo script naming a record that no longer exists is
    worse than one that names none.
    """
    from sqlalchemy import text

    from concordance.config import get_settings
    from concordance.db.session import session_scope

    queries = {
        "exact_npi": """
            SELECT mr.id AS match_id, sr.record_id, sr.last_name, sr.npi
            FROM match_results mr JOIN sanction_records sr ON sr.id = mr.sanction_record_id
            JOIN ground_truth g ON g.sanction_record_id = sr.id
            WHERE mr.superseded_by IS NULL AND mr.decision = 'MATCH'
              AND mr.route = 'deterministic'
              AND g.scenario_tag = 'exact_npi' AND sr.npi IS NOT NULL
            ORDER BY sr.ordinal LIMIT 1
        """,
        "missing_npi": """
            SELECT mr.id AS match_id, sr.record_id, sr.last_name, mr.calibrated_confidence
            FROM match_results mr JOIN sanction_records sr ON sr.id = mr.sanction_record_id
            JOIN ground_truth g ON g.sanction_record_id = sr.id
            WHERE mr.superseded_by IS NULL AND mr.decision = 'MATCH'
              AND mr.route = 'probabilistic'
              AND g.scenario_tag = 'missing_npi'
            ORDER BY mr.raw_match_weight DESC LIMIT 1
        """,
        "ambiguous": """
            SELECT mr.id AS match_id, sr.record_id, sr.last_name, mr.calibrated_confidence
            FROM match_results mr JOIN sanction_records sr ON sr.id = mr.sanction_record_id
            JOIN ground_truth g ON g.sanction_record_id = sr.id
            WHERE mr.superseded_by IS NULL AND mr.decision = 'AMBIGUOUS'
              AND g.scenario_tag = 'ambiguous'
            ORDER BY sr.ordinal LIMIT 1
        """,
        "organization": """
            SELECT mr.id AS match_id, sr.record_id, sr.organization_name, mr.decision
            FROM match_results mr JOIN sanction_records sr ON sr.id = mr.sanction_record_id
            JOIN ground_truth g ON g.sanction_record_id = sr.id
            WHERE mr.superseded_by IS NULL AND mr.decision = 'MATCH'
              AND g.scenario_tag = 'org_acronym'
            ORDER BY sr.ordinal LIMIT 1
        """,
    }
    out: dict[str, Any] = {}
    with session_scope(get_settings()) as session:
        for name, sql in queries.items():
            row = session.execute(text(sql)).mappings().first()
            out[name] = {k: str(v) for k, v in row.items()} if row else None
    return out


def build_workbook() -> dict[str, Any]:
    """Scenario 7's upload: real records under headers nobody has mapped yet."""
    WORKBOOK.parent.mkdir(exist_ok=True)
    proc = subprocess.run(
        [PY, str(ROOT / "scripts" / "e2e_fixture.py"), "build", "demo", str(WORKBOOK)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"    could not build the upload workbook: {proc.stderr.strip()[:300]}")
        return {"path": None}
    try:
        built = json.loads(proc.stdout)
    except json.JSONDecodeError:
        built = {}
    return {"path": str(WORKBOOK), **built}


# --------------------------------------------------------------------------
# the whole thing
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    keep = "KEEP=1" in argv or "--keep" in argv
    yes = "YES=1" in argv or "--yes" in argv
    started = time.perf_counter()
    total = 9

    if not keep:
        _step(1, total, "empty the database")
        _cli("db", "reset", *(["--yes"] if yes else []))

        _step(2, total, f"generate {PROVIDERS:,} providers and {SANCTIONS:,} records, seed {SEED}")
        _cli(
            "data", "seed",
            "--corruption", str(CORRUPTION),
            "--providers", str(PROVIDERS),
            "--sanctions", str(SANCTIONS),
            "--seed", str(SEED),
        )

        _step(3, total, "load it into Postgres")
        _cli("db", "load")
    else:
        print("\nKEEP=1: reusing the dataset already loaded")

    _step(4, total, "fit the model and calibrate it")
    _cli("match", "fit", "--seed", str(SEED))

    _step(5, total, "create the demo users")
    users = ensure_users()

    _step(6, total, "run the reconciliation")
    _cli("run", "reconcile", "--strategy", "probabilistic", "--seed", str(SEED))

    _step(7, total, "a second config, and a second run to diff against the first")
    config = second_config()
    for kind, (before, after) in config["moved"].items():
        print(f"    {config['version']}  {kind} accept {before} -> {after}")
    _cli("configs", "activate", config["version"])
    _cli("run", "reconcile", "--strategy", "probabilistic", "--seed", str(SEED))

    _step(8, total, "a corruption sweep, so the Lab page has a curve to show")
    # Every strategy at every level, in process. The Lab page draws the
    # robustness curve from it and the corruption dial reads it; without one
    # scenario 6 is an empty page and a button that takes minutes.
    _cli("lab", "sweep", "--seed", str(SEED))

    _step(9, total, "the upload workbook for the column-mapping screen")
    workbook = build_workbook()

    runs = newest_runs(2)
    state = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seed": SEED,
        "corruption": CORRUPTION,
        "users": {role: email for email, _, role in DEMO_USERS} | {f"{k}_id": v for k, v in users.items()},
        "passwords": {role: password for _, password, role in DEMO_USERS},
        "config": config,
        "runs": runs,
        "queue_depth": queue_depth(),
        "records": sample_records(),
        "workbook": workbook,
    }
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    print(f"\ndemo ready in {time.perf_counter() - started:.0f}s\n")
    print(f"  admin     {DEMO_USERS[0][0]} / {DEMO_USERS[0][1]}")
    print(f"  analyst   {DEMO_USERS[1][0]} / {DEMO_USERS[1][1]}")
    for i, run in enumerate(runs):
        print(f"  run {'B' if i == 0 else 'A'}     {run['id']}  config {run.get('version')}")
    print(f"  queue     {state['queue_depth']} ambiguous record(s) awaiting review")
    if workbook.get("path"):
        print(f"  upload    {workbook['path']}")
    print(f"\n  ids written to {STATE}")
    print("  walkthrough in docs/demo_script.md")
    print("\n  start the system with `make up`, then log in at http://localhost:5173")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
