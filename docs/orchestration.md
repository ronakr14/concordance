# Orchestration — runs, the queue, replay and diff

Stage 6 turns "the engine can score a record" into "the system ran, and can prove what it
did". Four mechanisms, each of which exists because of a specific way the naive version
goes wrong.

Reproduce everything below with:

```
python tasks.py migrate                        # the Stage 6 columns
python tasks.py reconcile                      # one run, in this process
python tasks.py reconcile QUEUE=true           # or enqueue it for a worker
python tasks.py worker                         # claim and run whatever is queued
python -m concordance.cli run list
python -m concordance.cli run replay <run_id>
python -m concordance.cli run diff <run_a> <run_b>
```

---

## 1. The queue

Postgres, not Redis or Celery. The rule for the whole project is in-house, and the queue
is the case where that is also the better engineering choice: the jobs table is in the
same transaction as the work it describes, so a run and the row that says the run happened
cannot disagree.

The claim is one statement:

```sql
SELECT ... FROM jobs
 WHERE status = 'PENDING' AND run_after <= now()
 ORDER BY run_after, id
 LIMIT 1
   FOR UPDATE SKIP LOCKED
```

`SKIP LOCKED` is what lets two workers pull from the same queue without coordinating: each
takes a row the other has not locked and neither blocks. Without it the second worker waits
on the first worker's row lock and the queue serialises — two processes, one worth of
throughput.

**Three transactions per job, deliberately.**

| Boundary | Why it is its own transaction |
|---|---|
| Claim, then commit | A worker killed mid-job leaves a visible `RUNNING` row with a stale lock, rather than silently reverting to `PENDING` with its attempt uncounted. A crash loop that is invisible is a crash loop nobody fixes |
| Run the handler | Everything the handler writes commits together — except `reconcile`, which commits per chunk on purpose (below) |
| Finish or fail | Recording the outcome must not be able to fail along with the work, or a dead job looks pending forever |

**Failure has three states, not two.** A handler that raises leaves the job `PENDING` with
`run_after` pushed out by exponential backoff, until `attempts` reaches `max_attempts`, at
which point the job becomes `DEAD` with `last_error` retained. A job whose `kind` has no
registered handler skips the backoff entirely and dead-letters on the first attempt:
retrying something nothing can run only delays the diagnosis.

**Stale locks are reclaimable.** A `RUNNING` job whose `locked_at` is older than 30 minutes
is returned to the pending pool by whichever worker notices first. That is the recovery
path for a worker that was killed rather than stopped.

**Shutdown is graceful.** On SIGTERM the loop stops claiming but finishes the job in hand,
so a rolling restart never abandons work mid-record.

### The scheduler

There is no cron container and no APScheduler. A scheduled job is a kind, an interval and
the last time it was enqueued; the worker asks what is due on each pass of its loop. Two
properties follow that a cron container does not give you:

- **Catch-up is explicit.** A worker that was down over the weekend enqueues the missed run
  at startup, because the question is "how long since this last ran", not "did the clock
  strike while I was watching". Cases that expired while nothing was running are
  transitioned on the next start.
- **Duplicate suppression is the queue's job.** The scheduler proposes; the worker enqueues
  only when no job of that kind is already pending, so two workers produce one row.

Idempotency still belongs to the handler. `expire_cases` transitions nothing on a second
run the same day, so an extra enqueue costs a query rather than a wrong answer.

---

## 2. The run

`matching/engine.py` holds the pipeline and imports nothing that knows about storage;
`jobs/reconcile.py` is the half that reads Postgres and writes the results back. The seam
is the same one Stage 0 established, and it is why the evaluation harness and a production
run share a scoring path rather than resembling one another.

Three things a straight loop would not do:

- **One bad record fails that record, not the run.** A record the comparators cannot handle
  becomes a `RecordOutcome` carrying an error; the run continues and reports the count.
- **Counts are accumulated live and committed per chunk.** A run whose progress is invisible
  until it ends is not a run anyone can watch, and a failure at record four thousand keeps
  the four thousand decisions it already made, under a run row marked `FAILED` that says
  why.
- **Provenance is written before the first record is scored**: engine version, scoring
  config id, prompt version, the strategy, the request, and both snapshot hashes.

### Supersede and conflict (Q5)

A re-run never edits a previous result. It writes new rows and points the old ones at them
through `superseded_by`, so `superseded_by IS NULL` means "current" and the history stays
reachable. Two consequences that matter more than the mechanism:

- **An active case is never mutated by a re-run.** The case keeps citing the match result it
  was opened on, and that row keeps saying exactly what it said.
