# Data dictionary
Every column of every table, with its type, whether it is nullable, its default
and what it means. The mechanical columns are generated from `Base.metadata`, so
they cannot drift from the schema; the meanings are written by hand.

A test asserts that every column in the schema appears in this file. Adding a
column without documenting it fails the suite, which is the only way a document
this size stays true.

## 1. Tables

### `audit_logs`

Append-only. The application role has `UPDATE` and `DELETE` revoked.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `BIGINT` | no | — | Surrogate primary key. |
| `actor_user_id` | `UUID` | yes | — | Who acted. Null for a system action. |
| `actor_role` | `VARCHAR(20)` | yes | — | Their role at the time, denormalized so the row stays true after the role changes. |
| `action` | `VARCHAR(100)` | no | — | What happened, e.g. `case.closed`, `match.approved`. |
| `entity_type` | `VARCHAR(50)` | no | — | Table or aggregate the action touched. |
| `entity_id` | `VARCHAR(64)` | no | — | Identifier of the touched row, as text. |
| `before` | `JSONB` | yes | — | JSONB snapshot before the change. Null for a creation. |
| `after` | `JSONB` | yes | — | JSONB snapshot after the change. Null for a deletion. |
| `request_id` | `VARCHAR(64)` | yes | — | Correlation id tying the row to one HTTP request or job. |
| `ip` | `INET` | yes | — | Caller's IP address, as `inet`. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `ix_audit_logs_action` on audit_logs.action
- `ix_audit_logs_actor_user_id` on audit_logs.actor_user_id
- `ix_audit_logs_created_at` on created_at DESC
- `ix_audit_logs_entity_type_entity_id` on audit_logs.entity_type, audit_logs.entity_id

### `cases`

A confirmed match under review for a fixed window.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `case_number` | `VARCHAR(50)` | no | — | Human-readable identifier a reviewer quotes, e.g. `CASE-2026-000123`. |
| `provider_id` | `VARCHAR(64)` | no | — | Provider under review. |
| `sanction_record_id` | `UUID` | no | — | Sanction record the case was opened on. |
| `match_result_id` | `UUID` | yes | — | The result that justified opening it. |
| `status` | `VARCHAR(20)` | no | `ACTIVE` | Case state. One of ('ACTIVE', 'EXPIRED', 'CLOSED', 'REJECTED'). |
| `start_date` | `DATE` | no | — | First day of the review window. |
| `end_date` | `DATE` | no | — | Last day of the review window. The expiry job reads this (Q3). |
| `duration_months` | `INTEGER` | no | `3` | Window length in months; three by default. |
| `created_by` | `UUID` | yes | — | User who opened the case. |
| `closed_by` | `UUID` | yes | — | User who closed it. |
| `close_reason` | `TEXT` | yes | — | Why it was closed. |
| `conflict_flag` | `BOOLEAN` | no | `false` | True when a later run disagreed with the result this case was opened on (Q5). The case is surfaced, never closed automatically. |
| `conflict_match_result_id` | `UUID` | yes | — | The later result that disagreed. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was last modified. Maintained by the ORM's `onupdate`. |

**Indexes**

- `ix_cases_conflict_flag` on cases.conflict_flag (partial: `conflict_flag`)
- `ix_cases_created_at` on cases.created_at
- `ix_cases_match_result_id` on cases.match_result_id
- `ix_cases_provider_id` on cases.provider_id
- `ix_cases_status_end_date` on cases.status, cases.end_date
- `uq_cases_active_provider_sanction` on cases.provider_id, cases.sanction_record_id (unique, partial: `status = 'ACTIVE'`)
- `uq_cases_case_number` on cases.case_number (unique)

### `column_mappings`

How one authority's headers map onto the canonical field set (Q1).

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `source_authority` | `VARCHAR(100)` | no | — | The issuing authority whose file layout this mapping describes. |
| `name` | `VARCHAR(100)` | no | — | Mapping name, unique within the authority. Lets one source have more than one layout. |
| `mapping` | `JSONB` | no | — | JSONB `{canonical_field: source_header}`. Canonical fields are the documented target vocabulary. |
| `is_default` | `BOOLEAN` | no | `false` | True for the mapping the upload wizard pre-fills for this authority. |
| `created_by` | `UUID` | yes | — | User who saved the mapping. Null once that user is deleted. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `uq_column_mappings_source_authority_name` on column_mappings.source_authority, column_mappings.name (unique)

### `config_activations`

Every decision to make a scoring config the one new runs score with. The active
config is the newest row; nothing on `scoring_configs` changes when it switches.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `BIGINT` | no | identity | Surrogate primary key. Breaks ties between activations in the same instant. |
| `scoring_config_id` | `UUID` | no | — | The config made active. |
| `activated_by` | `UUID` | yes | — | Who activated it. Null for the migration's seed row and for a first run that activated the only config there was. |
| `reason` | `TEXT` | yes | — | Why - for example, the holdout numbers that justified a retune. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When it became active. |

**Indexes**

- `ix_config_activations_created_at` on config_activations.created_at
- `ix_config_activations_scoring_config_id` on config_activations.scoring_config_id

