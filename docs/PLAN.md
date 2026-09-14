# Concordance — Build Plan

Glass-box reconciliation of healthcare providers against sanction & exclusion lists.

Status: planning. Nothing implemented yet.

---

## 1. Name

**Recommended: Concordance**

"Concordance" is the actual term of art in record linkage for the degree of agreement
between two records describing the same entity. It says what the system does, it is a
real word so it reads as a product rather than a hackathon acronym, and it is
unclaimed in this space.

Repo/package: `concordance` · service names `concordance-api`, `concordance-web`.

Alternatives, in order:

| Name | Hook | Downside |
|---|---|---|
| **Caliper** | Ties to the calibrated-confidence differentiator | Less domain-specific |
| **Palisade** | Exclusion / barrier imagery, compliance flavour | Says nothing about matching |
| **Lodestar** | "Ground truth you navigate by" | Vague |
| **Attest** | Compliance attestation | Crowded name |

---

## 2. What makes this portfolio-grade

The spec's most important line is buried at the bottom:

> Data from both sources can be up to **90% incorrect**.

That single constraint invalidates the obvious build. A RapidFuzz threshold plus an LLM
prompt is a weekend toy; against 90%-corrupt data it produces confident nonsense and has
no way to know it. Everything distinctive in this project is a direct answer to that line.

Five differentiators, and they compose into one narrative rather than sitting as a
feature list:

1. **Calibrated probabilistic linkage.** Field weights are *learned*, not hand-tuned —
   Fellegi–Sunter linkage with match/non-match probabilities fitted by
   Expectation-Maximization. The output confidence is a genuine posterior probability,
   and we prove it: reliability diagram, Brier score, Expected Calibration Error. When
   the system says 0.90, roughly 90% of those are correct. Hand-tuned weights cannot
   make that claim.

2. **Principled LLM routing.** Because confidence is calibrated, the grey band between
   auto-accept and auto-reject thresholds is defined by *cost*, not vibes. Only pairs
   inside the band reach the LLM. The dashboard reports tokens and dollars saved versus
   an LLM-on-everything baseline, and F1 against that baseline.

3. **Decision replay / audit time-machine.** Every run is pinned to a data snapshot
   hash, a scoring-config version, an engine version and a prompt version; every LLM
   call is cached by content hash. Any historical decision can be re-derived byte-for-byte,
   and any two runs can be diffed: *which* decisions changed when the config changed,
   and why. This is what makes the audit trail defensible rather than decorative.

4. **Corruption dial + robustness curve.** The synthetic generator takes a corruption
   level from 0% to 90%. The evaluation harness sweeps it and plots precision / recall /
   F1 per strategy (deterministic, fuzzy-only, probabilistic, probabilistic+LLM). The
   result is a chart showing exactly where each approach breaks — and that the hybrid
   engine degrades gracefully where the naive one collapses.

5. **Human-in-the-loop that actually loops.** Analyst approvals and rejections are stored
   as labels. A retuning job re-fits the model on them and re-optimizes thresholds for a
   target precision. The dashboard tracks precision/recall across review rounds, so the
   feedback loop is visible rather than asserted.

The pitch line: *most reconciliation demos tell you they are confident. This one can
prove how confident it deserves to be, reproduce any decision it ever made, and show you
the exact data-quality level at which it stops working.*

---

## 3. Decisions locked

| Decision | Choice |
|---|---|
| UI | React 19 + Vite + TypeScript (spec's Overview sheet said Streamlit — overruled, Role-4 sheet's React wins) |
| Differentiators | All five above |
| Cloud emulator (Floci) | **Skipped.** Local filesystem + Postgres. Storage behind a `StorageBackend` interface so an S3 adapter can drop in later |
| Dataset scale | 50,000 providers / 5,000 sanction records |
| "In-house" definition | No external SaaS. OSS pip/npm packages fine. The only outbound network call is the LLM API |
| Auth | In-house: users table, argon2 password hashing, JWT access + refresh, roles `analyst` / `admin` |
| AI Assistant | Yes — constrained NL→SQL with a parser-level guard |
| Budget | Full-time sprint, 1–2 weeks |
| LLM providers | OpenRouter + Groq free tiers in dev, behind an in-house router |
| Build order | **Local-first.** Postgres arrives at Stage 5, Docker at Stage 10. See §4 |
| Deployment | `docker compose up` brings up the whole stack — a Stage 10 deliverable, not a dev dependency |

### Scope warning

Honest estimate for all of the above, solo: **130–170 hours** (§11 added roughly nine).
A 10-working-day full-time sprint is ~80. Section 9 gives a day-by-day plan with an
explicit cut line — everything above the line is a complete, demoable, portfolio-worthy
system; everything below is upside. Do not start below the line.

---

## 4. Build order: local-first, infrastructure last

**Docker and Postgres are deferred.** The first five stages need neither.

This is not a compromise, it is the better order. The matching engine — normalization,
NPI validation, blocking, comparison vectors, the EM fit, calibration, evaluation — is
pure functions over data. None of it needs a database. Running it against Parquet files
in-process means:

- **Zero infrastructure before the interesting work.** The differentiator gets built
  first, not after two days of yaml.
- **The Stage 3 sweep is actually feasible.** Ten corruption levels × four strategies is
  forty full reconciliation passes. In-memory that is minutes; forty round-tripping
  database runs is an afternoon each time you change a weight.
