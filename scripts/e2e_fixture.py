"""Data for the browser end-to-end suite (`frontend/e2e`).

    python scripts/e2e_fixture.py build <tag> <out.xlsx>   -> JSON on stdout
    python scripts/e2e_fixture.py cleanup <tag>
    python scripts/e2e_fixture.py sweep          # every run's leftovers

`build` writes a workbook of records the engine has already decided - three
deterministic matches, two ambiguous records, two organizations - copied under
fresh record keys and a source authority named for the run, so every run
uploads new bytes and nothing it does touches the loaded dataset. The headers
are deliberately not the canonical names, so the column-mapping screen has
work to do. `cleanup` removes everything the run created, the users included;
it connects as the owner, the only role allowed to delete audit rows.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from openpyxl import Workbook
from sqlalchemy import text

from concordance.config import Settings
from concordance.db.session import session_scope

#: canonical field -> the header this made-up authority publishes it under
HEADERS = {
    "record_id": "Case Ref",
    "last_name": "Surname",
    "first_name": "Given Name",
    "middle_name": "Middle",
    "organization_name": "Entity Name",
    "dob_raw": "Birth Date",
    "npi": "NPI Number",
    "address_line1": "Street",
    "city": "Town",
    "state": "St",
    "zip": "Postal",
    "specialty": "Practice Area",
    "license_number": "Licence No",
    "sanction_type": "Action",
    "exclusion_date": "Action Date",
    "ein": "Tax ID",
}

DECIDED = """
    SELECT sr.* FROM match_results mr JOIN sanction_records sr ON sr.id = mr.sanction_record_id
    WHERE mr.superseded_by IS NULL AND mr.decision = :decision AND sr.file_id IS NULL {extra}
    ORDER BY sr.ordinal LIMIT :n
"""
ORGANIZATIONS = """
    SELECT sr.* FROM sanction_records sr JOIN ground_truth g ON g.sanction_record_id = sr.id
    WHERE g.scenario_tag = 'org_exact' AND sr.file_id IS NULL ORDER BY sr.ordinal LIMIT :n
"""


def authority(tag: str) -> str:
    return f"E2E-{tag}"


def build(tag: str, out: str) -> dict[str, Any]:
    with session_scope(Settings()) as session:

        def pick(sql: str, **params: Any) -> list[Any]:
            return list(session.execute(text(sql), params).mappings())

        chosen = [
            *[("match", r) for r in pick(DECIDED.format(extra="AND mr.route = 'deterministic' AND NOT sr.is_organization"), decision="MATCH", n=3)],
            *[("ambiguous", r) for r in pick(DECIDED.format(extra=""), decision="AMBIGUOUS", n=2)],
            *[("organization", r) for r in pick(ORGANIZATIONS, n=2)],
        ]

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append([*HEADERS.values(), "Notes"])
    records = []
    for i, (role, row) in enumerate(chosen):
        key = f"E2E-{tag}-{i}"
        values = [key if f == "record_id" else row.get(f) for f in HEADERS]
        sheet.append([v.isoformat() if hasattr(v, "isoformat") else v for v in values] + ["e2e"])
        name = row["organization_name"] if row["is_organization"] else row["last_name"]
        records.append({"record_id": key, "role": role, "name": name})
    book.save(out)
    return {"file": out, "authority": authority(tag), "headers": HEADERS, "records": records}


def cleanup(tag: str) -> dict[str, Any]:
    params = {"a": authority(tag), "pattern": f"%-{tag}@e2e.concordance.example.com"}
    users = "SELECT id FROM users WHERE email LIKE :pattern"
    records = "SELECT id FROM sanction_records WHERE source_authority = :a"
    files = f"SELECT id FROM sanction_files WHERE source_authority = :a OR uploaded_by IN ({users})"
    runs = f"SELECT id FROM reconciliation_runs WHERE file_id IN ({files}) OR triggered_by IN ({users})"
    with session_scope(Settings()) as session:
        session.execute(
            text(f"""DELETE FROM audit_logs WHERE actor_user_id IN ({users})
                OR entity_id IN (SELECT id::text FROM cases WHERE sanction_record_id IN ({records}))
                OR entity_id IN (SELECT id::text FROM match_results WHERE run_id IN ({runs}))"""),
            params,
        )
        session.execute(text(f"DELETE FROM cases WHERE sanction_record_id IN ({records})"), params)
        session.execute(text(f"DELETE FROM jobs WHERE payload->>'run_id' IN (SELECT id::text FROM ({runs}) r)"), params)
        session.execute(text(f"DELETE FROM reconciliation_runs WHERE id IN ({runs})"), params)
        session.execute(text("DELETE FROM sanction_records WHERE source_authority = :a"), params)
        session.execute(text(f"DELETE FROM sanction_files WHERE id IN ({files})"), params)
        session.execute(text("DELETE FROM column_mappings WHERE source_authority = :a"), params)
        gone = session.execute(text("DELETE FROM users WHERE email LIKE :pattern"), params).rowcount
    return {"authority": authority(tag), "users_removed": gone}


def sweep() -> dict[str, Any]:
    """Clean every run whose users still exist: interrupted runs, `E2E_KEEP=1` runs."""
    with session_scope(Settings()) as session:
        emails = session.scalars(text("SELECT email FROM users WHERE email LIKE '%@e2e.concordance.example.com'"))
        tags = sorted({e.split("@")[0].rsplit("-", 1)[-1] for e in emails})
    return {"swept": [cleanup(tag)["authority"] for tag in tags]}


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "build":
        result = build(argv[1], argv[2])
    elif len(argv) == 2 and argv[0] == "cleanup":
        result = cleanup(argv[1])
    elif argv == ["sweep"]:
        result = sweep()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    # The last line is the JSON: the database layer logs to stdout too.
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