### `eval_runs`

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `run_id` | `UUID` | yes | — | The reconciliation run measured, when the evaluation describes one. |
| `corruption_level` | `FLOAT` | yes | — | Corruption level of the dataset measured. |
| `strategy` | `VARCHAR(30)` | no | — | Which strategy was measured. One of ('deterministic', 'fuzzy', 'probabilistic', 'probabilistic_llm'). |
| `precision` | `FLOAT` | yes | — | True positives over predicted positives. Null where the slice predicted none. |
| `recall` | `FLOAT` | yes | — | True positives over expected matches. |
| `f1` | `FLOAT` | yes | — | Harmonic mean of precision and recall. |
| `false_positives` | `INTEGER` | no | `0` | Records matched to the wrong provider or matched when they should not have been. |
| `false_negatives` | `INTEGER` | no | `0` | Records that should have matched and did not. |
| `brier` | `FLOAT` | yes | — | Brier score of the calibrated confidences. |
| `ece` | `FLOAT` | yes | — | Expected calibration error. The holdout target is below 0.05. |
| `reliability_bins` | `JSONB` | no | `'{}'::jsonb` | JSONB of the reliability diagram's bins, so a report can be redrawn without rerunning. |
| `blocking_recall` | `FLOAT` | yes | — | Share of true matches whose provider appeared in the candidate set at all - the ceiling every later stage works under. |
| `scoring_config_id` | `UUID` | yes | — | Config the measured strategy ran with. |
| `sweep_id` | `UUID` | yes | — | The Lab experiment this cell belongs to, so a robustness curve is always one experiment's cells. Cascades on delete. |
| `detail` | `JSONB` | no | `'{}'::jsonb` | JSONB of everything the columns do not hold: per-scenario and per-model tallies, routes, the fit's before/after calibration, and for an LLM sample its intervals and costs. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `ix_eval_runs_strategy_corruption_level` on eval_runs.strategy, eval_runs.corruption_level
- `ix_eval_runs_sweep_id` on eval_runs.sweep_id

### `feedback_events`

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `BIGINT` | no | — | Surrogate primary key. |
| `match_result_id` | `UUID` | no | — | The result the reviewer judged. |
| `reviewer_id` | `UUID` | yes | — | Who judged it. |
| `label` | `VARCHAR(20)` | no | — | The verdict. One of ('TRUE_MATCH', 'FALSE_MATCH'). |
| `comparison_vector` | `JSONB` | no | `'{}'::jsonb` | JSONB of the field agreement levels as shown to the reviewer - the features the label belongs to. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `ix_feedback_events_match_result_id` on feedback_events.match_result_id

### `ground_truth`

The generator's answer for a synthetic record. Absent for production data.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `sanction_record_id` | `UUID` | no | — | The record this truth describes. Primary key - exactly one truth per record. |
| `expected_provider_id` | `VARCHAR(64)` | yes | — | The provider the generator intended. Null when the expected outcome is not a match. |
| `expected_outcome` | `VARCHAR(20)` | no | — | What a correct engine should decide. One of ('MATCH', 'NO_MATCH', 'AMBIGUOUS'). |
| `corruption_profile` | `JSONB` | no | `'{}'::jsonb` | JSONB: which corruption operations were applied, and with what parameters. |
| `scenario_tag` | `VARCHAR(50)` | no | `` | Which generated scenario the record belongs to, e.g. `address_variation`. |

**Indexes**

- `ix_ground_truth_expected_provider_id` on ground_truth.expected_provider_id
- `ix_ground_truth_scenario_tag` on ground_truth.scenario_tag

### `jobs`