- **Tests stay fast.** Pure functions over fixtures, no container to start, no migration
  to run, no test database to reset.
- **A shippable portfolio artifact exists after Stage 3** — a CLI plus an evaluation
  report with the calibration and robustness charts. Everything after that is the
  application wrapped around an engine that already works.

### The seam that makes it not-throwaway

Two interfaces, defined in Stage 0, keep the file-based phase from becoming rework:

```python
class RecordStore(Protocol):        # providers and sanction records
    def all_providers(self) -> Iterable[Provider]: ...
    def get_provider(self, pid: str) -> Provider | None: ...
    def sanction_batch(self, offset: int, limit: int) -> list[SanctionRecord]: ...

class CandidateGenerator(Protocol):  # blocking
    def candidates(self, rec: SanctionRecord) -> list[Candidate]: ...
```

- `ParquetRecordStore` + `InMemoryCandidateGenerator` (inverted index) — Stages 1–4
- `PostgresRecordStore` + `SqlCandidateGenerator` (indexed queries) — Stage 5 onward

The engine consumes the protocols and never touches storage. Adding Postgres is a new
implementation plus a loader, not a rewrite. Both candidate generators are kept
permanently: the in-memory one stays the fastest way to run a sweep, the SQL one is what
the application uses. Running both against the same fixture and asserting identical
candidate sets is a genuinely useful test.

The LLM response cache gets the same treatment — `FileCache` in Stage 4, `PostgresCache`
in Stage 5, one `ResponseCache` protocol.

### Revised stage order

| Stage | Needs |
|---|---|
| 0 Scaffolding | Python 3.12 only |
| 1 Synthetic data + ground truth | — |
| 2 Normalization, NPI, blocking | — |
| 3 **Probabilistic engine + calibration** ⭐ | — |
| 4 LLM layer | network + API keys |
| 5 Persistence — Postgres, schema, loaders | **Postgres install** |
| 6 Orchestration, runs, replay | Postgres |
| 7 API + auth + workflow | Postgres |
| 8 React application | Postgres |
| — CUT LINE — | |
| 9 Lab, feedback loop, assistant | Postgres |
| 10 Hardening, **Docker packaging**, docs, demo | **Docker install** |

### Prerequisites, by the stage that first needs them

1. **Python 3.12 — before Stage 0.** The system Python here is 3.14.4, too new; several
   wheels in this stack lag major releases. Use a 3.12 venv.
2. **OpenRouter and Groq API keys — before Stage 4.**
3. **Postgres 16 — before Stage 5.** Native Windows install (EDB installer). `pg_trgm`
   ships with it. The design depends on Postgres specifically — trigram GIN indexes,
   JSONB, `SELECT … FOR UPDATE SKIP LOCKED`, `REVOKE` on `audit_logs`, and a read-only
   role for the assistant. SQLite cannot substitute; do not try.
4. **Docker — before Stage 10.** Packaging only. The compose file is a deliverable, not
   a development dependency. Everything runs natively until then.

---

## 5. Architecture

```
                  ┌──────────────────────────────┐
                  │  concordance-web  (nginx)    │
                  │  React 19 + Vite + TS        │
                  └──────────────┬───────────────┘
                                 │ REST + JWT
                  ┌──────────────▼───────────────┐
                  │  concordance-api  (FastAPI)  │
                  │  auth · upload · runs ·      │
                  │  matches · cases · audit     │
                  └──────┬───────────────┬───────┘
                         │               │ enqueue
                         │       ┌───────▼──────────────┐
                         │       │ concordance-worker   │
                         │       │ reconciliation ·     │
                         │       │ eval · retune        │
                         │       └───────┬──────────────┘
                         │               │
                  ┌──────▼───────────────▼───────┐      ┌────────────────┐
                  │  PostgreSQL 16               │      │ LLM router     │
                  │  data · jobs · llm cache ·   │◄─────┤ OpenRouter     │
                  │  audit · run snapshots       │      │ Groq           │
                  └──────────────────────────────┘      └────────────────┘
```

That is the **final** shape, reached at Stage 10. Until Stage 5 there is no database and
no container — the engine runs in-process against Parquet files, driven by the CLI:

```
        data/generated/*.parquet
                 │
        ParquetRecordStore
                 │
        InMemoryCandidateGenerator
                 │
   normalize → compare → EM fit → calibrate → route → [LLM] → eval report
                 │
        reports/*.html + *.json
```

### Deliberate choices

- **No Redis, no Celery.** The job queue is a Postgres table drained with
  `SELECT ... FOR UPDATE SKIP LOCKED`. One fewer service, transactional with the data it
  operates on, and a better interview answer than "I added Celery."
- **The worker is a separate container, same image.** Different entrypoint. Reconciliation
  over 50k×5k must never block an HTTP request.
- **Storage and blocking sit behind protocols** (see §4). File-backed first, Postgres
  later, engine unchanged.
- **LLM calls are cached in Postgres**, keyed by `sha256(model + prompt_version + rendered_prompt)`.
  Free-tier rate limits stop being a problem, re-runs are free, and replay is exact.
- **Storage behind an interface.** `LocalStorage` today; the S3 adapter is ~40 lines if
  Floci or real AWS comes back.

