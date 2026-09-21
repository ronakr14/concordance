"""The whole Stage 7 workflow through HTTP, against the real database.

One module-scoped world, built once because a reconciliation run hashes the
50,000-provider snapshot over the network: register two users, upload a
workbook built from records the engine is known to decide a particular way,
commit it, run it through the worker, and then review the results.

The API connects as the least-privilege application role - the role production
uses - so a missing grant fails here rather than in a deployment. Cleanup runs
as the owner, which is the only role allowed to delete audit rows.

Every record this module creates belongs to a source authority named for this
run, so nothing it does can touch the loaded dataset, and teardown can find
every row by that name.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from openpyxl import Workbook

pytestmark = [pytest.mark.integration, pytest.mark.slow]

JWT_KEY = "integration-signing-key-long-enough-to-be-plausible"
PASSWORD = "a long enough passphrase 7"
LEIE = {
    "record_id": "RECKEY", "last_name": "LASTNAME", "first_name": "FIRSTNAME",
    "middle_name": "MIDNAME", "organization_name": "BUSNAME", "dob": "DOB", "npi": "NPI",
    "address_line1": "ADDRESS", "address_line2": "ADDRESS2", "city": "CITY", "state": "STATE",
    "zip": "ZIP", "specialty": "SPECIALTY", "license_number": "LICENSE",
    "license_state": "LICENSE_STATE", "sanction_type": "EXCLTYPE", "exclusion_date": "EXCLDATE",
    "reinstatement_date": "REINDATE", "ein": "EIN",
}
HEADERS = [*LEIE.values(), "Source Notes"]


@dataclass
class World:
    client: Any
    settings: Any
    tag: str
    authority: str
    admin: dict[str, str]
    analyst: dict[str, str]
    admin_id: str
    analyst_id: str
    file_id: str
    run_id: str
    #: record key in the workbook -> the role it plays in these tests
    roles: dict[str, str]
    #: the source rows, by record key, for building an updated file
    rows: dict[str, dict[str, Any]]
    expected: dict[str, str | None]
    data: bytes = b""
    #: `(request id, method, path)` of every mutating call that succeeded.
    mutations: list[tuple[str, str, str]] = field(default_factory=list)

    def call(self, method: str, path: str, *, as_: str = "admin", **kwargs: Any) -> Any:
        tokens = self.admin if as_ == "admin" else self.analyst
        rid = f"{self.tag}-{uuid.uuid4().hex[:10]}"
        headers = {"Authorization": f"Bearer {tokens['access_token']}", "X-Request-Id": rid}
        response = self.client.request(method, path, headers=headers, **kwargs)
        response.request_id = rid
        if method != "GET" and response.status_code < 400:
            self.mutations.append((rid, method, path))
        return response

    def results(self, run_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Current results of a run, by the workbook record key."""
        page = self.call("GET", f"/matches?run_id={run_id or self.run_id}&limit=100").json()
        return {item["record_id"]: item for item in page["items"]}

    def by_role(self, role: str, run_id: str | None = None) -> list[dict[str, Any]]:
        results = self.results(run_id)
        return [results[key] for key, r in self.roles.items() if r == role and key in results]


# --------------------------------------------------------------------------
# building the world
# --------------------------------------------------------------------------


