# Concordance — Implementation Checklist

Derived from `docs/PLAN.md`. Nothing in the plan is omitted here.

**Rules of use**
- Work top to bottom. Do not start a stage before the previous stage's **GATE** passes.
- Stages 0-4 need **no database and no container**. Postgres arrives at Stage 5, Docker at
  Stage 10. See PLAN §4 for why this order is better, not merely cheaper.
- A gate is not "I think it works" — it is a command you run that prints a result.
- Every `⭐` item is load-bearing for the portfolio story. Never cut one.
- Items tagged `(Q1)`…`(Q6)` trace back to a resolved PLAN §11 decision — read that
  section before implementing one, the reasoning matters more than the item.

Progress: `2 / 11 stages complete` · a portfolio artifact exists from the end of Stage 3.

---

## Traceability — original spec deliverables

The source plan names some deliverables that this build renames or merges. Same work,
different filename. Recorded here so anyone reading
`Provider_Sanctions_Hackathon_Team_Plan.txt` can follow the mapping.

| Spec deliverable | Where it lives here | Note |
|---|---|---|
| `normalization.py` | `matching/normalization.py` | Same, plus an organization path |
| `npi_validator.py` | `matching/npi_validator.py` | Same, plus a real Luhn checksum |
| `candidate_generator.py` | `matching/blocking.py` | Renamed — "blocking" is the record-linkage term. Two implementations behind one protocol |
| `fuzzy_matcher.py` | `matching/comparators.py` | Merged. RapidFuzz similarity is one input to a field's agreement level, not a standalone stage |
| `scorer.py` | `matching/scorer.py` | Same, but weights are EM-learned rather than configured |
| `ai_matcher.py` | `matching/ai_matcher.py` | Same |
| `provider_matches` table | `match_results` + `match_candidates` | Split. One result per sanction record, N ranked candidates beneath it |
| Streamlit dashboard | React 19 + Vite | Overruled — the spec contradicted itself; Role-4 sheet's React wins |
| "Weighted Scoring" (spec stage 6) | `fellegi_sunter.py` | Configurable weights replaced by learned m/u probabilities. Strictly stronger, and the reason the confidence is calibratable |

Everything else in the four role sheets maps one-to-one onto a checklist item below.

---

## Stage -1 — Prerequisites & unblocking

Infrastructure is deferred. Only the first block is needed to start. The rest are listed
here so nothing is forgotten, but each is due immediately before the stage that needs it.

### Needed now — before Stage 0

- [x] Install Python 3.12 alongside system 3.14 (do not uninstall 3.14)
- [x] Verify `py -3.12 --version` prints 3.12.x
- [x] Create local venv on 3.12: `py -3.12 -m venv .venv`
- [x] Install `git`; confirm `git --version`
- [x] Configure `git config --global core.autocrlf input`

### Needed before Stage 4 (LLM layer)

- [ ] Create OpenRouter account; generate API key
- [ ] Create Groq account; generate API key
- [ ] Identify at least two free models on each provider that support JSON output
- [ ] Record per-model context window and rate limits in `docs/llm_providers.md`
- [ ] Confirm neither key is ever written to a tracked file

### Needed before Stage 5 (Persistence) ⭐

- [ ] Install PostgreSQL 16 natively on Windows (EDB installer)
- [ ] Verify `psql --version` resolves
- [ ] Create the `concordance` database and an application role
- [ ] Confirm `CREATE EXTENSION pg_trgm` succeeds — trigram blocking depends on it
- [ ] Record the connection string in `.env` (never committed)

> SQLite is **not** a substitute. The design uses trigram GIN indexes, JSONB,
> `SELECT … FOR UPDATE SKIP LOCKED`, `REVOKE` on `audit_logs`, and a read-only role for
> the assistant. None exist in SQLite. Do not start Stage 5 without Postgres.

### Needed before Stage 8 (React)

- [ ] Install Node 20 LTS, or confirm the existing Node 24 builds Vite cleanly

### Needed before Stage 10 (Packaging)

- [ ] Install Docker Desktop with the WSL2 backend
- [ ] Verify `docker --version` and `docker compose version` resolve
- [ ] Verify `docker run --rm hello-world` succeeds
- [ ] Confirm WSL2 memory allocation is at least 8 GB (`.wslconfig`)

### Open questions from PLAN §11 — **all resolved**

- [x] **Q1** Sanction source format → **generic + column mapping**, two-phase upload, mapping persisted per source
- [x] **Q2** Organization providers → **separate matching path**, two independent EM fits
- [x] **Q3** Case expiry → **scheduled job in worker**, one audit row per transition
- [x] **Q4** Tenancy → **single org**, no `tenant_id`, no RLS
- [x] **Q5** Re-reconciliation → **supersede and keep history**, active cases never mutated, conflicts flagged
- [x] **Q6** Production LLM → **assume no native structured output**, capability flag reserved
- [x] All six written into `docs/PLAN.md` §11 as binding decisions

### GATE -1
- [x] `py -3.12 -c "import sys; print(sys.version)"` prints 3.12
- [x] All six open questions answered in writing (PLAN §11)

> **Scope note.** Q1 and Q2 each added real work beyond the original estimate: the
> two-phase upload with a column-mapping UI is roughly +4h across Stages 7 and 8, and
> the organization matching path is roughly +5h across Stages 1, 2 and 3. Budget ~9
> extra hours — the sprint in PLAN §9 is now twelve days, not ten.

---

## Stage 0 — Local scaffolding · ~4h

No Docker, no Postgres, no Alembic, no FastAPI app. Those arrive at Stages 5, 7 and 10.
This stage builds the package, the CLI and **the protocol seam that keeps Stages 1-4
from becoming rework**.

### Repository