### Repository layout

```
concordance/
├─ docker-compose.yml            # postgres, api, worker, web
├─ docker-compose.dev.yml        # hot reload overrides
├─ .env.example
├─ Makefile                      # up, down, seed, eval, migrate, test, lint
├─ docs/
│  ├─ PLAN.md                    # this file
│  ├─ data_dictionary.md
│  ├─ architecture.md
│  ├─ matching_engine.md         # the F-S maths, written out
│  ├─ scenario_catalogue.md
│  └─ demo_script.md
├─ backend/
│  ├─ pyproject.toml
│  ├─ Dockerfile
│  ├─ alembic/
│  └─ src/concordance/
│     ├─ config.py               # pydantic-settings
│     ├─ db/                     # engine, session, models, repositories
│     ├─ api/                    # routers, deps, schemas, errors
│     ├─ auth/                   # hashing, jwt, rbac deps
│     ├─ matching/
│     │  ├─ normalization.py
│     │  ├─ npi_validator.py     # Luhn with 80840 prefix
│     │  ├─ blocking.py          # candidate generation
│     │  ├─ comparators.py       # per-field agreement levels
│     │  ├─ fellegi_sunter.py    # EM fit + posterior
│     │  ├─ calibration.py       # isotonic, reliability, ECE, Brier
│     │  ├─ scorer.py            # orchestrates one pair
│     │  ├─ ai_matcher.py        # grey-band adjudication
│     │  └─ engine.py            # full pipeline, versioned
│     ├─ llm/
│     │  ├─ router.py            # provider fallback chain
│     │  ├─ providers/           # openrouter.py, groq.py
│     │  ├─ cache.py
│     │  ├─ schema.py            # JSON-schema validate + repair
│     │  └─ prompts/             # versioned .md templates
│     ├─ assistant/              # NL→SQL + SELECT-only guard
│     ├─ jobs/                   # queue, worker loop, handlers
│     ├─ cases/                  # lifecycle service
│     ├─ audit/                  # append-only event writer
│     ├─ eval/                   # metrics, sweeps, reports
│     ├─ synth/                  # generator + corruption engine
│     └─ cli.py                  # typer: seed, reconcile, eval, sweep, replay
├─ frontend/
│  ├─ Dockerfile                 # build → nginx
│  ├─ vite.config.ts
│  └─ src/
│     ├─ api/                    # typed client generated from OpenAPI
│     ├─ components/
│     ├─ pages/                  # Dashboard Providers Sanctions Queue
│     │                          # Investigation Cases Audit Assistant Lab
│     └─ lib/
└─ tests/
   ├─ unit/  integration/  e2e/  fixtures/
```

---

## 6. Data model

Tables, grouped by concern.

**Identity**
- `users` — id, email, password_hash (argon2), full_name, role (`analyst`|`admin`), is_active, timestamps
- `refresh_tokens` — id, user_id, token_hash, expires_at, revoked_at

**Master data**
- `providers` — provider_id, npi, first_name, middle_name, last_name, suffix, dob,
  address_line1, address_line2, city, state, zip, license_number, license_state,
  specialty, organization_name, is_organization, status, created_at
  · plus persisted normalized columns: `name_norm`, `name_phonetic`, `addr_norm`, `zip5`
- `sanction_files` — id, filename, storage_uri, sha256, uploaded_by, row_count, uploaded_at,
  `mapping_id` FK, `status` (`INSPECTED` | `COMMITTED` | `REJECTED`) — two-phase upload, §11.1
- `column_mappings` — id, source_authority, name, `mapping` JSONB (source column →
  canonical field), is_default, created_by, created_at — §11.1
- `sanction_records` — id, file_id, raw payload (JSONB), extracted fields mirroring
  providers, sanction_type, exclusion_date, reinstatement_date, source_authority
  · plus the same normalized columns

**Matching**
- `reconciliation_runs` — id, triggered_by, file_id, status, started_at, finished_at,
  `engine_version`, `scoring_config_id`, `prompt_version`, `provider_snapshot_hash`,
  `sanction_snapshot_hash`, counts by outcome, llm_calls, llm_tokens, llm_cost_usd
- `scoring_configs` — id, version, weights/m-u params (JSONB), thresholds
  (`t_auto_accept`, `t_auto_reject`), fitted_at, fitted_from (`em`|`supervised`|`manual`), notes
- `match_results` — id, run_id, sanction_record_id, decision
  (`MATCH`|`AMBIGUOUS`|`NO_MATCH`), chosen_provider_id, posterior, calibrated_confidence,
  raw_match_weight, route (`deterministic`|`probabilistic`|`llm`), llm_call_id,
  explanation, review_status (`PENDING`|`APPROVED`|`REJECTED`|`ESCALATED`), reviewed_by,
  reviewed_at, reviewer_comment, `superseded_by` (self FK, nullable), `superseded_at` — §11.5
- `match_candidates` — id, match_result_id, provider_id, rank, per-field agreement
  levels and scores (JSONB), match_weight, posterior
- `llm_calls` — id, cache_key, provider, model, prompt_version, request (JSONB),
  response (JSONB), latency_ms, prompt_tokens, completion_tokens, cost_usd, created_at