def _workbook(rows: list[list[Any]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _row(values: dict[str, Any]) -> list[Any]:
    return [values.get(f) for f in LEIE] + ["integration test"]


def _pick(owner: Any, sql: str, n: int) -> list[Any]:
    from sqlalchemy import text

    return list(owner.execute(text(sql + f" LIMIT {n}")).mappings())


def _source_rows(owner: Any) -> dict[str, list[Any]]:
    """Records whose outcome the test can know in advance."""
    # Ground truth rather than current results: what the last run decided depends
    # on its config, and a test should not depend on whichever run someone made
    # most recently.
    truth = """
        SELECT sr.*, g.expected_provider_id AS decided_provider
        FROM sanction_records sr JOIN ground_truth g ON g.sanction_record_id = sr.id
        WHERE g.scenario_tag = '{tag}' AND sr.file_id IS NULL ORDER BY sr.ordinal
    """
    return {
        # An exact NPI is decided deterministically, whatever the config.
        "match": _pick(owner, truth.format(tag="exact_npi"), 5),
        # Built to be undecidable: every member of a common-name cluster fits
        # equally, so any config must decline it.
        "ambiguous": _pick(owner, truth.format(tag="ambiguous"), 3),
        "org": _pick(owner, truth.format(tag="org_exact"), 3),
    }


def _values(record: Any, key: str) -> dict[str, Any]:
    out = {f: record.get(f) for f in LEIE if f not in ("record_id", "dob")}
    out["record_id"] = key
    out["dob"] = record.get("dob_raw")
    for name in ("exclusion_date", "reinstatement_date"):
        if out.get(name) is not None:
            out[name] = out[name].isoformat()
    return out


@pytest.fixture(scope="module")
def world(app_url: str, owner_url: str, tmp_path_factory: Any) -> Iterator[World]:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from concordance.api.app import create_app
    from concordance.config import Settings
    from concordance.db.session import dispose_engine

    tag = f"t{uuid.uuid4().hex[:8]}"
    authority = f"TEST-{tag}"
    # Pre-ping: teardown runs minutes after setup, and a hosted database may
    # have closed the idle connection in between.
    owner_engine = create_engine(
        owner_url, connect_args={"connect_timeout": 20}, pool_pre_ping=True
    )
    owner = sessionmaker(bind=owner_engine)()

    settings = Settings(
        DATABASE_URL=owner_url,
        APP_DATABASE_URL=app_url,
        JWT_SECRET=JWT_KEY,
        DB_CONNECT_TIMEOUT=20,
        STORAGE_LOCAL_PATH=tmp_path_factory.mktemp("storage"),
    )
    dispose_engine()
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    created: dict[str, Any] = {"users": []}

    try:
        tokens: dict[str, dict[str, str]] = {}
        for role in ("admin", "analyst"):
            email = f"{role}-{tag}@concordance.example.com"
            registered = client.post(
                "/auth/register", json={"email": email, "password": PASSWORD, "role": role}
            )
            assert registered.status_code == 201, registered.text
            created["users"].append(registered.json()["id"])
            login = client.post("/auth/login", json={"email": email, "password": PASSWORD})
            assert login.status_code == 200, login.text
            tokens[role] = login.json()

        picked = _source_rows(owner)
        # End the read's transaction now. Held open for the whole module, it is
        # killed by the server's idle-in-transaction timeout, and teardown's
        # cleanup dies with it - leaving the module's rows behind.
        owner.rollback()
        assert len(picked["match"]) == 5 and picked["ambiguous"] and picked["org"]
        roles: dict[str, str] = {}
        rows: dict[str, dict[str, Any]] = {}
        expected: dict[str, str | None] = {}
        for role, records in picked.items():
            for i, record in enumerate(records):
                key = f"{tag.upper()}-{role[:3].upper()}{i}"
                roles[key] = role if role != "match" else f"match{i}"
                rows[key] = _values(record, key)
                expected[key] = record["decided_provider"]

        malformed = [
            ["TRUNCATED-ROW", "only two cells"],
            [f"EXTRA-{i}" for i in range(len(HEADERS) + 3)],
            ["=SUM(A1:A9)", 12345] + [None] * (len(HEADERS) - 2),
        ]
        data = _workbook([_row(v) for v in rows.values()] + malformed)

        world = World(
            client=client, settings=settings, tag=tag, authority=authority,
            admin=tokens["admin"], analyst=tokens["analyst"],
            admin_id=created["users"][0], analyst_id=created["users"][1],
            file_id="", run_id="", roles=roles, rows=rows, expected=expected, data=data,
        )
        yield world
    finally:
        _cleanup(owner, authority, created["users"])
        owner.close()
        owner_engine.dispose()
        dispose_engine()


def _cleanup(owner: Any, authority: str, users: list[str]) -> None:
    from sqlalchemy import text

    owner.rollback()
    records = "SELECT id FROM sanction_records WHERE source_authority = :a"
    files = "SELECT id FROM sanction_files WHERE source_authority = :a OR uploaded_by = ANY(CAST(:u AS uuid[]))"
    runs = f"""SELECT id FROM reconciliation_runs
               WHERE file_id IN ({files}) OR triggered_by = ANY(CAST(:u AS uuid[]))"""
    params = {"a": authority, "u": users}
    owner.execute(text(f"""DELETE FROM audit_logs WHERE actor_user_id = ANY(CAST(:u AS uuid[]))
        OR entity_id IN (SELECT id::text FROM cases WHERE sanction_record_id IN ({records}))
        OR entity_id IN (SELECT id::text FROM match_results WHERE run_id IN ({runs}))"""), params)
    owner.execute(text(f"DELETE FROM cases WHERE sanction_record_id IN ({records})"), params)
    owner.execute(text(f"DELETE FROM jobs WHERE payload->>'run_id' IN (SELECT id::text FROM ({runs}) r)"), params)
    owner.execute(text(f"DELETE FROM reconciliation_runs WHERE id IN ({runs})"), params)
    owner.execute(text("DELETE FROM sanction_records WHERE source_authority = :a"), params)
    owner.execute(text(f"DELETE FROM sanction_files WHERE id IN ({files})"), params)
    owner.execute(text("DELETE FROM column_mappings WHERE source_authority = :a"), params)
    owner.execute(text("DELETE FROM users WHERE id = ANY(CAST(:u AS uuid[]))"), params)
    owner.commit()


def _run_worker(world: World) -> None:
    from concordance.jobs.worker import Worker

    assert Worker(settings=world.settings, kinds=["reconcile"]).run_once() is not None


def _add_months(start: date, months: int) -> date:
    from concordance.cases.lifecycle import _add_months as add

    return add(start, months)


# --------------------------------------------------------------------------
# the tests, in workflow order
# --------------------------------------------------------------------------


def test_upload_inspects_and_proposes_without_ingesting(world: World) -> None:
    response = world.call(
        "POST", "/sanctions/upload",
        files={"file": ("leie_update.xlsx", world.data, "application/octet-stream")},
        data={"source_authority": world.authority},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "INSPECTED"
    assert body["proposal_source"] == "heuristic"
    assert body["proposed_mapping"] == LEIE, "the heuristic recovers the LEIE dialect"
    assert body["unmapped_required"] == []
    assert body["rows"] == len(world.rows) + 3
    world.file_id = body["file_id"]

    records = world.call("GET", f"/sanctions?file_id={world.file_id}").json()
    assert records["total"] == 0, "inspection ingests nothing"


def test_a_byte_identical_reupload_is_refused(world: World) -> None:
    response = world.call(
        "POST", "/sanctions/upload",
        files={"file": ("again.xlsx", world.data, "application/octet-stream")},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "duplicate_file"
    assert error["details"]["file_id"] == world.file_id


def test_commit_with_an_incomplete_mapping_names_the_missing_field(world: World) -> None:
    response = world.call(
        "POST", f"/sanctions/upload/{world.file_id}/commit",
        json={"mapping": {"npi": "NPI", "state": "NOT A COLUMN"}},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_mapping"
    assert set(error["details"]) == {
        "mapping.state", "mapping.last_name", "mapping.organization_name"
    }


def test_commit_ingests_the_good_rows_and_reports_the_bad(world: World) -> None:
    response = world.call(
        "POST", f"/sanctions/upload/{world.file_id}/commit", json={"mapping": LEIE}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMMITTED"
    assert (body["new"], body["replaced"], body["unchanged"]) == (len(world.rows), 0, 0)
    assert body["rejected"] == 3
    assert {r["row"] for r in body["rejected_rows"]} == {
        len(world.rows) + 2, len(world.rows) + 3, len(world.rows) + 4
    }

    again = world.call("POST", f"/sanctions/upload/{world.file_id}/commit", json={"mapping": LEIE})
    assert again.status_code == 409 and again.json()["error"]["code"] == "file_not_inspected"

    detail = world.call("GET", f"/sanctions/files/{world.file_id}").json()
    assert detail["mapping_name"] and detail["summary"]["new"] == len(world.rows)

    record = world.call("GET", f"/sanctions?file_id={world.file_id}&limit=1").json()["items"][0]
    full = world.call("GET", f"/sanctions/{record['id']}").json()
    assert "raw" in full, full
    assert full["raw"]["Source Notes"] == "integration test", "unmapped columns are kept in raw"


def test_the_mapping_was_saved_as_the_authoritys_default(world: World) -> None:
    mappings = world.call("GET", f"/column-mappings?source_authority={world.authority}").json()
    assert len(mappings) == 1 and mappings[0]["is_default"]
    changed = {**mappings[0], "mapping": {"last_name": "LASTNAME"}}
    edit = world.call(
        "PUT", f"/column-mappings/{mappings[0]['id']}",
        json={k: changed[k] for k in ("name", "source_authority", "mapping", "is_default")},
    )
    assert edit.status_code == 409 and edit.json()["error"]["code"] == "mapping_in_use"


def test_a_run_is_queued_immediately_and_a_second_is_refused(world: World) -> None:
    response = world.call("POST", "/reconciliation/run", json={"file_id": world.file_id})
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "QUEUED" and run["job_id"] and run["scoring_config_id"]
    world.run_id = run["id"]

    second = world.call("POST", "/reconciliation/run", json={"file_id": world.file_id})
    assert second.status_code == 409
    assert second.json()["error"]["details"]["run_id"] == world.run_id


def test_the_worker_completes_the_queued_run(world: World) -> None:
    _run_worker(world)
    run = world.call("GET", f"/reconciliation/runs/{world.run_id}").json()
    assert run["status"] == "COMPLETED", run["error"]
    assert run["records_total"] == len(world.rows)
    assert run["sanction_snapshot_hash"] and run["provider_snapshot_hash"]

    results = world.results()
    assert set(results) == set(world.rows), "a file-scoped run scores that file and nothing else"


def test_the_engine_decides_the_copied_records_as_it_did_the_originals(world: World) -> None:
    for i in range(5):
        [result] = world.by_role(f"match{i}")
        key = result["record_id"]
        assert result["decision"] == "MATCH"
        assert result["chosen_provider_id"] == world.expected[key]
    assert all(r["decision"] == "AMBIGUOUS" for r in world.by_role("ambiguous"))


def test_organizations_are_routed_to_the_organization_model(world: World) -> None:
    orgs = world.by_role("org")
    assert orgs and all(o["is_organization"] for o in orgs)
    for org in orgs:
        detail = world.call("GET", f"/matches/{org['id']}").json()
        assert detail["explanation"]["model"] == "organization"
    matched = [o for o in orgs if o["decision"] == "MATCH"]
    assert matched, "an exact organization copy should match"
    assert all(o["chosen_provider_id"] == world.expected[o["record_id"]] for o in matched)


def test_the_detail_carries_the_evidence_and_the_view_is_audited(world: World) -> None:
    [result] = world.by_role("match0")
    response = world.call("GET", f"/matches/{result['id']}", as_="analyst")
    detail = response.json()
    top = detail["candidates"][0]
    assert top["provider"]["provider_id"] == top["provider_id"]
    assert top["field_weights"] and top["field_levels"]
    assert detail["sanction_record"]["record_id"] == result["record_id"]

    audit = world.call(
        "GET", f"/audit?entity_type=match_result&entity_id={result['id']}&action=match.viewed"
    ).json()
    assert any(row["request_id"] == response.request_id for row in audit["items"])


def test_approval_is_admin_only_opens_a_case_and_is_idempotent(world: World) -> None:
    [result] = world.by_role("match0")
    denied = world.call("POST", f"/matches/{result['id']}/approve", as_="analyst", json={})
    assert denied.status_code == 403

    approved = world.call("POST", f"/matches/{result['id']}/approve", json={"comment": "clear NPI match"})
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["case_created"] is True
    case = body["case"]
    start = date.fromisoformat(case["start_date"])
    assert case["duration_months"] == 3
    assert date.fromisoformat(case["end_date"]) == _add_months(start, 3)
    assert body["match"]["approved_provider_id"] == result["chosen_provider_id"]

    duplicate = world.call("POST", f"/matches/{result['id']}/approve", json={})
    assert duplicate.status_code == 409
    error = duplicate.json()["error"]
    assert error["code"] == "already_approved" and error["details"]["case_id"] == case["id"]

    cases = world.call("GET", f"/cases?provider_id={result['chosen_provider_id']}").json()
    assert [c["id"] for c in cases["items"] if c["match_result_id"] == result["id"]] == [case["id"]]


def test_an_ambiguous_match_needs_an_explicit_candidate(world: World) -> None:
    [first, *_] = world.by_role("ambiguous")
    bare = world.call("POST", f"/matches/{first['id']}/approve", json={})
    assert bare.status_code == 422
    assert bare.json()["error"]["code"] == "provider_choice_required"
    assert bare.json()["error"]["details"]["candidates"]

    stranger = world.call("POST", f"/matches/{first['id']}/approve", json={"provider_id": "P-NOBODY"})
    assert stranger.json()["error"]["code"] == "provider_not_a_candidate"

    engine_pick = first["chosen_provider_id"]
    choice = next(c for c in bare.json()["error"]["details"]["candidates"] if c != engine_pick)
    approved = world.call(
        "POST", f"/matches/{first['id']}/approve",
        json={"provider_id": choice, "duration_months": 6},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["match"]["approved_provider_id"] == choice
    assert body["match"]["chosen_provider_id"] == engine_pick, "the engine's answer is left as it was"
    case = body["case"]
    assert case["duration_months"] == 6
    assert date.fromisoformat(case["end_date"]) == _add_months(date.fromisoformat(case["start_date"]), 6)


def test_reject_needs_a_comment_and_records_feedback(world: World, owner_session: Any) -> None:
    from sqlalchemy import text

    [result] = world.by_role("match1")
    silent = world.call("POST", f"/matches/{result['id']}/reject", as_="analyst", json={})
    assert silent.status_code == 422

    rejected = world.call(
        "POST", f"/matches/{result['id']}/reject", as_="analyst",
        json={"comment": "different person, same name"},
    )
    assert rejected.status_code == 200 and rejected.json()["review_status"] == "REJECTED"

    labels = owner_session.execute(
        text("SELECT label, comparison_vector FROM feedback_events WHERE match_result_id = :m"),
        {"m": result["id"]},
    ).all()
    assert [label for label, _ in labels] == ["FALSE_MATCH"]
    assert labels[0][1]["field_levels"], "the vector the reviewer saw is stored with the label"


def test_an_escalated_match_is_decided_by_an_admin(world: World) -> None:
    [result] = world.by_role("match2")
    escalated = world.call(
        "POST", f"/matches/{result['id']}/escalate", as_="analyst", json={"comment": "unsure"}
    )
    assert escalated.status_code == 200 and escalated.json()["review_status"] == "ESCALATED"

    handed_back = world.call(
        "POST", f"/matches/{result['id']}/reject", as_="analyst", json={"comment": "no"}
    )
    assert handed_back.status_code == 403
    assert handed_back.json()["error"]["code"] == "escalated_to_admin"

    approved = world.call("POST", f"/matches/{result['id']}/approve", json={})
    assert approved.status_code == 200


def test_cases_open_close_reopen_and_expire(world: World, owner_session: Any) -> None:

    from concordance.cases.lifecycle import expire_cases

    [result] = world.by_role("match0")
    [case] = [c for c in world.call("GET", "/cases?status=ACTIVE&limit=100").json()["items"]
              if c["match_result_id"] == result["id"]]

    second = world.call("POST", "/cases", json={"match_result_id": result["id"]})
    assert second.status_code == 409 and second.json()["error"]["code"] == "case_already_active"

    assert world.call("POST", f"/cases/{case['id']}/close", as_="analyst",
                      json={"reason": "resolved"}).status_code == 403
    closed = world.call("POST", f"/cases/{case['id']}/close", json={"reason": "provider reinstated"})
    assert closed.status_code == 200 and closed.json()["status"] == "CLOSED"
    again = world.call("POST", f"/cases/{case['id']}/close", json={"reason": "twice"})
    assert again.status_code == 409

    started = date(2000, 1, 31)
    reopened = world.call(
        "POST", "/cases",
        json={"match_result_id": result["id"], "duration_months": 1, "start_date": started.isoformat()},
    )
    assert reopened.status_code == 201, reopened.text
    fresh = reopened.json()
    assert fresh["end_date"] == "2000-02-29", "a month from 31 January is the end of February"

    from concordance.db.session import session_scope

    with session_scope(world.settings) as session:
        report = expire_cases(session)
    assert fresh["id"] in report.case_ids

    detail = world.call("GET", f"/cases/{fresh['id']}").json()
    assert detail["status"] == "EXPIRED"
    assert detail["provider"]["provider_id"] == result["chosen_provider_id"]
    trail = world.call("GET", f"/cases/{fresh['id']}/audit", as_="analyst").json()["items"]
    assert [row["action"] for row in trail] == ["case.expired", "case.opened"]
    assert trail[0]["actor_role"] == "system" and trail[0]["actor_user_id"] is None


def test_a_case_that_has_not_begun_reads_as_pending(world: World) -> None:
    """PENDING is derived: stored ACTIVE, with a start date still ahead."""
    [result] = world.by_role("match0")  # its earlier cases are closed and expired
    before = world.call("GET", "/stats/kpis").json()
    starts = datetime.now(UTC).date() + timedelta(days=30)
    opened = world.call(
        "POST", "/cases", json={"match_result_id": result["id"], "start_date": starts.isoformat()}
    )
    assert opened.status_code == 201, opened.text
    case = opened.json()
    assert case["status"] == "ACTIVE" and case["phase"] == "PENDING"

    def listed(phase: str) -> set[str]:
        rows = world.call("GET", f"/cases?status={phase}&limit=100", as_="analyst").json()["items"]
        return {c["id"] for c in rows}

    assert case["id"] in listed("PENDING") and case["id"] not in listed("ACTIVE")
    assert world.call("GET", f"/cases/{case['id']}").json()["phase"] == "PENDING"
    after = world.call("GET", "/stats/kpis").json()
    assert after["cases_pending"] == before["cases_pending"] + 1
    assert after["cases_active"] == before["cases_active"]
    chart = {b["label"]: b["count"] for b in world.call("GET", "/stats/case-status").json()}
    assert chart["PENDING"] == after["cases_pending"] and chart["ACTIVE"] == after["cases_active"]

    closed = world.call("POST", f"/cases/{case['id']}/close", json={"reason": "opened for a test"})
    assert closed.status_code == 200 and closed.json()["phase"] == "CLOSED"


def test_an_updated_file_supersedes_and_flags_the_live_case(world: World) -> None:
    """Q5: a new version of a record re-decides it; the approved case is flagged, not changed."""
    [target] = world.by_role("match2")  # approved after escalation: its case is live
    key = target["record_id"]
    changed = {
        **world.rows[key],
        "last_name": "QWXZYVONNEGUT", "first_name": "ZEBEDIAH", "npi": None, "dob": "1901-01-01",
        "address_line1": "1 Nowhere Plaza", "city": "Nome", "state": "AK", "zip": "99762",
        "license_number": None,
    }
    rows = [_row(changed if k == key else v) for k, v in world.rows.items()]
    upload = world.call(
        "POST", "/sanctions/upload",
        files={"file": ("leie_next_month.xlsx", _workbook(rows), "application/octet-stream")},
    )
    assert upload.status_code == 201, upload.text
    inspected = upload.json()
    assert inspected["proposal_source"] == "stored", "the saved mapping is recognised by its headers"
    assert inspected["source_authority"] == world.authority

    committed = world.call(
        "POST", f"/sanctions/upload/{inspected['file_id']}/commit",
        json={"mapping": inspected["proposed_mapping"]},
    ).json()
    assert (committed["new"], committed["replaced"], committed["unchanged"]) == (0, 1, len(world.rows) - 1)

    stale = world.call("POST", "/reconciliation/run", json={"file_id": world.file_id})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "file_outdated"

    queued = world.call("POST", "/reconciliation/run", json={"file_id": inspected["file_id"]})
    assert queued.status_code == 202
    _run_worker(world)
    run = world.call("GET", f"/reconciliation/runs/{queued.json()['id']}").json()
    assert run["status"] == "COMPLETED" and run["records_total"] == 1

    [new] = world.call("GET", f"/matches?run_id={run['id']}").json()["items"]
    assert new["decision"] != "MATCH" or new["chosen_provider_id"] != target["chosen_provider_id"]

    current = world.results()
    assert key not in current, "the old decision is no longer current"
    history = world.call(
        "GET", f"/matches?run_id={world.run_id}&include_superseded=true&limit=100"
    ).json()["items"]
    old = next(h for h in history if h["record_id"] == key)
    assert old["superseded_by"] == new["id"] and old["review_status"] == "APPROVED"

    conflicted = world.call("GET", "/cases?conflict=true&limit=100").json()["items"]
    [case] = [c for c in conflicted if c["match_result_id"] == target["id"]]
    assert case["status"] == "ACTIVE", "a re-run never closes a case a human opened"
    assert case["conflict_match_result_id"] == new["id"]
    flagged = world.call("GET", "/matches?conflict=true&limit=100").json()["items"]
    assert new["id"] in {m["id"] for m in flagged}


def test_the_detail_names_its_band_its_reviewer_and_the_case_it_contradicts(world: World) -> None:
    [approved] = world.by_role("match0")
    detail = world.call("GET", f"/matches/{approved['id']}").json()
    band = detail["band"]
    assert band is not None and band["version"]
    assert 0.0 <= band["t_auto_reject"] <= band["t_auto_accept"] <= 1.0
    assert detail["reviewed_by_email"] == f"admin-{world.tag}@concordance.example.com"

    # The result that superseded match2's approved decision contradicts its live case.
    old = _superseded(world, "match2")
    newer = world.call("GET", f"/matches/{old['superseded_by']}").json()
    [conflicting] = newer["conflicting_cases"]
    assert conflicting["match_result_id"] == old["id"] and conflicting["status"] == "ACTIVE"


def _superseded(world: World, role: str) -> dict[str, Any]:
    """The first run's result for a role whose record a later upload replaced."""
    history = world.call(
        "GET", f"/matches?run_id={world.run_id}&include_superseded=true&limit=100"
    ).json()["items"]
    return next(h for h in history if world.roles.get(h["record_id"]) == role)


def test_the_provider_directory_derives_compliance(world: World) -> None:
    [pending] = world.by_role("match3")
    # match2 was approved, so its provider holds a live case - flagged, not closed.
    excluded_id = _superseded(world, "match2")["approved_provider_id"]

    listed = world.call("GET", f"/providers?q={excluded_id}", as_="analyst").json()
    assert [p["provider_id"] for p in listed["items"]] == [excluded_id]
    assert listed["items"][0]["compliance_status"] == "EXCLUDED"
    not_clear = world.call("GET", f"/providers?q={excluded_id}&compliance=CLEAR").json()
    assert not_clear["total"] == 0, "the compliance filter agrees with the column"

    # Pending and proposed by the engine: under review, unless another record's
    # approval has already excluded the same provider.
    reviewed = world.call("GET", f"/providers/{pending['chosen_provider_id']}").json()
    assert reviewed["compliance_status"] in ("UNDER_REVIEW", "EXCLUDED")
    ranked = {m["id"]: m for m in reviewed["matches"]}
    assert ranked[pending["id"]]["candidate_rank"] >= 1

    profile = world.call("GET", f"/providers/{excluded_id}").json()
    assert any(c["status"] == "ACTIVE" for c in profile["cases"])
    if profile["npi"]:
        by_npi = world.call("GET", f"/providers?q={profile['npi']}").json()["items"]
        assert excluded_id in {p["provider_id"] for p in by_npi}
    name = profile["organization_name"] if profile["is_organization"] else profile["last_name"]
    by_name = world.call("GET", f"/providers?q={name.lower()}&limit=500").json()
    assert by_name["total"] >= 1, "name search folds case the way the engine does"

    orgs = world.call("GET", "/providers?record_type=organization&limit=5&sort=name").json()
    assert orgs["items"] and all(p["is_organization"] for p in orgs["items"])
    assert world.call("GET", "/providers/P-NOBODY-AT-ALL").status_code == 404


def test_case_rows_and_audit_rows_name_people_not_ids(world: World) -> None:
    rows = world.call("GET", "/cases?limit=100", as_="analyst").json()["items"]
    ours = [c for c in rows if c["created_by_email"] == f"admin-{world.tag}@concordance.example.com"]
    assert ours and all(c["provider_name"] and c["subject_name"] for c in ours)
    closed = [c for c in ours if c["status"] == "CLOSED"]
    assert closed and closed[0]["closed_by_email"] == ours[0]["created_by_email"]

    trail = world.call("GET", f"/audit?actor_user_id={world.analyst_id}&limit=5").json()["items"]
    assert trail and {r["actor_email"] for r in trail} == {f"analyst-{world.tag}@concordance.example.com"}


def test_facets_offer_the_values_on_file(world: World) -> None:
    facets = world.call("GET", "/sanctions/facets", as_="analyst").json()
    assert world.authority in facets["source_authorities"]
    assert facets["states"] == sorted(facets["states"]) and facets["sanction_types"]


def test_bulk_review_reports_each_item_and_commits_the_rest(world: World) -> None:
    [first] = world.by_role("match3")
    [second] = world.by_role("match4")
    [done] = world.by_role("match0")  # approved long ago
    stranger = str(uuid.uuid4())

    response = world.call(
        "POST", "/matches/bulk", as_="analyst",
        json={"ids": [first["id"], done["id"], stranger, second["id"], first["id"]],
              "action": "escalate", "comment": "batch: same surname cluster"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["id"] for r in body["results"]] == [first["id"], done["id"], stranger, second["id"]], (
        "request order, duplicates folded"
    )
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id[first["id"]]["ok"] and by_id[first["id"]]["review_status"] == "ESCALATED"
    assert by_id[second["id"]]["ok"]
    assert by_id[done["id"]]["error"]["code"] == "not_pending"
    assert by_id[stranger]["error"]["code"] == "not_found"
    assert (body["succeeded"], body["failed"]) == (2, 2)

    handed_back = world.call(
        "POST", "/matches/bulk", as_="analyst",
        json={"ids": [first["id"]], "action": "reject", "comment": "no"},
    ).json()
    assert handed_back["results"][0]["error"]["code"] == "escalated_to_admin"

    refused = world.call(
        "POST", "/matches/bulk", json={"ids": [first["id"]], "action": "approve", "comment": "x"}
    )
    assert refused.status_code == 422, "approval is never offered in bulk"

    audited = world.call(
        "GET", f"/audit?entity_type=match_result&entity_id={second['id']}&action=match.escalated"
    ).json()["items"]
    assert [row["request_id"] for row in audited] == [response.request_id]


def test_a_queued_run_can_be_cancelled_and_the_worker_leaves_it_alone(world: World) -> None:
    queued = world.call("POST", "/reconciliation/run", json={"limit": 5})
    if queued.status_code == 409:
        pytest.skip("another global run is live on this database")
    run_id = queued.json()["id"]
    cancelled = world.call("POST", f"/reconciliation/runs/{run_id}/cancel", as_="analyst")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    again = world.call("POST", f"/reconciliation/runs/{run_id}/cancel")
    assert again.status_code == 409

    _run_worker(world)
    after = world.call("GET", f"/reconciliation/runs/{run_id}").json()
    assert after["status"] == "CANCELLED" and after["records_total"] == 0


def test_pagination_boundaries(world: World) -> None:
    total = world.call("GET", f"/matches?run_id={world.run_id}&include_superseded=true").json()["total"]
    assert total == len(world.rows)
    last = world.call(
        "GET", f"/matches?run_id={world.run_id}&include_superseded=true&limit=5&offset={total - 1}"
    ).json()
    assert (len(last["items"]), last["total"], last["offset"], last["limit"]) == (1, total, total - 1, 5)
    past = world.call(
        "GET", f"/matches?run_id={world.run_id}&include_superseded=true&offset={total}"
    ).json()
    assert past["items"] == [] and past["total"] == total

    pages = [
        world.call("GET", f"/matches?run_id={world.run_id}&include_superseded=true&limit=4&offset={o}")
        .json()["items"]
        for o in range(0, total, 4)
    ]
    ids = [m["id"] for page in pages for m in page]
    assert len(ids) == len(set(ids)) == total, "pages neither repeat nor skip"


def test_filters_narrow_the_queue(world: World) -> None:
    orgs = world.call("GET", f"/matches?run_id={world.run_id}&record_type=organization").json()
    assert {m["record_id"] for m in orgs["items"]} == {
        k for k, role in world.roles.items() if role == "org"
    } - {m["record_id"] for m in orgs["items"] if m["superseded_by"]}
    high = world.call("GET", f"/matches?run_id={world.run_id}&min_confidence=0.99").json()["items"]
    assert all(m["calibrated_confidence"] >= 0.99 for m in high)
    approved = world.call("GET", f"/matches?run_id={world.run_id}&review_status=APPROVED").json()
    assert all(m["review_status"] == "APPROVED" for m in approved["items"])
    today = datetime.now(UTC).date().isoformat()
    dated = world.call("GET", f"/matches?run_id={world.run_id}&date_from={today}&date_to={today}").json()
    assert dated["total"] > 0


def test_the_dashboard_numbers(world: World) -> None:
    kpis = world.call("GET", "/stats/kpis", as_="analyst").json()
    assert kpis["providers"] >= 50_000 and kpis["cases_active"] >= 1 and kpis["conflicts"] >= 1
    bins = world.call("GET", "/stats/confidence-distribution").json()
    assert len(bins) == 10 and bins[-1]["label"] == "0.9-1.0"
    assert world.call("GET", "/stats/state-distribution").json()
    statuses = {b["label"] for b in world.call("GET", "/stats/case-status").json()}
    assert statuses == {"PENDING", "ACTIVE", "EXPIRED", "CLOSED", "REJECTED"}
    assert world.call("GET", "/stats/reconciliation-volume?days=2").json()[-1]["runs"] >= 2


def test_the_remaining_reads_and_mapping_endpoints(world: World) -> None:
    assert world.client.get("/health").json()["database"] is True
    me = world.call("GET", "/auth/me", as_="analyst").json()
    assert (me["id"], me["role"]) == (world.analyst_id, "analyst")
    assert "password_hash" not in me

    runs = world.call("GET", f"/reconciliation/runs?file_id={world.file_id}").json()
    assert [r["id"] for r in runs["items"]] == [world.run_id]
    files = world.call("GET", "/sanctions/files?status=COMMITTED&limit=500").json()
    assert world.file_id in {f["id"] for f in files["items"]}

    created = world.call(
        "POST", "/column-mappings",
        json={"name": "narrow", "source_authority": world.authority,
              "mapping": {"last_name": "LASTNAME", "npi": "NPI"}},
    )
    assert created.status_code == 201 and created.json()["is_default"] is False
    promoted = world.call("POST", f"/column-mappings/{created.json()['id']}/default")
    assert promoted.status_code == 200 and promoted.json()["is_default"] is True
    others = world.call("GET", f"/column-mappings?source_authority={world.authority}").json()
    assert [m["id"] for m in others if m["is_default"]] == [created.json()["id"]]

    unusable = world.call(
        "POST", "/column-mappings",
        json={"name": "nameless", "source_authority": world.authority, "mapping": {"npi": "NPI"}},
    )
    assert unusable.status_code == 422 and "mapping.last_name" in unusable.json()["error"]["details"]


def test_logout_revokes_the_refresh_token(world: World) -> None:
    login = world.client.post(
        "/auth/login",
        json={"email": f"admin-{world.tag}@concordance.example.com", "password": PASSWORD},
    ).json()
    assert world.client.post(
        "/auth/logout", json={"refresh_token": login["refresh_token"]}
    ).status_code == 204
    refused = world.client.post("/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert refused.status_code == 401


def test_refresh_rotates_and_a_reused_token_is_refused(world: World) -> None:
    old = world.analyst["refresh_token"]
    rotated = world.client.post("/auth/refresh", json={"refresh_token": old})
    assert rotated.status_code == 200
    replay = world.client.post("/auth/refresh", json={"refresh_token": old})
    assert replay.status_code == 401
    # Reuse is read as theft: every session for the user is cut, including
    # the pair just issued.
    stolen = world.client.post(
        "/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert stolen.status_code == 401


def test_the_browser_session_keeps_its_refresh_token_in_an_httponly_cookie(world: World) -> None:
    from concordance.api.routers.auth import REFRESH_COOKIE

    login = world.client.post(
        "/auth/login",
        json={"email": f"admin-{world.tag}@concordance.example.com", "password": PASSWORD,
              "transport": "cookie"},
    )
    assert login.status_code == 200
    assert login.json()["refresh_token"] is None, "script never sees the refresh token"
    set_cookie = login.headers["set-cookie"]
    assert set_cookie.startswith(f"{REFRESH_COOKIE}=")
    for attribute in ("HttpOnly", "SameSite=strict", "Path=/api/auth"):
        assert attribute.lower() in set_cookie.lower(), attribute
    first = login.cookies[REFRESH_COOKIE]

    def refresh_with(token: str) -> Any:
        # Sent by hand: the cookie's path is the browser's `/api/auth`, which
        # the test client, talking to the API directly, never requests.
        return world.client.post("/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={token}"})

    rotated = refresh_with(first)
    assert rotated.status_code == 200 and rotated.json()["refresh_token"] is None
    second = rotated.cookies[REFRESH_COOKIE]
    assert second != first

    replayed = refresh_with(first)
    assert replayed.status_code == 401
    assert f'{REFRESH_COOKIE}=""' in replayed.headers["set-cookie"], "a refused cookie is cleared"

    bare = world.client.post("/auth/refresh")
    assert bare.status_code == 401 and bare.json()["error"]["code"] == "invalid_refresh_token"

    out = world.client.post("/auth/logout", headers={"Cookie": f"{REFRESH_COOKIE}={second}"})
    assert out.status_code == 204 and "max-age=0" in out.headers["set-cookie"].lower()


def test_every_mutation_left_an_audit_row_with_its_request_id(
    world: World, owner_session: Any
) -> None:
    from sqlalchemy import text

    audited = {
        rid
        for (rid,) in owner_session.execute(
            text("SELECT request_id FROM audit_logs WHERE request_id LIKE :t"),
            {"t": f"{world.tag}-%"},
        )
    }
    assert len(world.mutations) >= 15
    unaudited = [(method, path) for rid, method, path in world.mutations if rid not in audited]
    assert not unaudited, f"mutations with no audit row: {unaudited}"

    logins = owner_session.execute(
        text("SELECT count(*) FROM audit_logs WHERE action = 'user.login' "
             "AND actor_user_id = ANY(CAST(:u AS uuid[]))"),
        {"u": [world.admin_id, world.analyst_id]},
    ).scalar()
    assert logins == 4, "two at setup, one each for the logout and cookie tests"

    actions = {
        a for (a,) in owner_session.execute(
            text("SELECT DISTINCT action FROM audit_logs WHERE request_id LIKE :t"),
            {"t": f"{world.tag}-%"},
        )
    }
    assert {
        "sanction_file.inspected", "sanction_file.committed", "column_mapping.created",
        "run.requested", "run.cancelled", "match.viewed", "match.approved", "match.rejected",
        "match.escalated", "case.opened", "case.closed",
    } <= actions

    denied = world.call("GET", "/audit", as_="analyst")
    assert denied.status_code == 403