- **A re-run that disagrees with an active case flags it.** `conflict_flag` and
  `conflict_match_result_id` are set, an audit row is written with `actor_role = 'system'`,
  and a human decides. Closing the case automatically would let a config change silently
  retract a decision a person made.

---

## 3. Replay

`concordance run replay <run_id>` re-executes a finished run and asserts it reaches the same
decisions. The value is in what it refuses to do:

- **It refuses when the inputs have moved.** Both snapshot hashes are recomputed and
  compared with the ones the run recorded. `--force` shows the divergence anyway, and the
  report says the hashes did not match.
- **It uses the stored versions, not the current ones** — the scoring config the run points
  at, the strategy and blocking cap from the request the run recorded.
- **It never calls a model.** The router runs offline: a cached answer is served and a cache
  miss is an error, so `llm_calls: 0` is a fact rather than a hope. This is why `llm_calls`
  is a cache table and not a log.
- **It writes nothing.** A replay that inserted its own results would supersede the rows it
  is checking.

Drift is reported per record and per field — decision, chosen provider, confidence — with
counts for records the original run never decided and records it decided that the replay did
not reach.

---

## 4. Diff

`concordance run diff <run_a> <run_b>` answers "what changed, and why", in that order:

- **What moved**: unchanged, decision changed, confidence changed beyond a threshold, new,
  removed. Confidence gets a threshold (0.05 by default) because a refit moves every
  posterior slightly, and five thousand "changed" rows hide the twelve that matter.
- **Why**: the scoring config, engine version, prompt version, strategy, request and both
  snapshot hashes, each printed as `before -> after` when they differ. When the configs
  differ, the two thresholds are compared by name, since a threshold move is the most
  common single cause of a decision flip.

`--json` emits the same structure the Stage 9 lab renders.

---

## Measured

50,000 providers, 5,000 sanction records, against a hosted Postgres (Neon, eastus2) over a
home connection — every number here is network-dominated and a local database is far
faster.

| What | Measured |
|---|---|
| Reconcile 500 sanction records against 50,000 providers | **233 s** end to end, of which ~76 s was the two snapshot hashes |
| Provider snapshot hash, 50,000 rows | 40-76 s, in keyset-paged statements of 1,000 rows |
| Sanction snapshot hash, 5,000 rows | 1.4 s |
| Replay of a 40-record run | decision-identical, 40/40, **0 model calls** |
| Queue: 8 jobs, 2 workers in one process | 8 executions, no double-execution, both workers took work |

The 500-record run decided 491 `MATCH`, 9 `AMBIGUOUS`, 0 `NO_MATCH`. The 5,000-record
figure the gate asks for is **not yet recorded**: over this connection the run wedges
partway through (see below), and the honest place to take that number is a local Postgres.

Two things dominate, and neither is the matching:

1. **The snapshot hashes.** Both sides of the data have to cross the wire to be hashed, and
   50,000 provider rows is most of a minute. It is the price of a run that can be replayed
   and proved, and the hash is recomputed rather than cached because a cached provenance
   hash proves nothing.
2. **Round trips.** Blocking answers a chunk in two queries, and everything else is batched
   per chunk. The one place this was originally per record - a flush to get each result's
   primary key - was removed by generating the key client-side.

### The hosted-database caveat

Against Neon over a home connection, large statements in either direction intermittently
wedge the socket: the server finishes and waits for the client, the client waits for a
reply that never arrives, and the run hangs with no error. `py-spy dump` on the stuck
process shows it inside `psycopg`'s `wait_select`, and `pg_stat_activity` shows the server
in `ClientRead`. Three defences are in place, and all three are worth having anyway:

- Snapshot hashing reads in keyset-paged statements (`HASH_PAGE`, 1,000 rows).
- Inserts are capped at `DB_INSERT_PAGE_SIZE` rows per statement (default 50), so no single
  statement carries megabytes.
- `db/retry.py` retries a dropped connection - and a refused reconnect - on a fresh one,
  five attempts with growing backoff, for reads only.

They turn most failures into a retried chunk. They do not cure a socket that wedges with
data in flight, because TCP keepalives do not fire while a segment is unacknowledged. A
local Postgres does not behave this way, which is the configuration Stage 10 ships.

## Operational notes

- **Kill an orphaned backend before re-running.** A client killed mid-query can leave a
  Neon backend in `ClientWrite` holding the row lock its transaction took. The next run
  blocks on `INSERT INTO scoring_configs` until it is terminated:
  `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'active'`.
- **Do not pipe a long run through `tail`.** A pipe the harness stops draining blocks the
  process on stdout, which stops it reading the database socket; the server then sits in
  `ClientWrite` and the run appears hung. Redirect to a file instead.