**Workflow**
- `cases` — id, case_number, provider_id, sanction_record_id, match_result_id, status
  (`ACTIVE`|`EXPIRED`|`CLOSED`|`REJECTED`), start_date, end_date, duration_months
  (default 3), created_by, closed_by, close_reason, `conflict_flag`,
  `conflict_match_result_id` FK nullable — §11.5
- `audit_logs` — id, actor_user_id, actor_role, action, entity_type, entity_id,
  before (JSONB), after (JSONB), request_id, ip, created_at — append-only, revoke UPDATE/DELETE

**Evaluation & learning**
- `ground_truth` — sanction_record_id, expected_provider_id (nullable),
  expected_outcome (`MATCH`|`NO_MATCH`|`AMBIGUOUS`), corruption_profile (JSONB), scenario_tag
- `eval_runs` — id, run_id, corruption_level, strategy, precision, recall, f1,
  false_positives, false_negatives, brier, ece, reliability_bins (JSONB)
- `feedback_events` — id, match_result_id, reviewer_id, label (`TRUE_MATCH`|`FALSE_MATCH`),
  comparison_vector (JSONB), created_at — the training set for retuning

**Jobs**
- `jobs` — id, kind, payload (JSONB), status, attempts, locked_at, locked_by,
  run_after, last_error, created_at

Indexes: `providers(npi)`, `providers(name_norm)`, `providers(state, dob)`,
`providers(name_phonetic, state)`, `providers(license_number, license_state)`,
GIN trigram on `providers(name_norm)`, `match_results(run_id, review_status)`,
`audit_logs(entity_type, entity_id)`, `jobs(status, run_after)`, `llm_calls(cache_key)` unique.

---

## 7. Matching engine design

The core of the project. Written out here because the implementation should follow it exactly.

### 7.1 Normalization
Unicode NFKD fold, case fold, punctuation strip, whitespace collapse. Name: strip
credential suffixes (MD, DO, RN, PA-C, Jr, III), expand a nickname table
(Bob→Robert, Bill→William, …), emit both the ordered and the sorted token form so
name-order swaps do not cost anything. Address: USPS-style abbreviation expansion
(ST→STREET, N→NORTH), unit designators split out, ZIP truncated to 5. Dates parsed
across multiple formats, retained as a triple so partial matches (year+month) are
scoreable. Phonetic key: Double Metaphone on the last name.

### 7.2 NPI validation
An NPI is a 10-digit number with a Luhn check digit computed over the number prefixed
by `80840`. Implementing the real checksum in-house is cheap and catches invalid
identifiers that a length check misses. Classify each NPI as:
`VALID` / `MISSING` / `SENTINEL` (0000000000, 9999999999, 1111111111) /
`PLACEHOLDER_TEXT` (UNKNOWN, N/A, NONE, TBD, "-") / `MALFORMED` (wrong length,
non-numeric) / `CHECKSUM_FAIL`. Only `VALID` may drive a deterministic match.

### 7.3 Blocking (candidate generation)
Brute force is 50k × 5k = 250M pairs. Unacceptable. Union of cheap blocking keys, each
an indexed lookup:
- valid NPI exact
- `(state, dob)`
- `(last_name_phonetic, state)`
- `(zip5, last_name_first_3)`
- `(license_number, license_state)`
- trigram similarity on `name_norm` above a loose floor, capped per record

Union, dedupe, cap at N candidates per sanction record. Target: ≥98% recall of true
pairs at <100 candidates each. **Measure this** — blocking recall is the ceiling on
system recall, and reporting it is a strong signal of knowing what you are doing.

### 7.4 Comparison vectors
Each field yields a discrete agreement level, not a raw float — this is what makes the
EM step tractable:

| Field | Levels |
|---|---|
| last_name | exact / phonetic / jaro-winkler≥.92 / ≥.85 / disagree / missing |
| first_name | exact / nickname-equiv / initial-consistent / jw≥.85 / disagree / missing |
| dob | exact / transposed-parts / year+month / year-only / disagree / missing |
| address | exact-norm / same-street-diff-unit / token-set≥.9 / same-zip-only / disagree / missing |
| state | exact / disagree / missing |
| zip | zip5 exact / zip3 exact / disagree / missing |
| license | exact+state / exact-diff-state / disagree / missing |
| npi | valid-exact / valid-disagree / one-invalid / both-invalid |

Missing is its own level throughout, never silently treated as disagreement — with
90%-corrupt data that distinction drives most of the accuracy.

**That table is the individual vector.** Organizations use a second, separate vector —
legal name, DBA/alias, EIN, type-2 NPI, address, state, zip — with its own EM fit and its
own thresholds. The two must not be merged; see §11.2 for why merging them corrupts both
models. `is_organization` picks the pipeline at the top of `scorer.py`.

### 7.5 Fellegi–Sunter with EM
For each field *i* and agreement level *l*:
- `m_il = P(level = l | pair is a match)`
- `u_il = P(level = l | pair is a non-match)`

Fit both, plus the mixing proportion λ, by Expectation-Maximization over the candidate
pairs — **unsupervised**, no labels needed. Per-pair match weight:

```
w = Σ_i log2( m_i,l(i) / u_i,l(i) )
posterior = 1 / (1 + exp(-(w·ln2 + logit(λ))))
```