The background queue, dequeued with `FOR UPDATE SKIP LOCKED`.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `BIGINT` | no | — | Surrogate primary key. |
| `kind` | `VARCHAR(50)` | no | — | Job type, e.g. `reconcile`, `expire_cases`. |
| `payload` | `JSONB` | no | `'{}'::jsonb` | JSONB arguments for the handler. |
| `status` | `VARCHAR(20)` | no | `PENDING` | Queue state. One of ('PENDING', 'RUNNING', 'DONE', 'FAILED', 'DEAD'). |
| `attempts` | `INTEGER` | no | `0` | How many times the job has been claimed. |
| `max_attempts` | `INTEGER` | no | `3` | Attempts allowed before the job is dead-lettered. |
| `locked_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When the current worker claimed it. Older than the stale-lock window means the worker died. |
| `locked_by` | `VARCHAR(100)` | yes | — | Identifier of the worker holding it. |
| `run_after` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | Not before this instant. Carries both the initial delay and the retry backoff. |
| `last_error` | `TEXT` | yes | — | Why the last attempt failed. Kept on a dead row so the failure is diagnosable. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was last modified. Maintained by the ORM's `onupdate`. |

**Indexes**

- `ix_jobs_kind` on jobs.kind
- `ix_jobs_status_run_after` on jobs.status, jobs.run_after

### `lab_sweeps`

One Lab experiment: a corruption sweep, or an LLM sample that extends one. Written in `QUEUED` before any work starts; the worker adopts the row.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `kind` | `VARCHAR(20)` | no | — | What was measured. One of ('sweep', 'llm', 'feedback'). |
| `parent_id` | `UUID` | yes | — | For an LLM experiment, the sweep whose datasets and seed it reuses. |
| `status` | `VARCHAR(20)` | no | `QUEUED` | Lifecycle state. One of ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'). |
| `requested_by` | `UUID` | yes | — | Who asked for it. Null when the system did. |
| `job_id` | `BIGINT` | yes | — | The job running it. A live row whose job is dead reads as failed. |
| `params` | `JSONB` | no | `'{}'::jsonb` | JSONB of what was asked for: levels, strategies, seed, dataset size, sample size, reference price model. |
| `progress` | `JSONB` | no | `'{}'::jsonb` | JSONB `{done, total}` - levels for a sweep, model calls for an LLM run. |
| `summary` | `JSONB` | no | `'{}'::jsonb` | JSONB of wall time, cell count and per-level errors. |
| `error` | `VARCHAR(2000)` | yes | — | Why it failed, when it did. |
| `started_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When the worker adopted it. |
| `finished_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When it completed or failed. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row last changed. |

**Indexes**

- `ix_lab_sweeps_kind_created_at` on lab_sweeps.kind, lab_sweeps.created_at
- `ix_lab_sweeps_parent_id` on lab_sweeps.parent_id

### `llm_calls`

Every adjudication request and response. Also the Stage 5 response cache.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `cache_key` | `VARCHAR(64)` | no | — | `sha256(provider + model + prompt_version + rendered_prompt)`. Unique - this is the cache index. |
| `provider` | `VARCHAR(50)` | no | — | Which vendor answered (`groq`, `openrouter`). |
| `model` | `VARCHAR(100)` | no | — | Model id that answered. |
| `prompt_version` | `VARCHAR(50)` | no | — | Prompt version sent, e.g. `adjudication_v1`. |
| `request` | `JSONB` | no | — | JSONB of the messages sent. Normalized evidence only - never raw source text. |
| `response` | `JSONB` | no | — | JSONB of the normalized `LLMResponse`, including the raw provider body. |
| `latency_ms` | `INTEGER` | no | `0` | Round-trip time for the call. |
| `prompt_tokens` | `INTEGER` | no | `0` | Tokens in the prompt, as the provider counted them. |
| `completion_tokens` | `INTEGER` | no | `0` | Tokens in the completion, including a reasoning model's hidden tokens. |
| `cost_usd` | `NUMERIC(12, 6)` | no | `0` | Cost from the price table; 0 on a free tier. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `uq_llm_calls_cache_key` on llm_calls.cache_key (unique)

### `match_candidates`

The ranked candidates beneath a result, with the arithmetic that produced the decision.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `BIGINT` | no | — | Surrogate primary key. |
| `match_result_id` | `UUID` | no | — | The result this candidate sits beneath. |
| `provider_id` | `VARCHAR(64)` | no | — | The candidate provider. |
| `rank` | `INTEGER` | no | — | 1 is the engine's best candidate. |
| `field_levels` | `JSONB` | no | `'{}'::jsonb` | JSONB `{field: agreement_level}` - the comparator output for this pair. |
| `field_weights` | `JSONB` | no | `'{}'::jsonb` | JSONB `{field: weight_contribution}`. The Investigation UI renders this directly as the evidence table. |
| `match_weight` | `FLOAT` | no | `0` | This candidate's total match weight in log-odds. |
| `posterior` | `FLOAT` | no | `0` | This candidate's posterior probability. |
| `blocking_keys` | `JSONB` | no | `'[]'::jsonb` | JSONB array of the blocks that surfaced this candidate. |

**Indexes**

- `ix_match_candidates_match_result_id_rank` on match_candidates.match_result_id, match_candidates.rank
- `ix_match_candidates_provider_id` on match_candidates.provider_id

### `match_results`

The engine's decision per record per run. Superseded rather than updated (Q5).

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `run_id` | `UUID` | no | — | The run that produced this decision. |
| `sanction_record_id` | `UUID` | no | — | The record decided. Unique together with `run_id`. |
| `decision` | `VARCHAR(20)` | no | — | The engine's answer. One of ('MATCH', 'AMBIGUOUS', 'NO_MATCH'). |
| `chosen_provider_id` | `VARCHAR(64)` | yes | — | Provider matched, when the decision is `MATCH`. Null otherwise. |
| `posterior` | `FLOAT` | yes | — | Uncalibrated posterior probability for the chosen candidate. |
| `calibrated_confidence` | `FLOAT` | yes | — | Posterior after isotonic calibration. What the UI shows and what thresholds compare against. |
| `raw_match_weight` | `FLOAT` | yes | — | Total Fellegi-Sunter match weight in log-odds, before conversion to a probability. |
| `route` | `VARCHAR(20)` | no | — | Which path decided it. One of ('deterministic', 'probabilistic', 'llm'). |
| `llm_call_id` | `UUID` | yes | — | The adjudication behind an `llm` route. Null for the other routes. |
| `explanation` | `JSONB` | no | `'{}'::jsonb` | JSONB: the engine's reason code and notes, plus the adjudicator's reasoning and cited evidence. |
| `review_status` | `VARCHAR(20)` | no | `PENDING` | Where the row sits in the review queue. One of ('PENDING', 'APPROVED', 'REJECTED', 'ESCALATED'). |
| `reviewed_by` | `UUID` | yes | — | Reviewer who actioned it. |
| `reviewed_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When they actioned it. |
| `reviewer_comment` | `TEXT` | yes | — | Reviewer's note. |
| `approved_provider_id` | `VARCHAR(64)` | yes | — | The provider a reviewer approved. Kept beside `chosen_provider_id` rather than overwriting it: on an ambiguous result the engine's column holds only its top-ranked candidate, and the reviewer's pick often differs. |
| `superseded_by` | `UUID` | yes | — | The later result that replaced this one (Q5). Null means this row is current; every list query filters on that. |
| `superseded_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When it was superseded. |
| `audit_sampled` | `BOOLEAN` | no | `false` | Drawn into the random audit of auto-rejects: a `NO_MATCH` with candidates, sent for review with inclusion probability `reconciliation_runs.audit_rate`. The only unbiased labels below the reject threshold. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was last modified. Maintained by the ORM's `onupdate`. |

**Indexes**

- `ix_match_results_created_at` on match_results.created_at
- `ix_match_results_current_audit` on match_results.review_status (partial: `audit_sampled AND superseded_by IS NULL`) — the queue's audit filter
- `ix_match_results_current` on match_results.run_id, match_results.review_status (partial: `superseded_by IS NULL`)
- `ix_match_results_current_chosen_provider` on match_results.chosen_provider_id (partial: `superseded_by IS NULL`) — the provider directory's derived compliance status
- `ix_match_results_current_confidence` on calibrated_confidence DESC NULLS LAST (partial: `superseded_by IS NULL`)
- `ix_match_results_current_decision` on match_results.decision, match_results.review_status (partial: `superseded_by IS NULL`)
- `ix_match_results_run_id_review_status` on match_results.run_id, match_results.review_status
- `ix_match_results_sanction_record_id` on match_results.sanction_record_id
- `uq_match_results_run_id_sanction_record_id` on match_results.run_id, match_results.sanction_record_id (unique)

### `provider_block_keys`

The blocking index as a table. One row per (provider, block, key), written by the loader from `blocking._keys()`.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `id` | `BIGINT` | no | — | Surrogate primary key. |
| `provider_id` | `VARCHAR(64)` | no | — | The provider this key belongs to. |
| `block` | `VARCHAR(32)` | no | — | Which block produced the key - `npi`, `state_dob`, `phonetic_state`, `zip_name3`, `license`, `ein`, `org_token_state` or `org_acronym`. |
| `key` | `VARCHAR(200)` | no | — | The block key itself, exactly as `blocking._keys()` produced it. |
| `ordinal` | `BIGINT` | no | — | Load order within the dataset. The tie-break that makes SQL and in-memory blocking return candidates in the same order. |

**Indexes**

- `ix_provider_block_keys_block_key` on provider_block_keys.block, provider_block_keys.key
- `ix_provider_block_keys_provider_id` on provider_block_keys.provider_id

### `providers`

The provider master. Written only by the loader; read by everything.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `provider_id` | `VARCHAR(64)` | no | — | Business key from the source system (`P0000123` in generated data). What every other table references. |
| `status` | `VARCHAR(20)` | no | `ACTIVE` | Provider standing in the master file. One of ('ACTIVE', 'INACTIVE', 'RETIRED'). |
| `ordinal` | `BIGINT` | no | — | Load order within the dataset. The tie-break that makes SQL and in-memory blocking return candidates in the same order. |
| `cluster_id` | `VARCHAR(64)` | yes | — | Synthetic-data provenance: which generated cluster this provider belongs to. Null for production data. |
| `cluster_role` | `VARCHAR(32)` | yes | — | Synthetic-data provenance: the provider's role inside its cluster. Null for production data. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `npi` | `VARCHAR(20)` | yes | — | National Provider Identifier as supplied, before validation. May be a sentinel or a placeholder - see §4. |
| `first_name` | `VARCHAR(100)` | yes | — | Given name as supplied. |
| `middle_name` | `VARCHAR(100)` | yes | — | Middle name or initial as supplied. |
| `last_name` | `VARCHAR(100)` | yes | — | Family name as supplied. |
| `suffix` | `VARCHAR(20)` | yes | — | Generational or credential suffix as supplied (`JR`, `III`, `MD`). |
| `dob` | `DATE` | yes | — | Date of birth. Null where the source gave none or gave one that could not be parsed. |
| `address_line1` | `VARCHAR(200)` | yes | — | Street address as supplied. |
| `address_line2` | `VARCHAR(200)` | yes | — | Suite, unit or floor as supplied. |
| `city` | `VARCHAR(100)` | yes | — | City as supplied. |
| `state` | `VARCHAR(2)` | yes | — | Two-letter state code as supplied. |
| `zip` | `VARCHAR(10)` | yes | — | Postal code as supplied; may carry the ZIP+4 extension. |
| `license_number` | `VARCHAR(50)` | yes | — | Professional licence number as supplied. |
| `license_state` | `VARCHAR(2)` | yes | — | State that issued the licence. |
| `specialty` | `VARCHAR(100)` | yes | — | Practice specialty as supplied. |
| `organization_name` | `VARCHAR(200)` | yes | — | Legal name, for an organization record. |
| `dba_name` | `VARCHAR(200)` | yes | — | Trading (`doing business as`) name, for an organization record. |
| `ein` | `VARCHAR(20)` | yes | — | Employer Identification Number, for an organization record. |
| `is_organization` | `BOOLEAN` | no | `false` | True for a type-2 (organizational) record, false for an individual. |
| `name_norm` | `VARCHAR(200)` | no | `` | Derived. Folded, punctuation-stripped name - `normalize_person_name` for an individual, `normalize_org_name` for an organization. |
| `name_sorted_norm` | `VARCHAR(200)` | no | `` | Derived. `name_norm` with its tokens sorted, so a first/last swap still matches. |
| `name_phonetic` | `VARCHAR(64)` | no | `` | Derived. The record's primary phonetic key from `phonetics.phonetic_keys`. The full set is in `provider_block_keys`. |
| `addr_norm` | `VARCHAR(300)` | no | `` | Derived. Normalized street line, or the PO box when the address is one. |
| `zip5` | `VARCHAR(5)` | no | `` | Derived. First five digits of the postal code. |
| `trigram_key` | `VARCHAR(200)` | no | `` | Derived. `name_sorted_norm` (individual) or `org_name_norm` (organization) with spaces removed - the exact string the trigram block compares. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `ix_providers_license_number_license_state` on providers.license_number, providers.license_state
- `ix_providers_name_norm` on providers.name_norm
- `ix_providers_name_norm_gin` on providers.name_norm (gin, `gin_trgm_ops`) — substring name search in the provider directory
- `ix_providers_name_phonetic_state` on providers.name_phonetic, providers.state
- `ix_providers_npi` on providers.npi
- `ix_providers_ordinal` on providers.ordinal
- `ix_providers_state_dob` on providers.state, providers.dob
- `ix_providers_trigram_key_gin` on providers.trigram_key (gin)
- `ix_providers_zip5_last_name` on providers.zip5, providers.last_name
- `uq_providers_provider_id` on providers.provider_id (unique)

### `reconciliation_runs`

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `triggered_by` | `UUID` | yes | — | User who started the run. Null for a scheduled one. |
| `file_id` | `UUID` | yes | — | The sanction file reconciled. |
| `status` | `VARCHAR(20)` | no | `QUEUED` | Run state. One of ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'). |
| `started_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When the worker picked the run up. |
| `finished_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When it finished, successfully or not. |
| `engine_version` | `VARCHAR(50)` | yes | — | Version of the matching engine that decided this run. |
| `scoring_config_id` | `UUID` | yes | — | The fitted config used. Immutable, so the run's provenance stays true. |
| `prompt_version` | `VARCHAR(50)` | yes | — | Adjudication prompt version, when the LLM was involved. |
| `strategy` | `VARCHAR(30)` | no | `probabilistic` | Which strategy decided the run. One of ('deterministic', 'fuzzy', 'probabilistic', 'probabilistic_llm'). A replay that had to guess this from the routes it produced would not be a replay. |
| `request` | `JSONB` | no | `'{}'::jsonb` | The full request the run was started with, including `max_candidates`: blocking with a different cap proposes a different candidate set, so a replay needs it. |
| `provider_snapshot_hash` | `VARCHAR(64)` | yes | — | Content hash of the provider master as it was read. Half of what makes a run replayable. |
| `sanction_snapshot_hash` | `VARCHAR(64)` | yes | — | Content hash of the sanction records as they were read. |
| `records_total` | `INTEGER` | no | `0` | Records the run considered. |
| `matched_count` | `INTEGER` | no | `0` | Records decided `MATCH`. |
| `ambiguous_count` | `INTEGER` | no | `0` | Records left `AMBIGUOUS` for review. |
| `no_match_count` | `INTEGER` | no | `0` | Records decided `NO_MATCH`. |
| `llm_calls` | `INTEGER` | no | `0` | Adjudication calls made, cache hits excluded. |
| `llm_tokens` | `INTEGER` | no | `0` | Prompt plus completion tokens across those calls. |
| `llm_cost_usd` | `NUMERIC(12, 6)` | no | `0` | Cost from the price table. Numeric rather than float because it is summed across runs. |
| `error` | `TEXT` | yes | — | Failure detail when `status` is `FAILED`. |
| `job_id` | `BIGINT` | yes | — | The queue row executing this run, when it was started through the API. Null for a run started from the CLI. |
| `audit_rate` | `FLOAT` | yes | — | The share of auto-rejects with candidates drawn into the audit sample (`AUDIT_RATE` at run time). Null on runs from before the audit existed. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |

**Indexes**

- `ix_reconciliation_runs_created_at` on reconciliation_runs.created_at
- `ix_reconciliation_runs_file_id` on reconciliation_runs.file_id
- `ix_reconciliation_runs_status` on reconciliation_runs.status
- `uq_reconciliation_runs_live_scope` on coalesce(file_id, nil UUID) (unique; partial: `status IN ('QUEUED', 'RUNNING')`). One live run per file, and one for the global scope.

### `run_patterns`

A run's whole candidate-pair tally: each distinct comparison vector and how
often it occurred, per model. Every blocked pair, not only the top-k in
`match_candidates` - this is the population a retune fits EM on.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `run_id` | `UUID` | no | — | The run. Part of the primary key. |
| `kind` | `VARCHAR(20)` | no | — | The model the pairs were scored under: `individual` or `organization`. Part of the primary key. |
| `pattern` | `SMALLINT[]` | no | — | The comparison vector: one agreement level per field, in the model's field order. Part of the primary key. |
| `n` | `INTEGER` | no | — | How many candidate pairs in the run had exactly this vector. |

**Indexes**

- `pk_run_patterns` on run_patterns.run_id, run_patterns.kind, run_patterns.pattern (primary key)

### `refresh_tokens`

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `user_id` | `UUID` | no | — | Owner of the token. |
| `token_hash` | `VARCHAR(64)` | no | — | SHA-256 of the refresh token. The raw token exists only in the client. |
| `expires_at` | `TIMESTAMP WITH TIME ZONE` | no | — | When the token stops being accepted. |
| `revoked_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When the token was revoked, if it was. Null means live. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |

**Indexes**

- `ix_refresh_tokens_user_id` on refresh_tokens.user_id
- `uq_refresh_tokens_token_hash` on refresh_tokens.token_hash (unique)

### `sanction_files`

An upload in one of the two-phase upload's three states (Q1).

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `filename` | `VARCHAR(255)` | no | — | Name the file was uploaded under. |
| `storage_uri` | `VARCHAR(500)` | no | — | Where the original bytes are stored. |
| `sha256` | `VARCHAR(64)` | no | — | Content hash of the uploaded bytes. Unique, so the same file cannot be committed twice. |
| `uploaded_by` | `UUID` | yes | — | User who uploaded it. |
| `row_count` | `INTEGER` | no | `0` | Rows found during inspection. |
| `uploaded_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the upload completed. |
| `mapping_id` | `UUID` | yes | — | Column mapping applied at commit time. |
| `status` | `VARCHAR(20)` | no | `INSPECTED` | Two-phase upload state (Q1). One of ('INSPECTED', 'COMMITTED', 'REJECTED'). |
| `source_authority` | `VARCHAR(100)` | yes | — | Issuing authority declared for this file. |
| `notes` | `TEXT` | yes | — | Why a rejected file was rejected, or what inspection found. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |

**Indexes**

- `ix_sanction_files_status` on sanction_files.status
- `uq_sanction_files_sha256` on sanction_files.sha256 (unique)

### `sanction_records`

One row per row of an uploaded file, extracted into the canonical fields with the original kept in `raw`.

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `record_id` | `VARCHAR(64)` | no | — | Business key for the row (`S000123` in generated data). |
| `file_id` | `UUID` | yes | — | The upload this row came from. Null for a generated dataset, which did not arrive as an upload. |
| `raw` | `JSONB` | no | `'{}'::jsonb` | The original row, verbatim, as JSONB. Required for audit defensibility: a match is a claim about a document as it arrived. |
| `sanction_type` | `VARCHAR(100)` | yes | — | Type of sanction or exclusion as supplied. |
| `exclusion_date` | `DATE` | yes | — | Date the exclusion took effect. |
| `reinstatement_date` | `DATE` | yes | — | Date the provider was reinstated, if they were. |
| `source_authority` | `VARCHAR(100)` | yes | — | Issuing authority for the record. |
| `ordinal` | `BIGINT` | no | — | Load order within the dataset. The tie-break that makes SQL and in-memory blocking return candidates in the same order. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `npi` | `VARCHAR(20)` | yes | — | National Provider Identifier as supplied, before validation. May be a sentinel or a placeholder - see §4. |
| `first_name` | `VARCHAR(100)` | yes | — | Given name as supplied. |
| `middle_name` | `VARCHAR(100)` | yes | — | Middle name or initial as supplied. |
| `last_name` | `VARCHAR(100)` | yes | — | Family name as supplied. |
| `suffix` | `VARCHAR(20)` | yes | — | Generational or credential suffix as supplied (`JR`, `III`, `MD`). |
| `dob` | `DATE` | yes | — | Date of birth, parsed. Null where the source gave none or gave one that could not be parsed. Derived from `dob_raw`; query and index against this column, but do not match against it. |
| `dob_raw` | `VARCHAR(64)` | yes | — | Date of birth exactly as the file wrote it — `08-24-57`, `May 26, 1985`, `20000326`. Sanction files carry partial and malformed dates, and which form a record has is evidence the matcher uses, so the verbatim string is kept beside the parsed reading. This is the value the domain `SanctionRecord.dob` and the snapshot hash read. |
| `address_line1` | `VARCHAR(200)` | yes | — | Street address as supplied. |
| `address_line2` | `VARCHAR(200)` | yes | — | Suite, unit or floor as supplied. |
| `city` | `VARCHAR(100)` | yes | — | City as supplied. |
| `state` | `VARCHAR(2)` | yes | — | Two-letter state code as supplied. |
| `zip` | `VARCHAR(10)` | yes | — | Postal code as supplied; may carry the ZIP+4 extension. |
| `license_number` | `VARCHAR(50)` | yes | — | Professional licence number as supplied. |
| `license_state` | `VARCHAR(2)` | yes | — | State that issued the licence. |
| `specialty` | `VARCHAR(100)` | yes | — | Practice specialty as supplied. |
| `organization_name` | `VARCHAR(200)` | yes | — | Legal name, for an organization record. |
| `dba_name` | `VARCHAR(200)` | yes | — | Trading (`doing business as`) name, for an organization record. |
| `ein` | `VARCHAR(20)` | yes | — | Employer Identification Number, for an organization record. |
| `is_organization` | `BOOLEAN` | no | `false` | True for a type-2 (organizational) record, false for an individual. |
| `name_norm` | `VARCHAR(200)` | no | `` | Derived. Folded, punctuation-stripped name - `normalize_person_name` for an individual, `normalize_org_name` for an organization. |
| `name_sorted_norm` | `VARCHAR(200)` | no | `` | Derived. `name_norm` with its tokens sorted, so a first/last swap still matches. |
| `name_phonetic` | `VARCHAR(64)` | no | `` | Derived. The record's primary phonetic key from `phonetics.phonetic_keys`. The full set is in `provider_block_keys`. |
| `addr_norm` | `VARCHAR(300)` | no | `` | Derived. Normalized street line, or the PO box when the address is one. |
| `zip5` | `VARCHAR(5)` | no | `` | Derived. First five digits of the postal code. |
| `trigram_key` | `VARCHAR(200)` | no | `` | Derived. `name_sorted_norm` (individual) or `org_name_norm` (organization) with spaces removed - the exact string the trigram block compares. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `is_current` | `BOOLEAN` | no | `true` | Whether this is the current version of the record. An upload that changes a known record - same `source_authority` and `record_id` - inserts a new row and sets this false on the old one, which earlier runs still point at. |
| `replaced_by` | `UUID` | yes | — | The newer version that replaced this row. |
| `replaced_at` | `TIMESTAMP WITH TIME ZONE` | yes | — | When it was replaced. |

**Indexes**

- `ix_sanction_records_file_id` on sanction_records.file_id
- `ix_sanction_records_license_number_license_state` on sanction_records.license_number, sanction_records.license_state
- `ix_sanction_records_name_norm` on sanction_records.name_norm
- `ix_sanction_records_name_phonetic_state` on sanction_records.name_phonetic, sanction_records.state
- `ix_sanction_records_npi` on sanction_records.npi
- `ix_sanction_records_ordinal` on sanction_records.ordinal
- `ix_sanction_records_state_dob` on sanction_records.state, sanction_records.dob
- `ix_sanction_records_trigram_key_gin` on sanction_records.trigram_key (gin)
- `ix_sanction_records_zip5_last_name` on sanction_records.zip5, sanction_records.last_name
- `ix_sanction_records_current_ordinal` on sanction_records.ordinal (partial: `is_current`)
- `ix_sanction_records_record_id` on sanction_records.record_id
- `ix_sanction_records_sanction_type` on sanction_records.sanction_type
- `uq_sanction_records_current_identity` on coalesce(source_authority, ''), record_id (unique; partial: `is_current`). One current version per identity.
- `uq_sanction_records_file_id_record_id` on sanction_records.file_id, sanction_records.record_id (unique)

### `scoring_configs`

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `version` | `VARCHAR(100)` | no | — | Config version string. Unique; rows are immutable once written. |
| `params` | `JSONB` | no | — | JSONB: m/u probabilities, the lambda prior and the agreement-level tables for both models. |
| `t_auto_accept` | `FLOAT` | no | — | Posterior at or above which the engine decides `MATCH` without review. |
| `t_auto_reject` | `FLOAT` | no | — | Posterior at or below which the engine decides `NO_MATCH`. Between the two is the grey band. |
| `calibrator` | `JSONB` | no | `'{}'::jsonb` | JSONB knots of the fitted isotonic calibrator, so a stored confidence is reproducible without refitting. |
| `fitted_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the fit ran. |
| `fitted_from` | `VARCHAR(20)` | no | `em` | How the parameters were obtained. One of ('em', 'supervised', 'semi_supervised', 'manual'). |
| `notes` | `TEXT` | yes | — | Free text about the fit - what dataset, what changed. |
| `parent_id` | `UUID` | yes | — | The config a retune started from. Null for an EM fit. The chain of these is a config's lineage. |
| `metrics` | `JSONB` | no | `'{}'::jsonb` | JSONB: what the fit measured when it was written - labels used by source, the holdout it was judged on, and the parent's numbers on that same holdout. |
| `created_by` | `UUID` | yes | — | Who asked for the fit. Null for a CLI or migration import. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |

