# Architecture

How the pieces fit, and why each boundary is where it is. The engine itself is in
[`matching_engine.md`](matching_engine.md); runs, the queue, replay and diff are in
[`orchestration.md`](orchestration.md). This document is about the shape of the whole
system, the seams that were designed deliberately, and the decisions a reviewer is most
likely to want the reasoning behind.

---

## 1. Three processes

```
make up
  ├── preflight            env, database, migrations, ports, node_modules
  ├── alembic upgrade head
  ├── api      concordance.cli api serve     :8000
  ├── worker   concordance.cli jobs worker
  └── web      vite                          :5173
```

The api and the worker are **the same entrypoint module with different arguments**. That
was originally a property of sharing one container image, and it survived
containerization being cut because it was never really about the image: one package
means one settings object, one logging setup, one view of the domain, and no chance of
the scoring the API reports drifting from the scoring the worker performed.

The supervisor (`scripts/supervise.py`) exists because three processes that each fail on
their own give a reader three unrelated errors, none of which names the cause. It runs
the preflight first, applies migrations once, then spawns all three and labels their
interleaved output. One Ctrl-C stops the lot, by process tree: npm on Windows is a
`cmd.exe` shim whose `node` grandchild outlives a plain terminate and keeps port 5173.

## 2. The request path

```
browser ──▶ vite dev proxy /api ──▶ FastAPI ──▶ repositories ──▶ Postgres
                                        │
                                        └──▶ jobs table ──▶ worker ──▶ engine
```

Anything that takes longer than a request goes on the queue rather than into a
background thread: a reconciliation run, a Lab sweep, the LLM cost experiment, case
expiry. The queue is a Postgres table claimed with `FOR UPDATE SKIP LOCKED`, which is
the one place "build it in-house" and "the better engineering choice" agree — the job
row and the data it describes commit in the same transaction, so a run and the record
saying the run happened cannot disagree. See `orchestration.md` §1.

## 3. The matching pipeline

```
sanction record
   │
   ├─▶ normalization        names, addresses, organization suffixes
   ├─▶ NPI validation       Luhn checksum, not a length check
   ├─▶ blocking             candidate generation, recall 0.996 at ~43 candidates
   ├─▶ comparison vectors   per-field agreement levels, not similarity scores
   ├─▶ Fellegi–Sunter       weights learned by EM on unlabelled data
   ├─▶ isotonic calibration a score becomes a probability, ECE 0.087 → 0.026
   └─▶ thresholds           accept · grey band · reject
                                   │
                                   └─▶ LLM adjudication, grey band only
```

Every stage is a pure function of its input and a configuration, which is what makes
replay possible at all. Nothing in `matching/` reads the clock, the environment or the
database.

## 4. The seams

Four protocols (`protocols.py`) have two implementations each: one that works from files
and one that works from Postgres. This is the load-bearing structural decision in the
project, and it was made at Stage 0, before there was a database to talk to.

| Protocol | File implementation | Postgres implementation |
|---|---|---|
| `RecordStore` | `ParquetRecordStore` | `PostgresRecordStore` |
| `CandidateGenerator` | `InMemoryCandidateGenerator` | `SqlCandidateGenerator` |
| `ResponseCache` | `FileCache` | `PostgresCache` |
| `StorageBackend` | `LocalStorage` | `S3Storage`, the seam an object store drops into |

Two consequences. The engine's tests need no database, so they are fast and they run on a
laptop with nothing installed. And the first five stages of this project were built and
measured before Postgres existed at all — which was the point of the build order, not a
consequence of it.

## 5. Data flow for a sanction file

Upload is two-phase, because a sanction workbook arrives with whatever headers its
publisher chose:

1. **Inspect.** The file is parsed, its headers read, and a mapping proposed against the
   canonical fields. Nothing is written.
2. **Commit.** The analyst confirms or corrects the mapping, and only then are rows
   ingested, under a source authority and a batch id.

A file is rejected before it is read into memory if it exceeds `MAX_UPLOAD_BYTES`, and
at the first row past `MAX_UPLOAD_ROWS`.

## 6. Identity and access

- Passwords are hashed with argon2id.
- Access tokens are short-lived JWTs; refresh tokens are rotated on use and revoked on
  logout, stored server-side so a logout is a real revocation rather than a client-side
  deletion.
- The refresh cookie is scoped to `/api/auth`, as the browser sees it, so it is sent to
  the auth routes and nothing else.
- CORS names its origins. With credentials allowed, a wildcard would let any page spend a
  logged-in user's token.

Three database roles, which is more than it sounds and is the point:

| Role | Used by | Why it is separate |
|---|---|---|
| owner (`DATABASE_URL`) | migrations only | Owns the tables. An owner can never be revoked from its own table |
| app (`APP_DATABASE_URL`) | api, worker | `SELECT/INSERT/UPDATE/DELETE`, no DDL, and **no `UPDATE` or `DELETE` on `audit_logs`** |
| read-only | the assistant's SQL | `SELECT` on a handful of views and nothing else |

The append-only audit log is only provable because the application does not own its
tables. That is why the app role exists at all.

## 7. The assistant's two defences

A natural-language question becomes SQL, and that SQL is not trusted:

1. **A parser-level guard.** `sqlglot` parses the statement; anything that is not a
   single `SELECT` over the whitelisted views is refused by name — writes, DDL, comments,
   statement separators, schema-qualified escapes into `pg_catalog`, CTEs and subqueries
   that reach outside the list. A `LIMIT` is injected when missing and lowered when too
   high.
2. **A read-only role, under a statement timeout, inside a transaction that is rolled
   back either way.** The guard could have a hole; the role is what means a hole is not
   a breach.

Every question, its SQL, its refusal reason and its row count go to the audit log —
including the refusals, so what was refused is visible too.

## 8. Reproducibility

A run pins four things: a data snapshot hash, a scoring-config version, an engine
version, and a prompt version. Every LLM call is cached by content hash. Replay
re-derives a historical decision exactly; diff answers "what did this config change
actually move, and which field moved it".

This is the difference between an audit trail that is defensible and one that is
decorative. A log saying "we decided X on Tuesday" is worth very little if the system
can no longer show why.

## 9. Technology choices worth the reasoning

| Choice | Instead of | Why |
|---|---|---|
| Postgres job queue | Celery + Redis | The jobs table is transactional with the data the jobs touch. One fewer moving part, and no throughput requirement that needs more |
| Learned weights (EM) | Hand-tuned weights | Agreement on a rare surname is strong evidence and on a common one is nearly none. A tuned weight cannot tell them apart, and the gap widens as the data gets worse |
| Isotonic calibration | Platt scaling | Monotone but not parametric. The posterior's miscalibration here is not a sigmoid's shape |
| Neon (hosted Postgres) | A local install | Nothing to install on the development machine, and the connection-loss handling it forced on the worker is honest resilience the local case would have hidden |
| Native `make up` | `docker compose up` | Docker cannot be installed on the development machine. Writing an unverified compose file would be worse than none: the first thing a reviewer does with one is run it |
| React + Vite | Streamlit | The source plan said both, in different sheets. A review queue with a per-field evidence panel is an application, not a dashboard |
| Parquet for datasets | CSV | Typed columns and a tenth of the size at 50,000 providers, and the engine's tests read them directly |

## 10. What is deliberately not here

- **No container runtime.** Cut on 2026-09-20; CI is what proves host-independence now.
- **No message broker, no cache server, no search index.** Each would be a second source
  of truth for something Postgres already holds.
- **No cloud emulator.** Floci was considered and skipped: it needs Docker.
- **No third-party SaaS.** The only outbound network call in the system is to an LLM API.