Guard rails: Laplace smoothing on level counts, a floor on `u` so a rare-value agreement
cannot produce an infinite weight, fixed random restarts for reproducibility, and a
convergence log persisted with the fitted config.

Why this beats hand-tuned weights, and say so in the README: it learns automatically that
agreeing on a *rare* surname is far stronger evidence than agreeing on a common one, and
it derives field importance from the data rather than from a guess.

### 7.6 Calibration
Split ground truth into fit/holdout. On holdout: reliability diagram (10 bins),
Expected Calibration Error, Brier score. If raw posteriors are miscalibrated, fit an
isotonic regression on the fit split and apply it — then re-measure and show both curves.
Store bins in `eval_runs.reliability_bins` so the UI renders the diagram.

**This chart is the single most portfolio-valuable artifact in the project.** Nearly no
one ships it.

### 7.7 Thresholds and the grey band
With calibrated probabilities, thresholds become a business choice rather than a guess:
choose `t_auto_accept` as the lowest confidence at which precision on holdout still meets
a target (default 0.99), and `t_auto_reject` symmetrically on recall. Everything between
is the grey band and routes to the LLM. Report band width as a percentage of volume —
it is the LLM cost driver, and shrinking it as the model improves is a visible win.

### 7.8 LLM adjudication
Only grey-band pairs, top-K candidates only, and the prompt receives **only normalized
evidence and field scores — never raw free text from the source file**. Prompt rules,
per the spec's Stage 12: resolve identity only; never judge misconduct, guilt or
sanction validity; never introduce a fact not in the supplied evidence; returning
`NO_CONFIDENT_MATCH` or `AMBIGUOUS` is a correct and encouraged answer, not a failure.

Response is a strict JSON schema: `{decision, provider_id|null, confidence,
evidence_cited[], reasoning}`. Validate; on failure, one repair attempt with the
validation error fed back; on second failure, fall back to `AMBIGUOUS` and record the
parse failure. Every `evidence_cited` entry must resolve to a field actually present in
the prompt — **reject the response if it cites evidence that was not supplied.** That
check is a cheap, concrete anti-hallucination guard worth calling out in the README.

Prompts live in versioned files; the version string is written into every run and every
cached call.

### 7.9 Evaluation
`concordance eval` produces: precision, recall, F1, false-positive and false-negative
counts, per-scenario breakdown (exact-NPI, missing-NPI, sentinel-NPI, name-variation,
address-variation, ambiguous, true-negative), a confusion matrix, calibration metrics,
blocking recall, LLM call count / tokens / cost, and mean latency per stage.

`concordance sweep` runs it across corruption levels 0→0.9 × strategies
{deterministic, fuzzy-only, probabilistic, probabilistic+LLM} and writes the robustness
curve the Lab page renders.

---

## 8. Stage plan

Each stage ends in something runnable, committable and demoable. Do not start a stage
before the previous one's acceptance check passes.

Stages 0–4 need no database and no container. Postgres arrives at Stage 5, Docker at
Stage 10. See §4 for why.

### Stage 0 — Local scaffolding · ~4h
Repo scaffolded, git init, Python 3.12 venv. `pyproject.toml`, ruff + mypy + pytest
configured. `pydantic-settings` config and `.env.example`. Structured logging. Typer CLI
skeleton. **The two protocols from §4 — `RecordStore` and `CandidateGenerator` — plus
`ResponseCache` and `StorageBackend`.** Makefile (or `tasks.py`) targets that call the
CLI directly, no containers.
**Accept:** `make test` green on an empty suite; `make lint` and `make typecheck` pass;
`concordance --help` lists the command groups.

*No Docker, no compose, no Alembic, no `/health` — those belong to Stages 5 and 10.*

### Stage 1 — Synthetic data + ground truth · ~10h
Generator producing 50k providers with realistic name/DOB/address/license/specialty
distributions (weighted surname and given-name frequency tables — *rare names must be
rare*, or the EM step learns nothing interesting). Organization providers as well as
individuals, since the spec has both, matched by their own model per §11.2.

Corruption engine, dial 0.0→0.9, independently configurable families: name (order swap,
initial only, nickname, keyboard typo, transliteration, suffix added/dropped, married
name), NPI (missing, sentinel, placeholder text, checksum-fail, digit transposition),
DOB (missing, off-by-one, month/day swap, wrong century), address (abbreviated,
unit dropped, ZIP+4, wrong ZIP, PO box), license (missing, wrong state). Applied to
**both** sides, per the spec's 90%-incorrect limitation.

5k sanction records derived from providers — a deliberate mix of true matches, true
non-matches, and hard ambiguous cases (twins, father/son with shared name at the same
address, common-name collisions in one state). Ground truth written alongside, with the
corruption profile recorded per record. `docs/scenario_catalogue.md` documents each
scenario and what it is designed to break. Excel export with deliberately non-canonical
headers, per §11.1.

Output is **Parquet** under `data/generated/`, read through `ParquetRecordStore`.

**Accept:** `make seed CORRUPTION=0.5` writes the dataset in <60s; every sanction record
has a ground-truth row; identical seed reproduces byte-identical files; scenario
catalogue covers all eight spec scenarios.