**Indexes**

- `ix_scoring_configs_parent_id` on scoring_configs.parent_id
- `uq_scoring_configs_version` on scoring_configs.version (unique)

### `users`

| Column | Type | Null | Default | Meaning |
|---|---|---|---|---|
| `email` | `VARCHAR(320)` | no | — | Login identity. Uniqued case-insensitively by a functional index on `lower(email)`. |
| `password_hash` | `VARCHAR(255)` | no | — | Argon2 hash. The plaintext never reaches the database. |
| `full_name` | `VARCHAR(200)` | yes | — | Display name for the UI and for audit rows. |
| `role` | `VARCHAR(20)` | no | — | Authorisation role. One of ('analyst', 'admin'). |
| `is_active` | `BOOLEAN` | no | `true` | False disables login without deleting the user, so audit rows keep pointing at a real row. |
| `id` | `UUID` | no | `gen_random_uuid()` | Surrogate primary key. |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was inserted. Server clock, not the client's. |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | no | `now()` | When the row was last modified. Maintained by the ORM's `onupdate`. |

**Indexes**

- `uq_users_email_lower` on lower(email) (unique)

---

### Assistant views

Five read-only views, the only objects `concordance_assistant` may read. They
are projections, not tables: no JSONB column is exposed, so `sanction_records.raw`
and `match_results.explanation` cannot be read through them. See
`docs/assistant.md` and `backend/src/concordance/assistant/views.py`, which
documents every column for the prompt.