- [x] `git init` in `C:\Projects\provider-reconciliation`
- [x] Create `.gitignore` — `.venv/`, `__pycache__/`, `.env`, `node_modules/`, `dist/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `data/generated/`, `reports/`, `.cache/llm/`
- [x] Create top-level `README.md` stub (filled properly in Stage 10)
- [x] Create `LICENSE` (MIT)
- [x] Create directory skeleton as PLAN §5 specifies, minus `frontend/` and the Dockerfiles
- [x] First commit

### Package scaffolding

- [x] `backend/pyproject.toml` — project metadata, `requires-python = ">=3.12,<3.13"`
- [x] Core deps now: `pydantic`, `pydantic-settings`, `typer`, `structlog`, `rapidfuzz`, `pandas`, `pyarrow`, `numpy`, `scikit-learn` (isotonic only), `openpyxl`
- [x] Deferred deps declared as extras, installed at their stage: `llm` (`httpx`, `jsonschema`), `db` (`sqlalchemy>=2.0`, `alembic`, `psycopg[binary]`), `api` (`fastapi`, `uvicorn[standard]`, `pyjwt`, `argon2-cffi`, `python-multipart`), `assistant` (`sqlglot`)
- [x] Dev deps: `pytest`, `pytest-cov`, `ruff`, `mypy`, `faker`
- [x] Configure `ruff` — line length, rule selection, import sorting
- [x] Configure `mypy` — strict on `matching/` and `llm/`
- [x] Configure `pytest` — testpaths, coverage, markers (`unit`, `integration`, `e2e`, `slow`)
- [x] `backend/src/concordance/__init__.py` with `__version__`
- [x] Editable install into the venv; `import concordance` works

### The protocol seam ⭐ — the most important part of this stage

- [x] `RecordStore` protocol — `all_providers()`, `get_provider(pid)`, `sanction_batch(offset, limit)`, `provider_count()`, `snapshot_hash()` ⭐
- [x] `CandidateGenerator` protocol — `candidates(rec) -> list[Candidate]`, `build(store)` ⭐
- [x] `ResponseCache` protocol — `get(key)`, `put(key, value)`, `stats()` ⭐
- [x] `StorageBackend` protocol — `put(key, bytes) -> uri`, `get(uri)`, `exists(uri)`, `delete(uri)`
- [x] `LocalStorage` implementation writing under `STORAGE_LOCAL_PATH`
- [x] Stub `S3Storage` raising `NotImplementedError` — proves the seam exists
- [x] Domain dataclasses `Provider`, `SanctionRecord`, `Candidate` — plain, storage-agnostic, no ORM ⭐
- [x] **Nothing in `matching/` may import pandas, pyarrow, SQLAlchemy or psycopg** — enforced by an import-linter test ⭐

### Configuration

- [x] `config.py` using `pydantic-settings` — `Settings` class
- [x] Settings now: `LOG_LEVEL`, `ENV`, `DATA_DIR`, `REPORTS_DIR`, `STORAGE_BACKEND`, `STORAGE_LOCAL_PATH`, `MAX_CANDIDATES_PER_RECORD`, `TARGET_PRECISION`, `RANDOM_SEED`
- [x] Settings reserved for later stages, optional until then: `DATABASE_URL`, `JWT_SECRET`, `JWT_ACCESS_TTL`, `JWT_REFRESH_TTL`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `LLM_PROVIDER_CHAIN`, `LLM_MODEL`, `LLM_ENABLED`, `DEFAULT_CASE_MONTHS=3`
- [x] `.env.example` with every setting and a safe placeholder value
- [x] Settings cached via `lru_cache`; never read `os.environ` outside `config.py`
- [x] Fail fast if a required secret is missing in `ENV=production`

### Logging

- [x] `structlog` configured — human-readable in dev, JSON in production
- [x] Correlation-id context var, bound by the CLI per command run
- [x] Progress reporting helper for long CLI operations

### CLI skeleton

- [x] `typer` app with command groups: `data`, `match`, `llm`, `db`, `report`
- [x] `concordance --version`
- [x] Every command takes `--seed` and echoes the resolved config it ran with ⭐
- [x] Stub commands registered for everything the later stages fill in

### Task runner

- [x] `Makefile` (or `tasks.py` if make is awkward on Windows) calling the CLI directly
- [x] `make seed` (accepts `CORRUPTION=`, `PROVIDERS=`, `SEED=`)
- [x] `make fit` / `make eval` / `make sweep`
- [x] `make test` / `make test-unit` / `make cov`
- [x] `make lint` / `make fmt` / `make typecheck`
- [x] Targets that need later infrastructure are declared but exit with a clear "not until Stage N" message ⭐

### GATE 0
- [x] `concordance --help` lists all five command groups
- [x] `make test` runs green on an empty suite
- [x] `make lint` and `make typecheck` both pass
- [x] The import-linter test passes — `matching/` has no storage dependency ⭐
- [x] No Docker, Postgres or network access was required to reach this gate
- [x] `git log` shows the work committed

---

## Stage 1 — Synthetic data + ground truth · ~10h

### Reference data

- [x] Weighted surname frequency table — real long-tail distribution, rare names genuinely rare ⭐
- [x] Weighted given-name frequency table, split by era so DOB and name correlate plausibly
- [x] Nickname → canonical mapping table (Bob→Robert, Bill→William, Peggy→Margaret, …)
- [x] Credential suffix list (MD, DO, DDS, RN, NP, PA-C, PhD, Jr, Sr, II, III)
- [x] US state list with population weights
- [x] City/ZIP reference set per state (a sampled subset is fine; ZIPs must be internally consistent with state)
- [x] Street-name and street-type corpus
- [x] Specialty taxonomy list
- [x] Organization-name generator components (suffixes: LLC, Inc, Group, Associates, Medical Center)
- [x] All reference data committed as data files, not inline literals

### Provider generator

- [x] Generate 50,000 providers, deterministic under a seed ⭐
- [x] Valid NPIs generated with a **correct Luhn check digit** over the `80840` prefix
- [x] NPIs unique across the provider set
- [x] Mix of individual and organization providers (`is_organization`), roughly 15% organizations
- [x] Organizations have no DOB, no first/last name — `organization_name`, DBA/alias, EIN, type-2 NPI (Q2) ⭐
- [x] EINs generated in valid format with a plausible prefix
- [x] Organization names drawn from real-shaped components with corporate suffixes and some acronym forms
- [x] Planted organization near-duplicates: same name different city, acronym vs expanded, DBA vs legal name ⭐
- [x] DOB distribution realistic for practising clinicians
- [x] Addresses internally consistent (city ∈ state, ZIP ∈ state)
- [x] License numbers formatted per state convention, `license_state` usually equal to `state`
- [x] Deliberate near-duplicate clusters planted: twins, father/son same name same address, common-name collisions within one state ⭐
- [x] Seed is recorded in the run output so any dataset is reproducible

### Corruption engine ⭐

- [x] Single dial `corruption_level` in `[0.0, 0.9]`
- [x] Each family independently configurable and independently seeded
- [x] **Name**: token order swap
- [x] **Name**: first name reduced to initial
- [x] **Name**: nickname substitution
- [x] **Name**: keyboard-adjacency typo
- [x] **Name**: transliteration / diacritic loss
- [x] **Name**: credential suffix added or dropped
- [x] **Name**: married-name change on last name
- [x] **Name**: hyphenated name split or joined
- [x] **NPI**: missing / empty string
- [x] **NPI**: sentinel (`0000000000`, `9999999999`, `1111111111`)
- [x] **NPI**: placeholder text (`UNKNOWN`, `N/A`, `NONE`, `TBD`, `-`)
- [x] **NPI**: checksum-failing 10-digit number
- [x] **NPI**: adjacent digit transposition
- [x] **NPI**: wrong length
- [x] **DOB**: missing
- [x] **DOB**: off-by-one day or year
- [x] **DOB**: month/day swap
- [x] **DOB**: wrong century
- [x] **DOB**: alternate string format
- [x] **Address**: USPS abbreviation applied
- [x] **Address**: unit/suite dropped
- [x] **Address**: ZIP+4 instead of ZIP5
- [x] **Address**: wrong ZIP
- [x] **Address**: PO box substituted
- [x] **Address**: whole address missing
- [x] **License**: missing
- [x] **License**: wrong state
- [x] **License**: formatting variation
- [x] Corruption applied to **both** sides — provider master and sanction records — per the spec's limitation line
- [x] Every applied corruption recorded per record into `ground_truth.corruption_profile` ⭐

### Sanction record generator

- [x] Generate 5,000 sanction records
- [x] Deliberate outcome mix: true matches, true non-matches, genuinely ambiguous
- [x] Records covering **all eight spec scenarios**:
  - [x] exact NPI match
  - [x] missing NPI
  - [x] default/sentinel NPI
  - [x] name variation
  - [x] address variation
  - [x] ambiguous (multiple plausible candidates)
  - [x] false positive bait (close but genuinely different person)
  - [x] unmatched (no corresponding provider at all)
- [x] `sanction_type`, `exclusion_date`, `reinstatement_date`, `source_authority` populated plausibly
- [x] Some records with a reinstatement date in the past (no longer excluded) — exercises workflow edge cases
- [x] **Excel headers are deliberately non-canonical** so the column-mapping path is exercised from day one (Q1) ⭐
- [x] At least two distinct header dialects emitted, so mapping reuse per source authority is testable
- [x] Scenario: record whose `is_organization` disagrees across the two sides (Q2) ⭐

### Ground truth

- [x] One `ground_truth` row per sanction record, no exceptions
- [x] `expected_outcome` set, `expected_provider_id` set for `MATCH`
- [x] `scenario_tag` set for per-scenario evaluation breakdown
- [x] Ambiguous records tagged with the full set of plausible provider ids in `corruption_profile`

### Export & CLI

- [x] Sanction Excel export via `openpyxl`, realistic headers, some blank cells
- [x] Excel export includes a few malformed rows for upload-validation testing
- [x] `concordance data seed --providers N --sanctions N --corruption X --seed S`
- [x] `make seed CORRUPTION=0.5` wired to the CLI
- [x] Output written as **Parquet** under `data/generated/` — `providers.parquet`, `sanction_records.parquet`, `ground_truth.parquet` ⭐
- [x] A `manifest.json` beside them recording seed, corruption level, counts, generator version and a content hash ⭐
- [x] Seeding is idempotent — re-running the same seed overwrites with identical content
- [x] `ParquetRecordStore` implementing the Stage 0 `RecordStore` protocol ⭐
- [x] `ParquetRecordStore.snapshot_hash()` returns a stable, order-independent hash — the same contract Postgres will honour later ⭐
- [x] Dataset loads into memory in a few seconds; 50k providers fit comfortably

### Documentation

- [x] `docs/scenario_catalogue.md` — one entry per scenario: what it is, what it is designed to break, how many records, expected outcome
- [x] Document the corruption families and what each simulates in the real world

### GATE 1
- [x] `make seed CORRUPTION=0.5` completes in under 60 seconds
- [x] `providers.parquet` holds 50,000 rows
- [x] `sanction_records.parquet` holds 5,000 rows
- [x] Every sanction record has exactly one ground-truth row (verified programmatically, not by eye)
- [x] Re-running with the same seed produces byte-identical files (compare hashes) ⭐
- [x] `ParquetRecordStore` satisfies the `RecordStore` protocol under mypy ⭐
- [x] `docs/scenario_catalogue.md` covers all eight spec scenarios
- [x] Spot-check 20 corrupted records by hand against their corruption profile

---

## Stage 2 — Normalization, NPI, blocking · ~8h

### `normalization.py`

- [ ] Unicode NFKD fold
- [ ] Case fold
- [ ] Punctuation strip
- [ ] Whitespace collapse
- [ ] Credential suffix stripping
- [ ] Nickname expansion to canonical form
- [ ] Emit ordered token form (`name_norm`)
- [ ] Emit sorted token form (`name_sorted_norm`) so order swaps cost nothing ⭐
- [ ] USPS abbreviation expansion (ST→STREET, N→NORTH, AVE→AVENUE, …)
- [ ] Unit/suite designator split into its own component
- [ ] ZIP truncated to 5 digits
- [ ] Date parsing across multiple formats
- [ ] Dates retained as a `(year, month, day)` triple with nullable parts, enabling partial match ⭐
- [ ] State normalized to two-letter code (handles full names and common misspellings)
- [ ] Double Metaphone implementation for the phonetic key
- [ ] **Organization name normalization** as its own function (Q2) ⭐
- [ ] Corporate suffix canonicalization: LLC, L.L.C., Inc, Incorporated, Corp, Corporation, Group, Associates, PA, PC, LLP
- [ ] `&` ↔ `and` normalized
- [ ] Known acronym expansion for organization names
- [ ] Acronym form derived from a multi-word organization name, for acronym-vs-expanded matching
- [ ] EIN normalization and format validation (Q2)
- [ ] Every function is pure — no DB, no I/O, no globals

### `npi_validator.py`

- [ ] Luhn check digit computed over the `80840` prefix ⭐
- [ ] Classification returns exactly one of: `VALID`, `MISSING`, `SENTINEL`, `PLACEHOLDER_TEXT`, `MALFORMED`, `CHECKSUM_FAIL`
- [ ] Sentinel list configurable, defaults `0000000000`, `9999999999`, `1111111111`
- [ ] Placeholder list configurable, defaults `UNKNOWN`, `N/A`, `NONE`, `TBD`, `-`
- [ ] Leading/trailing whitespace and embedded separators handled before classification
- [ ] Only `VALID` may drive a deterministic match — enforced at the call site, not by convention

### `blocking.py` — `InMemoryCandidateGenerator` ⭐

Implements the Stage 0 `CandidateGenerator` protocol. The SQL implementation comes in
Stage 5; this one is kept permanently because it is what makes the Stage 3 sweep fast.

- [ ] Inverted index built once per dataset, reused across every pass ⭐
- [ ] Block: valid NPI exact
- [ ] Block: `(state, dob)`
- [ ] Block: `(last_name_phonetic, state)`
- [ ] Block: `(zip5, last_name_first_3)`
- [ ] Block: `(license_number, license_state)`
- [ ] Block: trigram similarity on `name_norm` above a loose floor, capped per record
- [ ] In-house character-trigram index for the fuzzy block — no `pg_trgm` available here ⭐
- [ ] Organization blocks: EIN exact, `(legal_name_token, state)`, acronym key (Q2) ⭐
- [ ] Union and dedupe candidates across all blocks
- [ ] Cap at `MAX_CANDIDATES_PER_RECORD`
- [ ] Record which block(s) produced each candidate — needed for debugging recall loss ⭐
- [ ] Index build over 50k providers in <10s, memory footprint measured and recorded
- [ ] Deterministic candidate ordering, so downstream results are reproducible ⭐

### Blocking recall measurement ⭐

- [ ] `concordance blocking-recall --corruption X` CLI command
- [ ] Reports: recall, mean candidates per record, p95 candidates, per-block contribution
- [ ] Reports which true pairs were **missed** and by which corruption family — this drives block tuning

### Tests

- [ ] Table-driven unit tests for every normalization function
- [ ] Unit tests for all six NPI classifications, including known-valid and known-invalid real-format NPIs
- [ ] Luhn implementation verified against hand-computed examples
- [ ] Nickname expansion round-trip tests
- [ ] Date parsing tests across every format the generator emits
- [ ] Phonetic key tests for known homophone pairs
- [ ] Blocking tests on a small fixture where the correct candidate set is known exactly

### GATE 2
- [ ] `concordance match blocking-recall --corruption 0.5` reports **≥98% recall** at **≤100 candidates/record** ⭐
- [ ] Same command at corruption 0.9 reported and recorded (may be lower — record the number)
- [ ] Index build over 50k providers completes in <10s ⭐
- [ ] Candidate sets are identical across repeated runs with the same seed ⭐
- [ ] Unit test suite for this stage passes with ≥90% coverage on the three modules
- [ ] `InMemoryCandidateGenerator` satisfies the `CandidateGenerator` protocol under mypy

---

## Stage 3 — Probabilistic engine + calibration ⭐ · ~14h

*This is the project. If a stage gets extra hours, it is this one.*

### `comparators.py`

- [ ] `last_name`: exact / phonetic / JW≥.92 / JW≥.85 / disagree / missing
- [ ] `first_name`: exact / nickname-equiv / initial-consistent / JW≥.85 / disagree / missing
- [ ] `dob`: exact / transposed-parts / year+month / year-only / disagree / missing
- [ ] `address`: exact-norm / same-street-diff-unit / token-set≥.9 / same-zip-only / disagree / missing
- [ ] `state`: exact / disagree / missing
- [ ] `zip`: zip5-exact / zip3-exact / disagree / missing
- [ ] `license`: exact+state / exact-diff-state / disagree / missing
- [ ] `npi`: valid-exact / valid-disagree / one-invalid / both-invalid
- [ ] **`missing` is always its own level, never folded into `disagree`** ⭐
- [ ] Levels are ordinal enums with stable integer codes — the EM tables index on them
- [ ] Comparison vector assembly returns a fixed-length tuple, same order every time
- [ ] Unit test per field covering every level

#### Organization comparison vector ⭐ (Q2 — separate model)

- [ ] `legal_name`: exact / token-set≥.9 / acronym-expanded-equiv / JW≥.85 / disagree / missing
- [ ] `dba_alias`: exact / token-set≥.9 / disagree / missing
- [ ] `ein`: valid-exact / valid-disagree / one-invalid / both-invalid
- [ ] `npi` (type-2): valid-exact / valid-disagree / one-invalid / both-invalid
- [ ] `address`, `state`, `zip`: same levels as the individual vector
- [ ] `is_organization` routes to one of the two pipelines at the top of `scorer.py` ⭐
- [ ] Cross-type pairs (org vs individual) handled explicitly, not silently scored
- [ ] Unit test per organization field covering every level

### `fellegi_sunter.py` ⭐

- [ ] Data structures for `m[field][level]`, `u[field][level]`, `lambda`
- [ ] E-step: posterior responsibility per candidate pair
- [ ] M-step: re-estimate m, u, λ from weighted level counts
- [ ] Laplace smoothing on level counts
- [ ] Floor on `u` so a rare-value agreement cannot produce an infinite weight ⭐
- [ ] Convergence criterion on log-likelihood delta, plus a max-iteration cap
- [ ] Fixed random restarts, deterministic under a seed ⭐
- [ ] Sensible initialization (optionally warm-started from a small labelled slice)
- [ ] Convergence log (iteration, log-likelihood, λ) persisted with the fitted config
- [ ] Match weight: `w = Σ log2(m / u)` over the observed levels
- [ ] Posterior: `1 / (1 + exp(-(w·ln2 + logit(λ))))`
- [ ] Per-field weight contribution returned alongside the total ⭐ — the Investigation UI needs it
- [ ] Guard against degenerate solutions: detect λ collapsing to 0 or 1 and fail loudly
- [ ] Fitted parameters serialize to and from `scoring_configs.params` JSONB losslessly
- [ ] **Two independent EM fits — individual and organization** — stored as separate keys in one config row (Q2) ⭐
- [ ] Separate threshold pair per model; neither model's statistics contaminate the other ⭐
- [ ] Organization fit guarded for small-sample instability (fewer orgs than individuals)

### `calibration.py` ⭐

- [ ] Deterministic fit/holdout split of ground truth, seeded
- [ ] Reliability diagram over 10 bins — bin edges, count, mean predicted, observed frequency
- [ ] Expected Calibration Error
- [ ] Brier score
- [ ] Isotonic regression calibrator fitted on the fit split
- [ ] Calibrator serialized into `scoring_configs.calibrator`
- [ ] Metrics computed **before and after** calibration, both retained for the UI ⭐
- [ ] Calibrator application is a pure function of the stored parameters — no refit at inference

### Threshold selection

- [ ] `t_auto_accept` = lowest confidence where holdout precision ≥ `TARGET_PRECISION` (default 0.99)
- [ ] `t_auto_reject` = symmetric selection on recall
- [ ] Grey-band width reported as a percentage of volume ⭐
- [ ] Thresholds written into the `scoring_configs` row, never hardcoded

### `scorer.py`

- [ ] Deterministic path: valid NPI exact match short-circuits to `MATCH`
- [ ] Deterministic path flags conflicting attributes for review rather than silently accepting ⭐ (source spec, Role 2 stage 3)
- [ ] Probabilistic path: compare vector → weight → posterior → calibrate
- [ ] Candidate ranking by calibrated confidence, top-K retained
- [ ] Routing decision: above accept → `MATCH`; below reject → `NO_MATCH`; between → grey band
- [ ] Margin check: if top two candidates are within a configurable delta, force `AMBIGUOUS` regardless of absolute confidence ⭐
- [ ] Returns a structured result carrying decision, confidence, route, per-candidate field levels and weights

### `eval/` harness

- [ ] Precision, recall, F1
- [ ] False-positive and false-negative counts
- [ ] Confusion matrix across `MATCH` / `AMBIGUOUS` / `NO_MATCH`
- [ ] Per-scenario breakdown using `ground_truth.scenario_tag` (all eight scenarios)
- [ ] **Individual and organization metrics reported separately as well as combined** (Q2) ⭐
- [ ] Calibration metrics (ECE, Brier, reliability bins)
- [ ] Blocking recall carried through into the report
- [ ] LLM call count, tokens, cost, when the strategy includes the LLM
- [ ] Mean latency per pipeline stage
- [ ] Ambiguous-handling metric: how often `AMBIGUOUS` was the *correct* answer
- [ ] Results written as JSON under `reports/` — the same payload shape that becomes an `eval_runs` row in Stage 5 ⭐
- [ ] Self-contained HTML report with the reliability diagram and robustness curve rendered as inline SVG ⭐
- [ ] Report is the Stage 3 portfolio artifact — readable standalone, no server needed ⭐

### CLI

- [ ] `concordance match fit --corruption X --seed S` → writes a versioned config **JSON file** under `data/configs/` ⭐
- [ ] Config JSON is exactly the payload that becomes a `scoring_configs` row in Stage 5 — no reshaping later ⭐
- [ ] `concordance eval --config-id N --strategy S`
- [ ] `concordance sweep` — corruption `0.0 → 0.9` × strategies `{deterministic, fuzzy, probabilistic, probabilistic_llm}` ⭐
- [ ] Sweep writes one `eval_runs` row per cell
- [ ] Fuzzy-only baseline strategy implemented for comparison ⭐
- [ ] Deterministic-only baseline strategy implemented for comparison

### Documentation

- [ ] `docs/matching_engine.md` — the full Fellegi–Sunter derivation, the level tables, the guard rails, and why learned weights beat hand-tuned ones ⭐
- [ ] `docs/matching_engine.md` explains **why individuals and organizations get separate fits** — the missing-level contamination argument (Q2) ⭐

### GATE 3
- [ ] EM converges on repeated runs with the same seed to identical parameters ⭐
- [ ] Holdout **ECE < 0.05** after isotonic calibration ⭐
- [ ] Both models fitted; individual and organization metrics reported separately ⭐
- [ ] Probabilistic F1 beats fuzzy-only F1 by a clear margin at corruption ≥ 0.4 ⭐
- [ ] Reliability diagram data renders correctly (verify the numbers, chart comes in Stage 9)
- [ ] `concordance match sweep` completes every cell (10 levels × 4 strategies) **in minutes, not hours** ⭐
- [ ] Standalone HTML evaluation report opens in a browser with both charts rendered ⭐
- [ ] Per-scenario breakdown shows a sane result for all eight scenarios
- [ ] `docs/matching_engine.md` written and accurate

---

## Stage 4 — LLM layer · ~10h

### Provider abstraction

- [ ] `LLMProvider` protocol — `complete(messages, schema, **opts) -> LLMResponse`
- [ ] `openrouter.py` implementation
- [ ] `groq.py` implementation
- [ ] Normalized `LLMResponse`: content, model, prompt_tokens, completion_tokens, latency_ms, raw
- [ ] Per-provider error taxonomy mapped to shared exceptions: `RateLimited`, `Transient`, `InvalidRequest`, `AuthFailed`
- [ ] Timeouts on every call, configurable
- [ ] `supports_structured_output` capability flag on each provider, **default false** (Q6) ⭐
- [ ] Router reads the flag but always takes the prompt-based JSON path for now — so the repair path stays the tested path, not dead code ⭐
- [ ] Prompt budget sized for a small free-tier context window; top-K is configurable so a larger production model needs no contract change

### `router.py`

- [ ] Fallback chain driven by `LLM_PROVIDER_CHAIN` setting (default Groq → OpenRouter)
- [ ] Retries with jittered exponential backoff on `Transient` and `RateLimited`
- [ ] Move to the next provider after exhausting retries, not on first error
- [ ] Per-call token accounting
- [ ] Per-call cost accounting from a configurable price table
- [ ] `LLM_ENABLED=false` short-circuits to `AMBIGUOUS` — full pipeline must run with no LLM at all ⭐
- [ ] All calls logged with request_id, provider, model, latency, tokens
- [ ] Never log API keys; never log raw provider error bodies containing keys

### `cache.py` — `FileCache`

Implements the Stage 0 `ResponseCache` protocol. `PostgresCache` arrives in Stage 5;
this one stays as the cache the CLI and the sweep use.

- [ ] Cache key = `sha256(provider + model + prompt_version + rendered_prompt)` ⭐
- [ ] `FileCache` storing one JSON file per key under `.cache/llm/`, sharded by key prefix ⭐
- [ ] Read-through: hit returns the stored response without a network call
- [ ] Write on success only; failures are not cached
- [ ] Cache hit/miss counters surfaced in the run summary
- [ ] Stored entry carries everything a `llm_calls` row needs — provider, model, prompt version, request, response, latency, tokens, cost — so Stage 5 migration is a straight import ⭐
- [ ] `concordance llm cache-stats` and `concordance llm cache-clear`

### `schema.py`

- [ ] JSON Schema for the adjudication response: `{decision, provider_id|null, confidence, evidence_cited[], reasoning}`
- [ ] `decision` restricted to `MATCH` | `NO_CONFIDENT_MATCH` | `AMBIGUOUS`
- [ ] Extract JSON from a response that wraps it in prose or code fences
- [ ] Validate against the schema
- [ ] On validation failure: **one** repair attempt, feeding the validation error back
- [ ] On second failure: fall back to `AMBIGUOUS`, record the parse failure, never raise ⭐
- [ ] `provider_id` must be one of the candidate ids supplied — reject otherwise
- [ ] **Every `evidence_cited` entry must resolve to a field actually present in the prompt; reject the response if it cites unsupplied evidence** ⭐
- [ ] Confidence coerced to `[0,1]`; out-of-range downgrades to `AMBIGUOUS`

### Prompts

- [ ] Prompts live in versioned files under `llm/prompts/`
- [ ] `PROMPT_VERSION` constant written into every run and every cache key ⭐
- [ ] System prompt states: resolve identity only ⭐
- [ ] System prompt states: never judge misconduct, guilt, or sanction validity ⭐
- [ ] System prompt states: never introduce a fact not in the supplied evidence ⭐
- [ ] System prompt states: `NO_CONFIDENT_MATCH` / `AMBIGUOUS` is a correct answer, not a failure ⭐
- [ ] Prompt receives **only normalized evidence and field scores — never raw free text from the source file** ⭐
- [ ] Prompt includes top-K candidates only
- [ ] Prompt includes per-field agreement levels and weight contributions
- [ ] Few-shot examples included, one of which correctly answers `AMBIGUOUS`
- [ ] Prompt injection surface reviewed: names and addresses are data, wrapped and delimited, never interpolated as instructions ⭐

### `ai_matcher.py`

- [ ] Invoked only for grey-band pairs
- [ ] Builds the evidence payload from `match_candidates`, not from raw records
- [ ] Writes the cache entry and carries its key on the result (becomes `match_results.llm_call_id` in Stage 5)
- [ ] Maps the LLM decision onto the engine's `MATCH` / `AMBIGUOUS` / `NO_MATCH` vocabulary
- [ ] Carries the reasoning and `evidence_cited` on the result for later persistence and UI highlighting

### Tests

- [ ] Provider clients tested against recorded fixtures, not the live API
- [ ] Cache hit path asserts zero HTTP calls
- [ ] Malformed JSON → repair → success path
- [ ] Malformed JSON → repair → still malformed → `AMBIGUOUS`, no exception
- [ ] Response citing unsupplied evidence is rejected
- [ ] Response naming a provider_id outside the candidate set is rejected
- [ ] Rate-limit response triggers backoff then provider failover
- [ ] `LLM_ENABLED=false` runs the whole pipeline

### Documentation

- [ ] `docs/llm_providers.md` — providers, models, limits, price table, chain configuration

### GATE 4
- [ ] A grey-band pair receives a real LLM decision end to end
- [ ] Re-running the identical pair makes **zero** network calls (cache verified by counter) ⭐
- [ ] Deliberately malformed model output degrades to `AMBIGUOUS` without raising ⭐
- [ ] A response citing unsupplied evidence is rejected by the guard ⭐
- [ ] Provider failover demonstrated (kill the first provider's key and observe the chain)
- [ ] Full pipeline runs with `LLM_ENABLED=false`

---

## Stage 5 — Persistence · ~10h

**Postgres 16 must be installed before starting.** See Stage -1.

This stage adds the second implementation of every Stage 0 protocol. The engine built in
Stages 1–4 does not change; if it does, the seam was wrong and that is the real bug.

### Postgres setup

- [ ] `pip install -e .[db]` — SQLAlchemy, Alembic, psycopg now enter the project
- [ ] `concordance` database created; application role with least privilege
- [ ] `CREATE EXTENSION IF NOT EXISTS pg_trgm` in the first migration, before the GIN index ⭐
- [ ] `DATABASE_URL` in `.env`, never committed

### Alembic

- [ ] `alembic init` inside `backend/`
- [ ] Point `env.py` at `Settings.DATABASE_URL`
- [ ] Point `target_metadata` at the SQLAlchemy `Base`
- [ ] Enable `compare_type` and `compare_server_default` in `env.py`

### Base

- [ ] `db/base.py` — declarative `Base`, naming convention for constraints and indexes
- [ ] `db/session.py` — engine, `sessionmaker`, `get_session` dependency, context manager for the worker
- [ ] Shared mixins: `TimestampMixin` (`created_at`, `updated_at`), UUID primary key default
- [ ] Enum types defined once in Python and mapped to native Postgres enums or constrained varchars — pick one and be consistent

### Identity tables

- [ ] `users` — `id`, `email` (unique, citext or lower-indexed), `password_hash`, `full_name`, `role`, `is_active`, `created_at`, `updated_at`
- [ ] `role` constrained to `analyst` | `admin`
- [ ] `refresh_tokens` — `id`, `user_id` FK, `token_hash`, `expires_at`, `revoked_at`, `created_at`
- [ ] Index `refresh_tokens(user_id)`, index `refresh_tokens(token_hash)` unique

### Master data tables

- [ ] `providers` — `provider_id` (business key, unique), `npi`, `first_name`, `middle_name`, `last_name`, `suffix`, `dob`, `address_line1`, `address_line2`, `city`, `state`, `zip`, `license_number`, `license_state`, `specialty`, `organization_name`, `is_organization`, `status`, `created_at`
- [ ] `providers` normalized columns persisted: `name_norm`, `name_sorted_norm`, `name_phonetic`, `addr_norm`, `zip5`
- [ ] `sanction_files` — `id`, `filename`, `storage_uri`, `sha256` (unique), `uploaded_by` FK, `row_count`, `uploaded_at`, `mapping_id` FK, `status`
- [ ] `sanction_files.status` constrained to `INSPECTED` | `COMMITTED` | `REJECTED` — two-phase upload (Q1) ⭐
- [ ] `column_mappings` — `id`, `source_authority`, `name`, `mapping` JSONB, `is_default`, `created_by` FK, `created_at` (Q1) ⭐
- [ ] Unique index `column_mappings(source_authority, name)`
- [ ] Canonical field set defined once in code and documented — the mapping target vocabulary
- [ ] `sanction_records` — `id`, `file_id` FK, `raw` JSONB, extracted fields mirroring `providers`, `sanction_type`, `exclusion_date`, `reinstatement_date`, `source_authority`
- [ ] `sanction_records` normalized columns: same five as `providers`
- [ ] `raw` JSONB preserves the original row verbatim — required for audit defensibility

### Matching tables

- [ ] `reconciliation_runs` — `id`, `triggered_by` FK, `file_id` FK, `status`, `started_at`, `finished_at`, `engine_version`, `scoring_config_id` FK, `prompt_version`, `provider_snapshot_hash`, `sanction_snapshot_hash`, counts by outcome, `llm_calls`, `llm_tokens`, `llm_cost_usd`
- [ ] `status` constrained to `QUEUED` | `RUNNING` | `COMPLETED` | `FAILED` | `CANCELLED`
- [ ] `scoring_configs` — `id`, `version`, `params` JSONB (m/u tables, λ), `t_auto_accept`, `t_auto_reject`, `calibrator` JSONB, `fitted_at`, `fitted_from`, `notes`
- [ ] `fitted_from` constrained to `em` | `supervised` | `manual`
- [ ] `scoring_configs.version` unique; rows are immutable once written
- [ ] `match_results` — `id`, `run_id` FK, `sanction_record_id` FK, `decision`, `chosen_provider_id` FK nullable, `posterior`, `calibrated_confidence`, `raw_match_weight`, `route`, `llm_call_id` FK nullable, `explanation`, `review_status`, `reviewed_by` FK nullable, `reviewed_at`, `reviewer_comment`
- [ ] `decision` constrained to `MATCH` | `AMBIGUOUS` | `NO_MATCH`
- [ ] `route` constrained to `deterministic` | `probabilistic` | `llm`
- [ ] `review_status` constrained to `PENDING` | `APPROVED` | `REJECTED` | `ESCALATED`
- [ ] Unique constraint on `match_results(run_id, sanction_record_id)`
- [ ] `match_results.superseded_by` FK (self-referential, nullable) and `superseded_at` (Q5) ⭐
- [ ] Partial index on `match_results` where `superseded_by IS NULL` — the default query path
- [ ] Repository list methods exclude superseded rows unless explicitly asked for history ⭐
- [ ] `match_candidates` — `id`, `match_result_id` FK, `provider_id` FK, `rank`, `field_levels` JSONB, `field_weights` JSONB, `match_weight`, `posterior`
- [ ] `field_weights` stores each field's contribution to the total — the Investigation UI renders it directly
- [ ] `llm_calls` — `id`, `cache_key` (unique), `provider`, `model`, `prompt_version`, `request` JSONB, `response` JSONB, `latency_ms`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `created_at`

### Workflow tables

- [ ] `cases` — `id`, `case_number` (unique, human-readable), `provider_id` FK, `sanction_record_id` FK, `match_result_id` FK, `status`, `start_date`, `end_date`, `duration_months` default 3, `created_by` FK, `closed_by` FK nullable, `close_reason`
- [ ] `status` constrained to `ACTIVE` | `EXPIRED` | `CLOSED` | `REJECTED`
- [ ] Unique partial index preventing two `ACTIVE` cases for the same `(provider_id, sanction_record_id)`
- [ ] `cases.conflict_flag` boolean and `cases.conflict_match_result_id` FK nullable (Q5) ⭐
- [ ] Index `cases(conflict_flag)` where true — drives the conflict surface in the queue
- [ ] `audit_logs` — `id`, `actor_user_id` FK nullable, `actor_role`, `action`, `entity_type`, `entity_id`, `before` JSONB, `after` JSONB, `request_id`, `ip`, `created_at`
- [ ] Migration revokes `UPDATE` and `DELETE` on `audit_logs` from the application role ⭐
- [ ] Verify the revoke actually bites — write a test that attempts an update and expects a permission error

### Evaluation & learning tables

- [ ] `ground_truth` — `sanction_record_id` FK, `expected_provider_id` FK nullable, `expected_outcome`, `corruption_profile` JSONB, `scenario_tag`
- [ ] `expected_outcome` constrained to `MATCH` | `NO_MATCH` | `AMBIGUOUS`
- [ ] `eval_runs` — `id`, `run_id` FK nullable, `corruption_level`, `strategy`, `precision`, `recall`, `f1`, `false_positives`, `false_negatives`, `brier`, `ece`, `reliability_bins` JSONB, `blocking_recall`, `created_at`
- [ ] `strategy` constrained to `deterministic` | `fuzzy` | `probabilistic` | `probabilistic_llm`
- [ ] `feedback_events` — `id`, `match_result_id` FK, `reviewer_id` FK, `label`, `comparison_vector` JSONB, `created_at`
- [ ] `label` constrained to `TRUE_MATCH` | `FALSE_MATCH`

### Jobs table

- [ ] `jobs` — `id`, `kind`, `payload` JSONB, `status`, `attempts`, `max_attempts`, `locked_at`, `locked_by`, `run_after`, `last_error`, `created_at`, `updated_at`
- [ ] `status` constrained to `PENDING` | `RUNNING` | `DONE` | `FAILED` | `DEAD`

### Indexes

- [ ] `providers(npi)`
- [ ] `providers(name_norm)`
- [ ] `providers(state, dob)`
- [ ] `providers(name_phonetic, state)`
- [ ] `providers(license_number, license_state)`
- [ ] GIN trigram index on `providers(name_norm)` ⭐
- [ ] `providers(zip5, last_name)` supporting the zip block
- [ ] Mirror the equivalent indexes on `sanction_records`
- [ ] `match_results(run_id, review_status)`
- [ ] `match_results(sanction_record_id)`
- [ ] `match_candidates(match_result_id, rank)`
- [ ] `audit_logs(entity_type, entity_id)`
- [ ] `audit_logs(created_at DESC)`
- [ ] `jobs(status, run_after)`
- [ ] `llm_calls(cache_key)` unique
- [ ] `cases(status, end_date)` supporting the expiry job

### Repository layer

- [ ] One repository module per aggregate: providers, sanctions, matches, cases, audit, users, jobs, eval
- [ ] Repositories accept a `Session`; they never open their own
- [ ] No raw SQL outside repositories and the assistant module
- [ ] Pagination helper shared across list repositories (limit/offset + total count)

### Second implementations of the Stage 0 protocols ⭐

- [ ] `PostgresRecordStore` implementing `RecordStore` ⭐
- [ ] `PostgresRecordStore.snapshot_hash()` produces the **same hash** as `ParquetRecordStore` for the same data ⭐
- [ ] `SqlCandidateGenerator` implementing `CandidateGenerator` — each block a single indexed query ⭐
- [ ] `EXPLAIN` confirms every block uses an index; no sequential scans
- [ ] Blocking batched — no 5,000 round trips
- [ ] `PostgresCache` implementing `ResponseCache`, backed by `llm_calls` ⭐
- [ ] Migration importing the existing `FileCache` entries into `llm_calls` — Stage 4's work is not thrown away ⭐
- [ ] **Nothing in `matching/` changed to make this work** — verify by diff ⭐

### Loader

- [ ] `concordance db load --from data/generated/` imports the Parquet dataset ⭐
- [ ] Bulk insert via `COPY`, not row-by-row ORM inserts
- [ ] Normalized columns populated at load time using the Stage 2 functions — one implementation, not two ⭐
- [ ] Ground truth loaded alongside
- [ ] Load of 50k providers + 5k sanctions completes in a sane time — record it
- [ ] `concordance db reset` drops and recreates, prompting for confirmation

### Documentation

- [ ] `docs/data_dictionary.md` covering **every column**: name, type, nullable, default, meaning
- [ ] Document all valid NPI sentinel values and placeholder strings
- [ ] Document every enum and its allowed values
- [ ] Document which columns are derived/normalized and by which function

### GATE 5
- [ ] `alembic upgrade head` runs clean on an empty database
- [ ] `alembic downgrade base` runs clean with no orphaned objects
- [ ] `alembic revision --autogenerate` afterwards produces an empty diff
- [ ] Loader imports the full Parquet dataset without error
- [ ] **`SqlCandidateGenerator` and `InMemoryCandidateGenerator` return identical candidate sets on the same fixture** ⭐
- [ ] `PostgresRecordStore.snapshot_hash()` equals `ParquetRecordStore.snapshot_hash()` on the same data ⭐
- [ ] An evaluation run against Postgres reproduces the Stage 3 metrics exactly ⭐
- [ ] `git diff` shows no changes to `matching/` in this stage ⭐
- [ ] Test proving `UPDATE audit_logs` is rejected for the app role
- [ ] `docs/data_dictionary.md` covers every column in every table — verified by reading the migration alongside it

---

## Stage 6 — Orchestration, runs, replay ⭐ · ~8h

### Job queue

- [ ] Enqueue helper writing to `jobs`
- [ ] Dequeue with `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` ⭐
- [ ] Worker loop: claim, execute, mark done or failed, release
- [ ] `attempts` increment, `max_attempts` cap
- [ ] Exponential backoff via `run_after`
- [ ] Dead-letter state (`DEAD`) after `max_attempts`, with `last_error` retained
- [ ] Stale lock recovery — a job `RUNNING` past a timeout is reclaimable ⭐
- [ ] Graceful shutdown on SIGTERM: finish the current job, do not claim another
- [ ] Handlers registered by `kind`: `reconcile`, `eval`, `sweep`, `retune`, `expire_cases`
- [ ] Concurrency safe with multiple worker replicas (test with two)

### `engine.py`

- [ ] Full pipeline: load → normalize → block → compare → score → route → adjudicate → persist
- [ ] `ENGINE_VERSION` constant, written into every run ⭐
- [ ] Batched processing with progress reporting into `reconciliation_runs`
- [ ] Per-stage timing captured
- [ ] Partial failure handling: one bad record fails that record, not the run
- [ ] Run status transitions correctly on success, failure and cancellation
- [ ] Counts by outcome aggregated onto the run row
- [ ] LLM call count, tokens and cost aggregated onto the run row

### Snapshot hashing ⭐

- [ ] `provider_snapshot_hash` — stable hash over the provider set actually used
- [ ] `sanction_snapshot_hash` — same for the sanction file's records
- [ ] Hash is order-independent and column-explicit, so it is reproducible
- [ ] Run records `engine_version`, `scoring_config_id`, `prompt_version` alongside the hashes

### Replay ⭐

- [ ] `concordance replay <run_id>` re-executes with the stored versions and hashes
- [ ] Replay refuses to run if the current data no longer matches the snapshot hash
- [ ] Replay serves every LLM call from cache — zero network calls
- [ ] Replay asserts decision-level identity with the original run and reports any drift
- [ ] Replay is exercised in a test

### Diff ⭐

- [ ] `concordance diff <run_a> <run_b>`
- [ ] Reports counts: unchanged, changed decision, changed confidence beyond a threshold, new, removed
- [ ] Per-changed-record detail: old decision, new decision, old/new confidence
- [ ] Reports the config delta (scoring config, prompt version, engine version) that explains the change ⭐
- [ ] Machine-readable output for the Stage 9 UI

### Case expiry job (Q3 — scheduled)

- [ ] `expire_cases` handler
- [ ] Moves `ACTIVE` cases past `end_date` to `EXPIRED`
- [ ] Writes an audit log entry for each transition ⭐
- [ ] Audit rows use `actor_user_id = NULL`, `actor_role = 'system'` ⭐
- [ ] In-house interval scheduler in the worker loop — no external cron, no APScheduler ⭐
- [ ] Job is idempotent — a second run in the same day transitions nothing
- [ ] Startup catch-up run handles cases that expired while the worker was down ⭐

### Supersede & conflict handling (Q5) ⭐

- [ ] A new run marks prior `match_results` for the same sanction record as superseded ⭐
- [ ] Nothing is ever deleted or overwritten — history stays reachable ⭐
- [ ] **Existing `ACTIVE` cases are never mutated by a re-run** ⭐
- [ ] A re-run reaching a different decision on a record with an `ACTIVE` case sets `conflict_flag` and `conflict_match_result_id` ⭐
- [ ] Conflicts surface in the review queue as their own filter
- [ ] Test: re-run with a changed config, confirm the active case survives untouched and is flagged

### GATE 6
- [ ] 5,000 sanction records reconciled against 50,000 providers in an acceptable wall time — **record the number** ⭐
- [ ] Two worker replicas process a queue with no double-execution ⭐
- [ ] `concordance replay <run_id>` is decision-identical to the original ⭐
- [ ] `concordance diff` between two scoring configs lists changed decisions with the causing config delta ⭐
- [ ] Killing the worker mid-run leaves the job reclaimable, not stuck
- [ ] Case expiry job transitions a seeded past-dated case and writes a `system`-actor audit row ⭐
- [ ] Re-run supersedes prior results; active case survives untouched and is conflict-flagged ⭐

---

## Stage 7 — API + auth + workflow · ~12h

### Auth

- [ ] argon2 password hashing with sane parameters
- [ ] `POST /auth/register` (admin-only in production; open in dev, gated by `ENV`)
- [ ] `POST /auth/login` → access + refresh tokens
- [ ] `POST /auth/refresh` → rotates the refresh token, revokes the old ⭐
- [ ] `POST /auth/logout` → revokes the refresh token
- [ ] `GET /auth/me`
- [ ] JWT signed with `JWT_SECRET`, short access TTL, longer refresh TTL
- [ ] `get_current_user` dependency
- [ ] `require_role("admin")` dependency ⭐
- [ ] Inactive users rejected
- [ ] Password strength validation on register
- [ ] Generic error on bad credentials — never reveal whether the email exists ⭐

### Sanctions — two-phase upload (Q1) ⭐

- [ ] `POST /sanctions/upload` — multipart Excel; **inspects only, does not ingest** ⭐
- [ ] Inspection returns detected columns, sample rows, and a proposed canonical mapping ⭐
- [ ] Proposal reuses the stored default mapping when the source authority is recognized ⭐
- [ ] Reject oversized files; cap row count
- [ ] Compute and store the file `sha256`
- [ ] Byte-identical re-upload (same sha256) rejected with 409 (Q5) ⭐
- [ ] Persist the file through the storage backend; store `storage_uri`; status `INSPECTED`
- [ ] `POST /sanctions/upload/{id}/commit` — applies a confirmed mapping and ingests ⭐
- [ ] Commit validates the mapping covers every required canonical field; precise per-field error otherwise ⭐
- [ ] `sanction_files.mapping_id` recorded so the file's interpretation is replayable ⭐
- [ ] Parse rows, persist `sanction_records` with `raw` JSONB preserved — including unmapped columns ⭐
- [ ] Populate normalized columns at write time, per record type (individual vs organization)
- [ ] Return the file id and row count; status `COMMITTED`
- [ ] `GET /sanctions` — paginated, filterable
- [ ] `GET /sanctions/{id}`
- [ ] `GET /sanctions/files` — upload history with lineage and the mapping used
- [ ] `GET /column-mappings` / `POST /column-mappings` / `PUT /column-mappings/{id}`
- [ ] Setting a mapping as default for a source authority

### Reconciliation

- [ ] `POST /reconciliation/run` — enqueues a job, returns the run id immediately ⭐
- [ ] Accepts an optional `scoring_config_id` and `file_id`
- [ ] `GET /reconciliation/runs` — list with status
- [ ] `GET /reconciliation/runs/{id}` — status, progress, counts, cost
- [ ] `POST /reconciliation/runs/{id}/cancel`
- [ ] Reject a second concurrent run on the same file with 409

### Matches

- [ ] `GET /matches` — paginated
- [ ] Filter: confidence range
- [ ] Filter: decision status
- [ ] Filter: review status
- [ ] Filter: state
- [ ] Filter: sanction type
- [ ] Filter: date range
- [ ] Filter: run id
- [ ] Filter: conflict flag (Q5) ⭐
- [ ] Filter: record type (individual / organization) (Q2)
- [ ] Superseded results excluded by default; `include_superseded=true` returns history (Q5) ⭐
- [ ] Sort: confidence, date
- [ ] `GET /matches/{id}` — full detail: sanction record, all candidates, field levels, field weight contributions, AI explanation, evidence cited ⭐
- [ ] `POST /matches/{id}/approve` — admin only
- [ ] `POST /matches/{id}/reject` — analyst or admin, with comment
- [ ] `POST /matches/{id}/escalate` — analyst, with comment
- [ ] **Duplicate approval returns 409 and does not create a second case** ⭐
- [ ] Approve/reject writes a `feedback_events` row with the comparison vector ⭐
- [ ] Approving a match with no `chosen_provider_id` is rejected with a clear error

### Cases

- [ ] `POST /cases` — created from an approved match; `duration_months` defaults to 3, configurable ⭐
- [ ] `end_date` derived from `start_date + duration_months`
- [ ] `case_number` generated, human-readable, unique
- [ ] Reject creating a second `ACTIVE` case for the same provider+sanction pair
- [ ] `GET /cases` — filter by status, provider, date range; paginated
- [ ] `GET /cases/{id}` — detail including approval metadata and linked match
- [ ] `GET /cases/{id}/audit` — audit history for that case
- [ ] `POST /cases/{id}/close` — admin only, requires a reason

### Audit

- [ ] Audit middleware or service writing every mutation ⭐
- [ ] Actions covered: upload, reconciliation run, AI decision, match view, approve, reject, escalate, case create, case close, case expire, login, retune ⭐
- [ ] `before` and `after` JSONB captured on state changes
- [ ] `request_id`, actor id, actor role, IP recorded
- [ ] `GET /audit` — filter by entity type, entity id, actor, action, date range; paginated
- [ ] Audit writes never block the primary transaction from committing incorrectly — same transaction, so a failed audit fails the action ⭐

### Stats

- [ ] `GET /stats/kpis` — providers, sanctions, matched, unmatched, ambiguous, pending review, approved, cases created ⭐
- [ ] `GET /stats/confidence-distribution`
- [ ] `GET /stats/state-distribution`
- [ ] `GET /stats/case-status`
- [ ] `GET /stats/reconciliation-volume` — time series
- [ ] All stats queries indexed; none do a full table scan at 50k rows

### Cross-cutting

- [ ] Consistent error envelope on every endpoint
- [ ] Pydantic request and response schemas for everything — no bare dicts
- [ ] Pagination envelope shared: `{items, total, limit, offset}`
- [ ] CORS configured for the dev frontend origin only
- [ ] Rate limiting on `/auth/login` ⭐
- [ ] OpenAPI schema generates cleanly with correct types and examples
- [ ] No secret ever returned in a response body

### Tests

- [ ] Happy path: inspect → map → commit → run → list matches → approve → case created
- [ ] Invalid Excel: missing columns, wrong types, empty file
- [ ] Commit with an incomplete mapping is rejected with a per-field error (Q1)
- [ ] Byte-identical re-upload returns 409 (Q5)
- [ ] Updated file from the same source is accepted and creates a new run (Q5)
- [ ] Re-run supersedes prior results and leaves the active case untouched but flagged (Q5) ⭐
- [ ] Organization sanction record routes to the organization model and matches correctly (Q2) ⭐
- [ ] Duplicate approval → 409 ⭐
- [ ] Ambiguous match cannot be approved without an explicit provider choice
- [ ] RBAC: analyst denied on approve and case-close
- [ ] Unauthenticated request → 401
- [ ] Expired token → 401
- [ ] Refresh rotation invalidates the old token
- [ ] Case creation with a non-default duration
- [ ] Case expiry transition
- [ ] Audit row written for every mutating test above ⭐
- [ ] Pagination boundary tests

### GATE 7
- [ ] Full pytest suite green
- [ ] `docker compose up` → OpenAPI docs load at `/docs` with no schema errors
- [ ] Every endpoint in this stage exercised by at least one test
- [ ] Duplicate-approval 409 demonstrated ⭐
- [ ] RBAC denial demonstrated ⭐

---

## Stage 8 — React application · ~16h

### Scaffolding

- [ ] Vite + React 19 + TypeScript project under `frontend/`
- [ ] Tailwind configured with a design-token scale (spacing, radii, colour)
- [ ] TanStack Query configured with sane defaults (stale time, retry policy)
- [ ] TanStack Table for every data grid
- [ ] Recharts for charts
- [ ] Router with protected routes
- [ ] **Typed API client generated from the OpenAPI schema** ⭐ — not hand-written
- [ ] Client regeneration wired into a `make` target
- [ ] Auth token storage, refresh-on-401 interceptor, logout on refresh failure
- [ ] Role-aware rendering — analyst never sees an approve button that will 403 ⭐

### Design system

- [ ] Status vocabulary defined once: `MATCH`, `AMBIGUOUS`, `UNMATCHED`, `PENDING`, `APPROVED`, `REJECTED`, `CASE_CREATED` ⭐
- [ ] One colour + one icon per status, used identically on every screen ⭐
- [ ] Confidence badge component with a consistent scale
- [ ] Shared table component: sorting, server-side pagination, column visibility
- [ ] Shared filter bar component
- [ ] Loading skeletons (not spinners) for tables and cards
- [ ] Empty states with a useful next action
- [ ] Error boundaries per route
- [ ] Toast notifications for mutations
- [ ] Layout shell: sidebar navigation across Dashboard, Providers, Sanctions, Queue, Cases, Audit ⭐
- [ ] Responsive down to ~1280px without breakage

### Login

- [ ] Login form with validation
- [ ] Error handling for bad credentials
- [ ] Redirect to the intended route after login

### Dashboard

- [ ] KPI tiles: providers, sanctions, matched, unmatched, ambiguous, pending review, approved, cases created ⭐
- [ ] Confidence distribution chart
- [ ] State distribution chart
- [ ] Case status chart
- [ ] Reconciliation volume over time chart
- [ ] Operational summary panel giving a clear overview of workload
- [ ] Every chart has an accessible label and a readable empty state

### Providers

- [ ] Directory table: provider id, NPI, name, specialty, organization, location, compliance status ⭐
- [ ] Server-side filter, sort and pagination
- [ ] Search by name and NPI
- [ ] Provider profile view with full detail
- [ ] Profile shows related compliance and reconciliation history

### Sanctions

- [ ] Sanction records table with source information ⭐
- [ ] Source file lineage view, showing which column mapping produced each file
- [ ] Upload step 1: file picker, progress, inspection result ⭐
- [ ] **Column mapping UI** — detected source columns on the left, canonical fields on the right, proposed mapping pre-filled (Q1) ⭐
- [ ] Sample rows shown live under the mapping so the analyst can see the effect ⭐
- [ ] Unmapped required fields blocked with a clear message
- [ ] Save mapping as the default for this source authority
- [ ] Upload step 2: commit, with per-column validation errors surfaced clearly ⭐
- [ ] Duplicate-file 409 shown as a readable message, not a raw error
- [ ] Post-upload prompt to trigger reconciliation

### Queue

- [ ] Review queue table of results needing review ⭐
- [ ] Filters: confidence, status, state, sanction type, date ⭐
- [ ] Filter: conflicts only (Q5) ⭐
- [ ] Filter: individual vs organization (Q2)
- [ ] Saved views / persisted filter state
- [ ] Bulk selection
- [ ] Row click navigates to Investigation

### Investigation ⭐ — the centrepiece

- [ ] Side-by-side sanction record vs candidate provider ⭐
- [ ] Per-field agreement badge showing the level, the score, **and the weight contribution** ⭐
- [ ] Fields visually sorted or marked by evidence strength
- [ ] Candidate ranking list with the ability to switch the selected candidate ⭐
- [ ] Calibrated confidence displayed with its position in the accept/grey/reject band ⭐
- [ ] AI explanation panel
- [ ] **Cited evidence highlighted in the record above** ⭐
- [ ] Route indicator: deterministic / probabilistic / LLM
- [ ] Recommendation banner: APPROVE / REVIEW / REJECT ⭐
- [ ] Approve action (admin), with confirmation
- [ ] Reject action, with comment
- [ ] Escalate / send-to-review action, with comment
- [ ] Create-case modal on approval, with configurable duration defaulting to 3 months ⭐
- [ ] Keyboard navigation between queue items — analysts work in volume
- [ ] Handles the no-candidate case gracefully
- [ ] **Organization records render the organization field set** — legal name, DBA, EIN — not empty DOB/first-name rows (Q2) ⭐
- [ ] Superseded-result banner with a link to the current result (Q5) ⭐
- [ ] Conflict banner when this record's active case disagrees with a newer run (Q5) ⭐

### Cases

- [ ] Case list: case id, provider, sanction, start/end dates, status, approval metadata ⭐
- [ ] Filter by status: active, pending, completed/expired, rejected, closed ⭐
- [ ] Case detail view
- [ ] Status timeline visualization
- [ ] Audit history embedded in the detail view ⭐
- [ ] Conflict indicator on flagged cases, linking to the newer contradicting result (Q5) ⭐
- [ ] Expired cases show the system-actor audit row that expired them (Q3)
- [ ] Close-case action (admin) with reason

### Audit

- [ ] Event timeline showing timestamp, action, actor, entity ⭐
- [ ] Filters: entity type, actor, action, date range
- [ ] Before/after diff viewer for state changes
- [ ] Deep link from a case or match into its filtered audit view

### GATE 8
- [ ] Full workflow driven in the browser: login → upload → **map columns** → commit → run → review → approve → case created → visible in audit ⭐
- [ ] An organization sanction record reviewed end to end with the organization field set rendered ⭐
- [ ] Analyst account cannot see or invoke admin-only actions ⭐
- [ ] Every screen has a working loading, empty and error state
- [ ] Frontend builds clean: `tsc --noEmit` and the production Vite build both pass
- [ ] No console errors during the full workflow
- [ ] `npm run dev` proxies to the locally running API without CORS errors
- [ ] `npm run build` output served statically also works — proves it is not dev-server-dependent (nginx packaging comes in Stage 10)

---

## ⬆ CUT LINE — above this is a complete, shippable, portfolio-grade system ⬆

*If the sprint runs out, ship Stage 10 now and treat Stage 9 as follow-on work.
Order of sacrifice within Stage 9: assistant → feedback loop → run-comparison UI → Lab page.*

---

## Stage 9 — Lab, feedback loop, assistant · ~14h

### Lab page ⭐ — best screenshot in the project

- [ ] Corruption dial control (0 → 0.9)
- [ ] Strategy toggles: deterministic, fuzzy, probabilistic, probabilistic+LLM ⭐
- [ ] **Robustness curve**: precision / recall / F1 versus corruption level, one line per strategy ⭐
- [ ] **Reliability diagram**, before and after calibration, with the perfect-calibration reference line ⭐
- [ ] ECE and Brier displayed alongside the diagram
- [ ] Grey-band width indicator
- [ ] **LLM cost panel**: calls, tokens, dollars — versus an LLM-on-everything baseline ⭐
- [ ] F1 comparison against that baseline, proving routing costs little accuracy ⭐
- [ ] Blocking recall displayed
- [ ] Per-scenario accuracy breakdown
- [ ] `POST /lab/sweep` endpoint enqueueing a sweep job
- [ ] `GET /lab/results` reading `eval_runs`
- [ ] Charts readable in a screenshot at presentation size

### Feedback loop ⭐

- [ ] `feedback_events` populated on every approve and reject (wired in Stage 7 — verify here)
- [ ] `concordance retune` — refits m/u on accumulated labels (supervised, not EM) ⭐
- [ ] Re-optimizes thresholds against `TARGET_PRECISION` using the labelled set
- [ ] Writes a **new** `scoring_configs` row; never mutates an existing one ⭐
- [ ] Minimum-label guard — refuse to retune below a configurable label count
- [ ] `POST /scoring-configs/retune` endpoint, admin only
- [ ] `GET /scoring-configs` — list versions with metrics and lineage
- [ ] `POST /scoring-configs/{id}/activate`
- [ ] Precision/recall per review round chart ⭐
- [ ] UI showing which config version produced which run
- [ ] Guard: retuning on biased labels (analysts only ever see the grey band) acknowledged and documented ⭐

### Run comparison UI ⭐

- [ ] Run picker for two runs
- [ ] Summary: unchanged, changed, new, removed counts
- [ ] Config delta panel explaining what differs between the runs ⭐
- [ ] Changed-decision table: old vs new decision and confidence
- [ ] Drill into any changed record's Investigation view

### AI Assistant ⭐

- [ ] Read-only Postgres role created in a migration ⭐
- [ ] Whitelisted views defined for the assistant — never raw tables ⭐
- [ ] Views exclude `users`, `refresh_tokens`, password hashes and API-key-bearing rows ⭐
- [ ] Schema description generated for the prompt from the whitelisted views only
- [ ] NL → SQL prompt, versioned like the others
- [ ] **Guard: parse the generated SQL with `sqlglot`** ⭐
- [ ] Guard: reject anything that is not a single statement ⭐
- [ ] Guard: reject anything that is not a `SELECT` ⭐
- [ ] Guard: reject DDL and DML keywords ⭐
- [ ] Guard: reject comments and statement separators ⭐
- [ ] Guard: reject any table or view outside the whitelist ⭐
- [ ] Guard: reject CTEs or subqueries that reach outside the whitelist ⭐
- [ ] Guard: enforce an injected `LIMIT`
- [ ] Execute under a statement timeout, on the read-only role ⭐
- [ ] **Show the generated SQL alongside every answer** ⭐
- [ ] Render results as a table, and as a chart where the shape suits it
- [ ] Rejected queries explain *why* they were rejected
- [ ] `POST /assistant/query` endpoint, authenticated
- [ ] Assistant queries written to the audit log ⭐
- [ ] Test suite of adversarial prompts attempting injection, privilege escalation and data exfiltration ⭐
- [ ] Assistant page in the UI with query history

### GATE 9
- [ ] Lab page renders the robustness curve and reliability diagram from real sweep data ⭐
- [ ] LLM cost-versus-baseline panel shows a real saving ⭐
- [ ] `concordance retune` produces a new config version with improved holdout precision ⭐
- [ ] Precision-per-round chart shows movement across at least three simulated review rounds
- [ ] Run comparison shows a real diff between two configs
- [ ] Every adversarial assistant prompt in the test suite is rejected ⭐
- [ ] Assistant answers at least ten realistic analyst questions correctly

---

## Stage 10 — Packaging, hardening, docs, demo · ~12h

**Docker Desktop must be installed before starting.** See Stage -1.

First appearance of containers. The application is known-good natively by now, so any
failure here is unambiguously a packaging failure — which is exactly why this is last.

### Containerization

- [ ] `.dockerignore` — `.git`, `.venv`, `node_modules`, `dist`, `data/generated`, `.cache`, `**/__pycache__`
- [ ] `backend/Dockerfile` — multi-stage, base `python:3.12-slim`, non-root user, no build toolchain in the final layer
- [ ] `frontend/Dockerfile` — Node build stage → `nginx:alpine` serve stage
- [ ] `frontend/nginx.conf` — SPA fallback to `index.html`, `/api` proxy, gzip
- [ ] `docker-compose.yml` — services `postgres`, `api`, `worker`, `web`
- [ ] `postgres` service: image `postgres:16`, named volume, healthcheck `pg_isready`
- [ ] `api` and `worker` share the backend image, differ only by entrypoint ⭐
- [ ] `api` and `worker` both `depends_on: postgres: condition: service_healthy`
- [ ] Healthchecks on `api` and `web`
- [ ] `docker-compose.dev.yml` override — source bind mounts, `--reload`, Vite dev server
- [ ] Migrations run on api startup, or as an explicit one-shot service — decide and document ⭐
- [ ] Confirm no secret literal appears anywhere in either compose file ⭐
- [ ] `make up` / `make down` / `make logs` / `make ps` now wired to compose
- [ ] `make clean` drops volumes, prompting for confirmation
- [ ] Containerized run reproduces the native metrics exactly ⭐

### Tests

- [ ] Coverage ≥80% on `src/concordance/matching/` ⭐
- [ ] Coverage ≥80% on `src/concordance/api/` ⭐
- [ ] Integration test covering the full pipeline on a small fixture
- [ ] End-to-end test: upload → run → approve → case, through the API
- [ ] Performance test asserting the 50k×5k run stays under the recorded budget
- [ ] All tests run inside Docker, not only on the host ⭐
- [ ] Flaky tests identified and fixed, not retried

### Security pass

- [ ] No secret in the repository — verify with a history scan, not just the working tree ⭐
- [ ] `.env` confirmed gitignored
- [ ] Dependency vulnerability scan (`pip-audit`, `npm audit`)
- [ ] SQL injection review of the assistant and every raw query ⭐
- [ ] Verify the read-only role genuinely cannot write ⭐
- [ ] Verify `audit_logs` genuinely cannot be updated or deleted by the app role ⭐
- [ ] Auth review: token TTLs, refresh rotation, logout revocation
- [ ] Confirm error responses leak no stack traces or internal paths
- [ ] Confirm the LLM prompt cannot be steered by record content ⭐

### Operations

- [ ] Cold start verified on a clean machine: clone → `.env` → `docker compose up` → seed → demo ⭐
- [ ] Startup ordering robust — api and worker wait for a healthy postgres
- [ ] Containers run as non-root
- [ ] Image sizes sane; no build toolchain in final layers
- [ ] Graceful shutdown verified for api and worker
- [ ] Log output readable and structured in `docker compose logs`

### Documentation

- [ ] `README.md`: one-paragraph pitch leading with the 90%-incorrect-data problem ⭐
- [ ] README: architecture diagram
- [ ] README: **reliability diagram screenshot** ⭐
- [ ] README: **robustness curve screenshot** ⭐
- [ ] README: Investigation page screenshot ⭐
- [ ] README: quick start, verified by following it verbatim on a clean checkout ⭐
- [ ] README: "why this is not just a fuzzy matcher" section ⭐
- [ ] README: measured results table — precision, recall, F1, ECE, blocking recall, LLM cost saving ⭐
- [ ] README: tech stack and the reasoning behind the non-obvious choices (Postgres queue over Celery, learned weights over tuned weights)
- [ ] `docs/architecture.md` complete
- [ ] `docs/data_dictionary.md` complete and current
- [ ] `docs/matching_engine.md` complete and current
- [ ] `docs/scenario_catalogue.md` complete and current
- [ ] `docs/llm_providers.md` complete and current
- [ ] `docs/demo_script.md` written ⭐
- [ ] PLAN §11 open questions all marked resolved

### Demo

- [ ] Demo seed with a curated, reproducible dataset ⭐
- [ ] Demo scenario 1: **exact NPI** — instant deterministic match ⭐
- [ ] Demo scenario 2: **missing NPI** — probabilistic match with visible field evidence ⭐
- [ ] Demo scenario 3: **ambiguous** — system declines to guess, routes to human ⭐
- [ ] Demo scenario 4: replay a historical decision and show it reproduce exactly ⭐
- [ ] Demo scenario 5: change the config, diff the runs, show what moved ⭐
- [ ] Demo scenario 6: Lab page — turn the corruption dial and watch the naive strategy collapse ⭐
- [ ] Demo scenario 7: upload a file with unfamiliar headers, map the columns live, ingest ⭐
- [ ] Demo scenario 8: organization match — different field set, different model, same workflow ⭐
- [ ] Demo timed end to end, under 10 minutes
- [ ] `make demo` resets and seeds the demo state in one command ⭐

### CI

- [ ] GitHub Actions workflow: lint, typecheck, unit tests on push ⭐
- [ ] Integration tests against a Postgres service container
- [ ] Frontend build and `tsc --noEmit` in CI
- [ ] Docker build verified in CI
- [ ] Status badge in the README

### GATE 10 — ship
- [ ] Clean-machine cold start works from the README alone ⭐
- [ ] `make demo` then the six demo scenarios, run end to end without a hitch ⭐
- [ ] CI green on the default branch ⭐
- [ ] README contains real measured numbers, not placeholders ⭐
- [ ] Repository contains no secrets, in the working tree or in history ⭐

---

## Portfolio artifacts — final verification

The things a reviewer will actually look at. Each must exist and be real.

- [ ] Reliability diagram proving calibration ⭐
- [ ] Robustness curve proving graceful degradation to 90% corruption ⭐
- [ ] Investigation screen showing per-field weight contributions and highlighted cited evidence ⭐
- [ ] Replay demonstrating a bit-identical historical decision ⭐
- [ ] Run diff explaining exactly what a config change moved ⭐
- [ ] LLM cost saving versus the LLM-on-everything baseline ⭐
- [ ] Precision improving across review rounds via the feedback loop ⭐
- [ ] Assistant rejecting a hostile query, with the reason shown ⭐
- [ ] `docs/matching_engine.md` readable by someone who has never seen Fellegi–Sunter ⭐
- [ ] One command from clone to running demo ⭐