### Stage 2 — Normalization, NPI, blocking · ~8h
`normalization.py` (individual and organization paths), `npi_validator.py` (real Luhn),
`blocking.py` with `InMemoryCandidateGenerator` — an inverted index over the blocking
keys, built once per dataset and reused across the whole sweep. Heavy unit tests: these
are pure functions and cheap to test exhaustively, so table-driven tests here carry the
whole suite's credibility.
**Accept:** blocking recall ≥98% at corruption 0.5 with ≤100 candidates/record, measured
and printed by a CLI command; index build over 50k providers in <10s.

### Stage 3 — Probabilistic engine + calibration ⭐ · ~14h
`comparators.py` (two vectors — individual and organization), `fellegi_sunter.py` (EM),
`calibration.py`, `scorer.py`, `eval/`. Two independent EM fits. CLI: `concordance fit`,
`concordance eval`, `concordance sweep`. Fitted configs serialize to JSON on disk —
the same payload that later becomes a `scoring_configs` row.
`docs/matching_engine.md` writes out the maths.
**Accept:** EM converges reproducibly; holdout ECE < 0.05 after isotonic; F1 beats a
fuzzy-only baseline by a clear margin at corruption ≥0.4; full sweep (10 levels × 4
strategies) completes in minutes; evaluation report renders with the reliability diagram
and robustness curve.

*This is the differentiator. If any stage deserves extra hours, it is this one.*
*A portfolio artifact exists at the end of this stage, before any application code.*

### Stage 4 — LLM layer · ~10h
Provider router with a fallback chain (Groq → OpenRouter → configured backups), retries
with jittered backoff, rate-limit handling, per-call token and cost accounting.
`FileCache` implementation of `ResponseCache`, keyed by content hash. JSON-schema
validation with one repair round. Versioned prompts. Evidence-citation guard.
`ai_matcher.py` for grey-band pairs. Capability flag for native structured output,
default off per §11.6.
**Accept:** a grey-band pair gets an LLM decision; second identical run makes zero
network calls; a deliberately malformed model response degrades to `AMBIGUOUS` without
raising; a response citing unsupplied evidence is rejected; the full sweep runs with
`LLM_ENABLED=false`.

### Stage 5 — Persistence · ~10h
**Postgres 16 installed natively.** All tables from §6 as SQLAlchemy 2.0 models. Alembic
initialized and the first migration written. Indexes including the trigram GIN and the
`pg_trgm` extension. `audit_logs` UPDATE/DELETE revoked at the role level. Repository
layer. `PostgresRecordStore`, `SqlCandidateGenerator`, `PostgresCache` — the second
implementations of the Stage 0 protocols. Loader importing the Parquet dataset.
`docs/data_dictionary.md` — every field, type, nullability, valid and sentinel values.
**Accept:** migration up and down clean; autogenerate afterwards produces an empty diff;
loader imports the full dataset; **`SqlCandidateGenerator` and `InMemoryCandidateGenerator`
return identical candidate sets on the same fixture** ⭐; a test proves `UPDATE audit_logs`
is rejected; data dictionary covers every column.

### Stage 6 — Orchestration, runs, replay ⭐ · ~8h
Postgres job queue with `SKIP LOCKED`, worker loop, retry/backoff/dead-letter, stale-lock
recovery, graceful shutdown. `engine.py` end-to-end against the Postgres implementations.
Snapshot hashing. `concordance replay <run_id>` re-derives a run from cache and asserts
identity. `concordance diff <run_a> <run_b>` reports decisions changed, with the config
delta that caused it. Supersede-and-flag on re-run per §11.5. Case expiry job per §11.3.
**Accept:** 5k sanction records reconciled against 50k providers in a sane wall time;
two workers process a queue with no double-execution; replay is decision-identical; diff
between two configs lists changed decisions with reasons; a re-run leaves an active case
untouched but flagged.

### Stage 7 — API + auth + workflow · ~12h
Auth: register/login/refresh/me, argon2, JWT, `require_role` dependency.
Endpoints: two-phase `POST /sanctions/upload` + `/commit` with column mapping per §11.1,
`POST /reconciliation/run` + `GET /reconciliation/runs/{id}`,
`GET /matches` (filter: confidence, status, state, sanction type, date, conflict;
paginated), `GET /matches/{id}` (full candidate detail + evidence),
`POST /matches/{id}/approve|reject|escalate`,
`POST /cases` (3-month default, configurable), `GET /cases`, `GET /cases/{id}`,
`POST /cases/{id}/close`, `GET /audit`, `GET /stats/*`, `/health`.
Audit middleware writing every mutation. Consistent error envelope. Idempotency on
approve (duplicate approval must 409, not double-create a case).
**Accept:** pytest suite covers happy path, invalid Excel, incomplete mapping, duplicate
approval, ambiguous match, RBAC denial, case creation and expiry; OpenAPI schema
generates cleanly.

### Stage 8 — React application · ~16h
Vite + TS + TanStack Query + TanStack Table + Tailwind + Recharts, run with `npm run dev`
against the local API. Typed client generated from OpenAPI. Login, protected routes,
role-aware UI.

- **Dashboard** — KPI tiles; confidence distribution; state distribution; case status;
  reconciliation volume over time.
- **Providers** — directory with server-side filter/sort/paginate; profile drawer with
  compliance history.
- **Sanctions** — records table, source file lineage, two-step upload with the column
  mapping UI and validation errors.