| View | Rows | Built from |
|---|---|---|
| `assistant_matches` | current engine decisions (`superseded_by IS NULL`) beside the record they decided | `match_results`, `sanction_records` |
| `assistant_cases` | compliance cases with a derived `phase` | `cases`, `sanction_records` |
| `assistant_providers` | the provider master, names flattened | `providers` |
| `assistant_sanctions` | current sanction records | `sanction_records` |
| `assistant_runs` | runs with their config version | `reconciliation_runs`, `scoring_configs` |

## 2. Enumerations

Every enumerated column is a `varchar` with a `CHECK` constraint rather than a
native Postgres enum type - see `db/base.py` for why. The allowed values are
defined once in `db/enums.py` and the constraint is generated from them.

| Column | Allowed values |
|---|---|
| `users.role` | `analyst`, `admin` |
| `sanction_files.status` | `INSPECTED`, `COMMITTED`, `REJECTED` |
| `providers.status` | `ACTIVE`, `INACTIVE`, `RETIRED` |
| `reconciliation_runs.status` | `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `scoring_configs.fitted_from` | `em`, `supervised`, `semi_supervised`, `manual` |
| `match_results.decision` | `MATCH`, `AMBIGUOUS`, `NO_MATCH` |
| `match_results.route` | `deterministic`, `probabilistic`, `llm` |
| `match_results.review_status` | `PENDING`, `APPROVED`, `REJECTED`, `ESCALATED` |
| `cases.status` | `ACTIVE`, `EXPIRED`, `CLOSED`, `REJECTED` |
| `ground_truth.expected_outcome` | `MATCH`, `NO_MATCH`, `AMBIGUOUS` |
| `eval_runs.strategy` | `deterministic`, `fuzzy`, `probabilistic`, `probabilistic_llm` |
| `feedback_events.label` | `TRUE_MATCH`, `FALSE_MATCH` |
| `jobs.status` | `PENDING`, `RUNNING`, `DONE`, `FAILED`, `DEAD` |
| `lab_sweeps.kind` | `sweep`, `llm`, `feedback` |
| `lab_sweeps.status` | `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` |

---

## 3. Derived columns

Six columns on `providers` and `sanction_records` are computed at load time by
the Stage 2 normalizer, never in SQL. One implementation, called once, so the
table and the in-memory index can never disagree about how a name folds.

| Column | Produced by |
|---|---|
| `name_norm` | `normalize_person_name()` for individuals, `normalize_org_name()` for organizations |
| `name_sorted_norm` | the same, with tokens sorted |
| `name_phonetic` | `phonetics.phonetic_keys()`, first key |
| `addr_norm` | `normalize_address().line` (or `.po_box`) |
| `zip5` | `normalize_zip()` |
| `trigram_key` | `sql_candidates.trigram_value()` |
| `provider_block_keys.key` | `blocking._keys(record, indexing=True)` |

---

## 4. NPI sentinels and placeholders

The `npi` column stores what the source supplied. `classify_npi()` decides what
it is worth, and only a `VALID` NPI may drive a deterministic match or key the
blocking index. The six classifications are:

| `NpiStatus` | What it means |
|---|---|
| `VALID` | Ten digits passing the Luhn checksum over the `80840` prefix. Usable as an identifier. |
| `MISSING` | Absent or blank. |
| `SENTINEL` | Syntactically valid, but a known placeholder number. Several records can share one, so treating it as an identifier would join unrelated providers to each other. |
| `PLACEHOLDER_TEXT` | A word standing in for a number. |
| `MALFORMED` | Not ten digits once separators are stripped. |
| `CHECKSUM_FAIL` | Ten digits that fail the checksum - a typo or a fabrication, evidence of nothing. |

Sentinel values (`DEFAULT_SENTINELS`, overridable per deployment): `0000000000`, `0123456789`, `1111111111`, `1234567890`, `9999999999`.

Placeholder strings (`DEFAULT_PLACEHOLDERS`), compared after folding and stripping separators: `-`, `--`, `N.A.`, `N/A`, `NA`, `NIL`, `NO NPI`, `NONE`, `NOT APPLICABLE`, `NOT AVAILABLE`, `NULL`, `PENDING`, `TBD`, `UNK`, `UNKNOWN`, `XXXXXXXXXX`, and the empty string.
