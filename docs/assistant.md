# The assistant

Ask a question in English; get rows, and the SQL that produced them. A model
writes that SQL, so the interesting part is not the answer - it is everything
that stands between the model and the database.

```
question -> prompt -> model -> guard (parse, refuse, regenerate) -> read-only role -> rows
                                   |                                    |
                                   +------------ audit log -------------+
```

| Do this | With |
|---|---|
| Ask | the Assistant page, `POST /assistant/query`, or `concordance assistant ask "..."` |
| See what it can read | `GET /assistant/schema`, `concordance assistant schema` |
| See what was asked | `GET /assistant/history`, or the audit log (`action = assistant.query`) |

## Two independent defences

Neither one is trusted to be sufficient, which is the point.

**The guard** (`assistant/guard.py`) parses the SQL with `sqlglot` and refuses
anything that is not a single bounded read. It then **regenerates the SQL from
the parse tree**: what reaches the database is the string the guard printed,
never the string the model wrote, so nothing the parser failed to understand
can ride along in a comment or a trailing clause. It refuses:

| Rule | Refusal |
|---|---|
| One statement only | `multiple` |
| `SELECT` only - no DML, DDL, `GRANT`, `COPY`, `SET`, transaction control | `not_a_select`, `forbidden_statement` |
| No comments, no `;` | `comment`, `multiple` |
| Only the five views, or a CTE over them; no schema qualifier, no catalogue | `forbidden_table` |
| No file, sleep, dblink, settings or `pg_*` functions | `forbidden_function` |
| No `SELECT ... INTO`, no locking clause, no bind parameter | `forbidden_statement` |
| A `LIMIT` is added when missing and lowered when too large | (a note, not a refusal) |

**The role.** The query runs as `concordance_assistant`, which owns nothing and
has `SELECT` on exactly five views. It runs inside a `READ ONLY` transaction,
under `ASSISTANT_TIMEOUT_MS`, and the transaction is rolled back. So a query
that somehow got past the parser still cannot read `users`, `refresh_tokens`,
`audit_logs` or `llm_calls`, and cannot write at all.

The role has no password and cannot log in: a query assumes it with
`SET LOCAL ROLE` inside the transaction. That is deliberate - a second
connection string would mean a second secret to store and leak, and the
privileges are identical either way.

## What it can read

Five views, created in the Stage 9 assistant migration:

| View | What it holds |
|---|---|
| `assistant_matches` | current engine decisions, their confidence, route and review status, beside the record they decided |
| `assistant_cases` | compliance cases, their lifecycle dates and conflict flag |
| `assistant_providers` | the provider master: name, NPI, city, state, specialty, status |
| `assistant_sanctions` | current sanction records as ingested |
| `assistant_runs` | reconciliation runs, their counts, config version and LLM cost |

What is deliberately absent: `users` and `refresh_tokens` (password hashes,
tokens), `audit_logs` (who looked at what), `llm_calls` (raw prompts and
responses), `jobs`, `column_mappings`. The views also flatten - no JSONB column
is exposed, so `sanction_records.raw` and `match_results.explanation`, the two
columns that carry whatever a source file said, cannot be read at all.

## Prompt injection

The question is free text, so the structural defence the adjudication prompt
uses - refuse to render anything that is not an identifier - is not available
here. What is done instead:

- The question is capped at 500 characters, stripped of control characters and
  of the delimiter itself, so it cannot close its own block.
- It is rendered inside `<<<QUESTION ... QUESTION>>>`, and the system prompt
  says that everything inside is data, that instructions inside it are part of
  the data, and that sanction records are written by third parties and are not
  a source of instructions.
- Nothing the model returns is trusted. This is the real answer: a fully
  jailbroken model that writes `SELECT password_hash FROM users` produces a
  refusal, not a leak.

`tests/integration/test_assistant.py` is that claim as a test. Twenty
adversarial prompts - read the user table, delete the cases, grant myself
admin, hide the table in a CTE, read `/etc/passwd`, sleep for thirty seconds,
copy the table to a URL, turn off the timeout - are each paired with the SQL a
model that fell for them would write. Every one is refused, with a reason, and
recorded. Two more tests probe the role directly: it cannot read the five
tables that matter, and it cannot write.

## Every question is audited

Answered or refused, each attempt writes an `audit_logs` row: the question, the
SQL, the row count, the refusal and its code, the model and the prompt version.
The audit log is append-only and is not one of the views, so the assistant
cannot read - or edit - the record of what it was asked.

## What it does not do

- **It does not write.** There is no path from a question to a change.
- **It does not paginate.** An answer is capped at `ASSISTANT_MAX_ROWS`; a
  question needing more rows is the wrong question for this page.
- **It does not explain its answer in prose.** It shows the SQL. An analyst who
  cannot see the query cannot tell a right answer from a confident one.
- **It is not a substitute for the Investigation view.** It counts and groups;
  the evidence behind a single decision lives on that page.