- **Queue** — review queue, all spec filters plus conflicts, saved views, bulk select.
- **Investigation** ⭐ — the centrepiece. Side-by-side sanction vs candidate; per-field
  agreement badges with the score and the *weight contribution* each field made;
  candidate ranking; calibrated confidence with its band position; AI explanation with
  cited evidence highlighted in the record above; recommendation banner
  (APPROVE / REVIEW / REJECT); approve / reject / escalate with comment; create-case
  modal with duration. Organization records render their own field set.
- **Cases** — list, detail, status timeline, audit history, conflict indicator.
- **Audit** — filterable event timeline.
- Status system (`MATCH` `AMBIGUOUS` `UNMATCHED` `PENDING` `APPROVED` `REJECTED`
  `CASE_CREATED`) with one consistent colour and icon vocabulary across every screen.
- Loading skeletons, empty states, error boundaries, toasts. Responsive to ~1280px
  (analyst tool — desktop-first is the right call, but do not let it break).

**Accept:** full workflow driven end-to-end in the browser: upload → map → run → review
→ approve → case created → visible in audit.

---
### ⬆ CUT LINE — everything above is a complete, shippable, portfolio-grade system ⬆
---

### Stage 9 — Lab, feedback loop, assistant · ~14h
- **Lab page** ⭐ — corruption dial, strategy toggles, live robustness curve,
  reliability diagram, LLM cost-versus-baseline panel. The single best screenshot in the
  whole project; put it first in the README.
- **Feedback loop** — `feedback_events` from reviewer decisions; `concordance retune`
  re-fits on labels and re-optimizes thresholds for target precision; new
  `scoring_config` version, never overwriting the old; precision/recall-per-round chart.
- **Run comparison UI** — pick two runs, see changed decisions side by side.
- **AI Assistant** — NL→SQL against a read-only Postgres role restricted to whitelisted
  views. Guard: parse the generated SQL, reject anything that is not a single `SELECT`,
  reject multiple statements, comments, DDL/DML keywords, and any table outside the
  whitelist; enforce `LIMIT`; statement timeout. Show the generated SQL with every
  answer. The guard is the feature — write it up.

### Stage 10 — Packaging, hardening, docs, demo · ~12h
**Docker Desktop installed.** `backend/Dockerfile`, `frontend/Dockerfile` +
`nginx.conf`, `docker-compose.yml` (postgres, api, worker, web) and a dev override.
This is the first time containers appear, and by now the application is known-good
natively, so a container problem is unambiguously a container problem.

Test coverage pass (target ≥80% on `matching/` and `api/`). Security pass: secret scan
over history, dependency audit, SQL injection review, verify the read-only role and the
`audit_logs` revoke. Seed + demo script for the eight scenarios. README with architecture
diagram, the calibration and robustness charts, quick start, and a short "why this is not
just a fuzzy matcher" section. `docs/demo_script.md`. One-command cold start verified on
a clean machine. GitHub Actions running lint + tests.

---

## 9. Ten-day sprint

| Day | Work | Infra needed |
|---|---|---|
| 1 | Stage 0, start Stage 1 | Python 3.12 |
| 2 | Stage 1 (generator + corruption + ground truth) | — |
| 3 | Stage 2 (normalization, NPI, blocking) | — |
| 4 | **Stage 3** — EM + calibration + eval | — |
| 5 | Finish Stage 3 — **portfolio artifact exists here** ⭐ | — |
| 6 | Stage 4 (LLM layer) | API keys |
| 7 | Stage 5 (Postgres, schema, loaders) | **Postgres install** |
| 8 | Stage 6 (orchestration, replay, diff) + Stage 7a (auth, upload) | — |
| 9 | Stage 7b (matches, cases, audit, tests) | — |
| 10 | Stage 8a (shell, dashboard, queue) | — |
| 11 | Stage 8b (**Investigation page**, cases, audit) | — |
| 12 | Stage 10 (Docker, hardening, README, demo) — **ship here** | **Docker install** |
| +1–3 | Stage 9, in order: Lab → feedback loop → assistant | — |

Twelve days, not ten. The original ten assumed Docker and Postgres on day one and no
column-mapping or organization-matching work; §11 added roughly nine hours and the
honest number moved. If the calendar is hard at ten days, ship after Day 10 with the API
complete and the UI partial — the engine, the evaluation report and the CLI already tell
the whole technical story.

If a day slips, the order of sacrifice is: assistant → feedback loop → run-comparison UI
→ Lab page → React polish. Never sacrifice Stage 3 or the Investigation page — they are
the project.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Docker deferred to Stage 10 — containerization problems surface late | Acceptable trade: by then the app is known-good natively, so a container failure is unambiguously a container failure. Keep the Dockerfiles simple and do not let Stage 10 slip off the end |
| Parquet-phase code diverges from the Postgres phase | The §4 protocols are defined in Stage 0, before any consumer exists. The Stage 5 gate asserts both candidate generators return identical sets |
| Postgres install slips past Stage 5 | It is a 5-minute EDB installer and it blocks Stages 5–9. Do it the evening before Day 7 |
| EM converges to a degenerate solution | Smoothing, `u` floor, fixed seeds, sane init from a small labelled slice, convergence logged |
| Free-tier LLM rate limits / flaky JSON | Cache everything, provider fallback chain, schema repair round, `AMBIGUOUS` fallback — pipeline must never hard-fail on the LLM |
| 250M pair explosion | Blocking measured in Stage 3 before anything downstream is built |
| React stage overruns | Build Investigation first inside Stage 8, ugly-but-working; polish last |
| Python 3.14 wheel breakage | Pin 3.12 in the container and in the local venv |
| Scope > available hours | Cut line in §8 is explicit and pre-agreed |

---

## 11. Resolved decisions

All six questions answered. These are now binding.

### 11.1 Sanction source format — **generic + column mapping**

The parser targets no fixed vendor schema. Upload detects the sheet's columns, proposes
a mapping to canonical fields, and the analyst confirms it. Mappings are persisted per
source authority and reused on subsequent uploads of the same source.

- New table `column_mappings` — `id`, `source_authority`, `name`, `mapping` JSONB
  (source column → canonical field), `created_by`, `created_at`, `is_default`
- `sanction_files` gains `mapping_id` FK so every file records how it was interpreted —
  part of the replay guarantee
- Upload is two-phase: `POST /sanctions/upload` inspects and returns detected columns
  plus a proposed mapping; `POST /sanctions/upload/{id}/commit` applies a confirmed
  mapping and ingests
- Canonical field set is fixed and documented in `docs/data_dictionary.md`
- Unmapped source columns are still preserved in `sanction_records.raw`
- The synthetic generator emits deliberately non-canonical headers so the mapping path
  is exercised from day one rather than bolted on when real data lands

### 11.2 Organization providers — **separate matching path**

Organizations and individuals are matched by two different models. Mixing them into one
comparison vector pollutes the missing-level statistics — every organization would
register as `dob: missing`, `first_name: missing`, which teaches the EM fit that those
levels are common and drains their evidential weight for individuals too.

- `is_organization` routes to one of two pipelines at the top of `scorer.py`
- **Individual vector:** as PLAN §7.4
- **Organization vector:** legal name (exact / token-set / acronym-expanded / disagree /
  missing), DBA or alias name, EIN (valid-exact / valid-disagree / one-invalid /
  both-invalid — same shape as NPI), address, state, zip, NPI (organizations have
  type-2 NPIs, which are valid and useful)
- Organization name normalization is its own function: strip and canonicalize corporate
  suffixes (LLC, L.L.C., Inc, Incorporated, Corp, Group, Associates, PA, PC), expand
  known acronyms, handle `&`/`and`
- **Two independent EM fits**, two sets of m/u tables, two threshold pairs, stored as
  separate keys inside one `scoring_configs` row
- Evaluation reports individual and organization metrics separately as well as combined
- Providers whose `is_organization` disagrees across the two sides are a scenario in
  their own right and belong in the scenario catalogue
- Cost: roughly 4 extra hours in Stage 4, plus a normalization function in Stage 3

### 11.3 Case expiry — **scheduled job in the worker**

A daily `expire_cases` job transitions `ACTIVE` cases past `end_date` to `EXPIRED` and
writes one audit row per transition. The expiry is a recorded event with an actor
(`system`) and a timestamp, which is what makes the compliance trail defensible; a
computed-on-read status leaves no evidence that anything happened.

- In-house interval scheduler in the worker loop — no external cron, no APScheduler
- The job is idempotent: running it twice transitions nothing the second time
- Audit rows use `actor_user_id = NULL`, `actor_role = 'system'`
- A startup catch-up run handles cases that expired while the worker was down

### 11.4 Tenancy — **single organization, no isolation**

One provider master, one user pool, no `tenant_id` anywhere and no row-level security.
Multi-tenancy would add a column to every table and a filter to every query for no demo
value, and it competes directly with Stage 4 for hours.

### 11.5 Re-reconciliation — **supersede and keep history**

Nothing is ever deleted or overwritten. A new run supersedes prior results and the link
between them is explicit.

- `match_results` gains `superseded_by` FK (nullable) and `superseded_at`
- A new run over the same sanction record marks the prior `match_results` row superseded
  rather than replacing it
- Queries default to non-superseded rows; history remains reachable
- Existing `ACTIVE` cases are **never** mutated by a re-run — an automated process must
  not silently alter a compliance decision a human approved
- If a re-run reaches a different decision for a sanction record that already has an
  `ACTIVE` case, the case is flagged: new `cases.conflict_flag` and
  `cases.conflict_match_result_id`, surfaced in the Cases UI and the review queue
- Re-uploading a byte-identical file (same `sha256`) is rejected with 409; an updated
  file from the same source is accepted and creates a new run
- This is what makes §2's replay guarantee real — the historical decision still exists
  to be replayed

### 11.6 Production LLM — **assume no native structured output**

The adjudication path stays prompt-based JSON plus the schema-validate-and-repair
pipeline from §7.8. That path works on the Groq and OpenRouter free models used in
development, so it is developed against something testable, and it is portable across
every provider. If the production model turns out to support native structured output,
enabling it is a strict upgrade rather than a rewrite.

- `LLMProvider` declares a `supports_structured_output` capability flag, defaulting false
- The router reads the flag but currently always takes the prompt-based path
- The repair-and-fallback path therefore remains the tested path, not dead code
- Prompt budget is sized for a small free-tier context window; if a larger production
  model is chosen, top-K candidates can be raised without touching the contract
