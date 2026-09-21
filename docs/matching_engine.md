# The matching engine

How Concordance decides whether a sanction record and a provider are the same
entity, why the weights are learned rather than chosen, and what each guard rail
is there to stop.

Everything here is implemented in `backend/src/concordance/matching/`. Numbers
quoted are from the 50,000-provider / 5,000-record dataset at corruption 0.5,
seed 20260914, and are reproducible with:

```
python tasks.py seed CORRUPTION=0.5
python tasks.py fit
python tasks.py eval
```

---

## 1. The shape of the problem

Two records describe a person or an organization. Neither is authoritative, both
are dirty in different ways, and the fields that would settle the question are
exactly the ones most likely to be missing. A real exclusion file has no usable
NPI on most rows; names arrive nicknamed, married, transliterated and
credential-suffixed; dates of birth arrive as `1973`, `10/1973`, `16-OCT-1973`
and `19731016` in the same column.

Two wrong answers cost differently. A false positive flags a practising
provider as excluded — somebody's livelihood, and an expensive mistake to make
in writing. A false negative leaves a genuinely excluded provider billing. The
system therefore needs three outcomes, not two, and the middle one has to be a
first-class answer rather than an admission of failure.

---

## 2. Comparison vectors

`comparators.py` reduces a pair to a fixed tuple of **ordinal agreement
levels**, one per field. Not similarity floats: Fellegi–Sunter estimates a
probability table per field per level, and a continuous score has no table to
estimate over.

**Individual vector** — `npi`, `last_name`, `first_name`, `dob`, `address`,
`state`, `zip`, `license`.

| field | levels, weakest to strongest |
|---|---|
| `npi` | BOTH_INVALID · ONE_INVALID · VALID_DISAGREE · VALID_EXACT |
| `last_name` | MISSING · DISAGREE · JW≥.85 · JW≥.92 · PHONETIC · EXACT |
| `first_name` | MISSING · DISAGREE · JW≥.85 · INITIAL · NICKNAME · EXACT |
| `dob` | MISSING · DISAGREE · YEAR_ONLY · YEAR_MONTH · TRANSPOSED · EXACT |
| `address` | MISSING · DISAGREE · SAME_ZIP_ONLY · TOKEN_SET≥.90 · SAME_STREET_DIFF_UNIT · EXACT |
| `state` | MISSING · DISAGREE · EXACT |
| `zip` | MISSING · DISAGREE · ZIP3 · ZIP5 |
| `license` | MISSING · DISAGREE · EXACT_DIFF_STATE · EXACT_SAME_STATE |

**Organization vector** — `npi`, `ein`, `legal_name`, `dba_alias`, `address`,
`state`, `zip`. `legal_name` carries an `ACRONYM` level; `ein` uses the same
four-level identifier shape as `npi`.

Three rules govern the tables.

### `MISSING` is never folded into `DISAGREE`

An absent date of birth says nothing about identity. A *different* date of birth
is among the strongest evidence against a pair there is. A model that cannot
tell them apart produces confident false negatives on exactly the records that
matter most, because most exclusion records are missing most fields. The fitted
weights bear this out: `dob: DISAGREE` scores −1.58 bits while `dob: MISSING`
scores +0.75 — nearly neutral, as it should be.

The identifier fields have no `MISSING` level, and that is the same idea said in
the identifier's own vocabulary. `BOTH_INVALID` and `ONE_INVALID` already mean
"no information"; `VALID_DISAGREE` means "these are different people". A
sentinel NPI of `0000000000` scored as a disagreement would be a manufactured
argument against a true pair, which is precisely the failure the
`sentinel_npi` scenario exists to catch.

### Levels are ordinal with stable integer codes

The EM tables index positionally and a fitted config has to keep meaning the
same thing next week. `test_every_level_code_is_stable_and_contiguous` fails the
build if a level is renumbered or a gap appears.

### Individuals and organizations get separate vectors

See §6.

### Two comparators worth singling out

**`dba_alias` does not compare legal name to legal name.** An organization has a
legal name and a trading name and the file picks one without saying which, so a
trading name on either side is compared against both of the other side's names —
but never legal-against-legal, because `legal_name` has already measured that.
Including it let a completely different trading name score `EXACT` on the
strength of the legal names agreeing: one fact counted twice, under a field that
never saw it.

**`ACRONYM` requires one side to actually be written as an acronym.** Comparing
the two sides' derived initials to each other was tried and is far too loose:
"Riverside Family Practice Group" and "Redwood Family Physicians Group" both
reduce to RFPG, as does any typo of either. The blocking index may make that
comparison — over-proposing candidates is cheap — but a comparator is evidence
and may not.

---

## 3. Fellegi–Sunter with EM

For field *i* and level *l*:

```
m[i][l] = P(level = l | the pair is a match)
u[i][l] = P(level = l | the pair is a non-match)
```

plus λ = P(a candidate pair is a match). Under conditional independence the
log-likelihood ratio for a pair is a sum:

```
w        = Σ_i log2( m[i][l(i)] / u[i][l(i)] )
posterior = 1 / (1 + exp(-(w · ln2 + logit(λ))))
```

**Nothing here is labelled.** EM fits m, u and λ from the candidate pairs alone,
treating "is this pair a match" as the latent variable. The ground truth is
never consulted until calibration.

### Why learned weights beat hand-tuned ones

A hand-tuned matcher assigns `name: 0.30, dob: 0.15, …` and those numbers never
change. The EM fit derives them from the data, and it discovers things nobody
would have written down. Three examples from the actual fit:

| field / level | learned weight (bits) | what it means |
|---|---:|---|
| `npi: VALID_EXACT` | **+17.02** | Two valid NPIs agreeing is worth about 130,000:1 |
| `last_name: EXACT` | **+1.00** | Surname agreement is *weak* evidence here |
| `state: EXACT` | **+0.11** | State agreement is worth almost nothing |
| `address: EXACT` | **+19.05** | A full street address is the strongest single non-identifier |
| `zip: ZIP5` | **+19.77** | An exact ZIP5 is nearly as decisive |

The second and third rows are the interesting ones, and a hand-tuned model would
have got them badly wrong. Surname and state agreement look like strong
evidence — and they would be, over random pairs. But these are not random pairs:
they came out of a blocking step that *selected on surname and state*. Within a
blocked candidate set almost every non-match also agrees on both, so `u` is high
and the weight collapses. The fit works this out from the data. Nobody has to
notice it, and nobody has to remember to re-derive it when the blocking keys
change.

That is the argument, and it generalizes past this dataset: EM learns that
agreeing on a rare value is stronger evidence than agreeing on a common one,
because rarity shows up directly in `u`.

### The guard rails

Each exists because the unguarded version fails, and each has a test.

**Smoothing toward the observed marginal, in proportion to component mass.**
Plain Laplace smoothing is a trap. With λ ≈ 0.027 the match component carries
about thirty-five times less mass than the non-match one, so adding the same
pseudo-count to both inflates `m` about thirty-five times more than `u`. A level
never observed at all then comes out at +5.15 bits — the model treats *"something
I have never seen"* as strong evidence of a match. This was live in the first
implementation and visible as `last_name: MISSING = +5.15`, which is nonsense.
Smoothing each component toward the overall level marginal with a pseudo-mass
proportional to that component's own mass makes an unobserved level score
exactly 0.0, which is what "no evidence" should mean.

**A floor on `u`.** Without it, a level merely rare among non-matches drives
`m/u` toward infinity and one field silently becomes the whole model.

**Label-switching correction.** EM has no idea which of its two components is
the match class. The labels are assigned *after* the fit by comparing mean
agreement between the two tables. Without this the model comes out exactly
inverted some fraction of the time and scores every true pair as a non-match.

**Degenerate-fit detection.** λ collapsing outside [1e-4, 1-1e-4] means the fit
found one component, not two. It raises `DegenerateFitError` rather than serving
a model whose posteriors are all the same number. The same error covers a fit
attempted on too few pairs — the organization model routinely has an order of
magnitude fewer than the individual one, and quietly serving a fit made on forty
pairs is worse than refusing.

**Fixed seeded restarts.** Five restarts from perturbed starting points, each
with its own generator seeded from the run seed, best log-likelihood wins. The
same seed and the same input give byte-identical parameters, which is what makes
a run replayable at Stage 6.

### Speed

EM runs over *distinct comparison patterns with counts*, not over pairs. 187,816
candidate pairs collapse to 1,527 distinct vectors, so an iteration costs
fifteen hundred operations rather than two hundred thousand. Both models fit in
under a second.

---

## 4. Calibration

A Fellegi–Sunter posterior ranks correctly and is **not a probability**. The
conditional-independence assumption is false — name, address and ZIP are
correlated — so evidence is double-counted and the posteriors pile up against 0
and 1. Measured on this dataset, the raw posteriors have an Expected Calibration
Error of **0.118**: the model says 0.999 for a large group of records and is
right about 87% of the time.

That matters more here than it usually does, because the whole threshold story
rests on it. "Auto-accept above the confidence where precision is 99%" is a
business decision only if the confidence is a real probability.

### What is calibrated, and on what scale

**The unit is the top candidate per record.** It would be easier to calibrate
over all 216,504 candidate pairs and the resulting ECE would look wonderful,
because the overwhelming majority score near zero and are correctly near zero.
It would also be meaningless: no decision is ever made about those pairs.

Folding the runner-up candidates into the fit split was also tried, on the
reasoning that their labels are equally well known and they would determine the
middle of the curve better. It is wrong and the measurement says so — holdout
ECE went from 0.0089 to 0.0213. The runners-up are almost all negatives, so the
fitted map becomes P(link | confidence, any rank) when a threshold needs
P(link | confidence, rank 1).

**The input is the match weight, not the posterior.** This is not a detail. A
pair at weight 43.6 and a pair at weight 37.2 both come back as posterior 1.0 to
within a rounding error, and the difference between them survives only in the
last few bits of a float. Fitting isotonic on that produces knots spaced 1e-13
apart and a calibrated confidence that is amplified floating-point noise — which
is exactly what the first implementation did, and it was not obvious until two
records that should have been indistinguishable came out at 0.93 and 0.40. The
match weight is the same ranking on a scale with room in it (−28.0 to +86.5
here), so the fitted map is stable.

### Isotonic regression, in-house

Pool-adjacent-violators is about thirty lines. Owning it means the calibrator
serializes to a list of knots that any language can apply, rather than to a
pickle only one version of one library can load. Applying it at inference is
arithmetic over stored numbers: no refit, no dependency.

**The input is quantized to 0.5 log2 units before pooling.** Unregularized PAVA
plants a near-vertical step wherever the fit split happens to flip, and a
holdout record landing on the wrong side of that step gets a confidence that is
confidently wrong — visible as two adjacent reliability bins reading 0.38→0.64
and 0.41→0.14, non-monotone on the holdout. Half a log2 unit is a likelihood
ratio of √2: two pairs whose total evidence differs by less than that are not
meaningfully distinguishable and belong in one block. The value was chosen as a
round interpretable interval rather than searched, because searching it on the
holdout would be tuning on the measurement.

### The result

| | before | after |
|---|---:|---:|
| ECE (individual, holdout n=1615) | 0.0868 | **0.0264** |
| Brier | 0.0865 | **0.0472** |
| ECE (organization, holdout n=407) | 0.0321 | **0.0061** |
| Brier | 0.0319 | **0.0103** |

Both sets are kept in the config and both are drawn on the reliability diagram.
The before-and-after pair is the evidence that the step did something.

---

## 5. Thresholds and the grey band

With a calibrated probability the thresholds become a business choice.

- **`t_auto_accept`** — the *lowest* confidence at which auto-accepting
  everything above it still meets `TARGET_PRECISION` (default 0.99). Lowest,
  because any higher threshold also meets the target while sending more work to
  a human for no gain.
- **`t_auto_reject`** — the symmetric choice on recall, and it names the
  distinct confidence *above* the last group being discarded. The scorer rejects
  on `confidence < t`, so setting it to the discarded value itself strands that
  whole group in the grey band. For a model that separates cleanly and whose
  negatives all land on one score, that is every negative it had. This was a
  real bug; `test_reject_threshold_discards_the_group_it_names` covers it.

At corruption 0.5 (config `config_c0.50_s20260914`) the individual model lands on
accept ≥ 0.451, reject < 0.368, with **15.5% of its holdout** in the grey band; the
organization model on accept ≥ 0.193, reject < 0.156, with **2.0%**. Over the whole
evaluated file the grey band is 19.3% of volume. That percentage is the LLM cost
driver at Stage 4, and shrinking it as the model improves is a visible win.

The thresholds are close together in confidence and still hold a sixth of the
individual volume between them, and the reason is worth stating:
`false_positive_bait` records share a name, city and state with a real provider
and differ only in date of birth, so they score in the same narrow band as true
matches with a corrupted field. On the whole file, 568 individual records score
between 0.4 and 0.5 and 40% of them are true matches — which is what a calibrated
0.44 should look like, and is exactly the population a threshold cannot separate.
Holdout recall will not support rejecting them outright, so they land in the grey
band. Section 9 has the volume.

When the holdout has fewer than twenty negatives the accept threshold is not
identifiable — every threshold has perfect precision when there is nothing to
get wrong — and the config carries a note saying so rather than a number that
reads like a guarantee.

---

## 6. Why individuals and organizations get separate fits

This is PLAN §11.2 and it is the single most important structural decision in
the engine.

Run an organization through the individual vector and it records
`first_name: MISSING`, `dob: MISSING`, `last_name: MISSING` — every time, for
every organization. The EM fit sees those levels occurring constantly among what
it believes are matches, raises `m[first_name][MISSING]` accordingly, and the
log-likelihood ratio for `first_name: MISSING` rises toward zero or above.

**The cost is not paid by organizations. It is paid by everyone.** A first-name
agreement for an individual is worth less than it should be, and a missing first
name stops being a neutral event, because the model has been taught that
missingness is normal among matches — by a population that was never going to
have the field at all. The contamination runs in both directions: the
organization model would learn from 42,500 individuals that legal names are
almost always missing.

So: two comparison vectors, two EM fits, two calibrators, two threshold pairs.
They are stored as separate keys in one `scoring_configs` row — one row, no
shared block, so the separation is structural rather than a convention somebody
has to remember. `is_organization` picks the pipeline at the top of `scorer.py`.

Evaluation reports the two separately as well as combined, for the same reason:
one number for both makes it impossible to tell which one regressed.

**Cross-type pairs** — the source filed an organization as a person, or the
reverse — are handled explicitly, never silently. Normalization already
overrules `is_organization` when a surname field parses as an organization name;
when a pair still crosses the line, both sides are coerced to the organization
model, the result is flagged `cross_type` and the note says so. Dropping the
pair would make the `org_type_disagreement` scenario unmatchable by
construction, and scoring it under the individual model is the contamination
this whole section is about.

---

## 7. Scoring and routing

`scorer.py`, in order:

### The deterministic path

A checksum-valid NPI that agrees exactly is an identifier match, and no
probabilistic model should be allowed to talk anyone out of it. But agreement on
the identifier is not licence to ignore the rest of the record. The rule is
graded, and the grading was measured:

| contradicting attributes (`dob`, `state`, `last_name` all `DISAGREE`) | outcome |
|---|---|
| none | `MATCH`, deterministically |
| exactly one | decline to short-circuit; the probabilistic path decides |
| two or more | `AMBIGUOUS`, flagged for review, naming the fields |

Treating a *single* contradiction as disqualifying is the obvious reading of the
source spec and it is too harsh: at corruption 0.5 roughly one record in seven
with a perfectly good NPI also has a date of birth off by a year, and the strict
rule sent 120 genuine matches to a human for nothing — `exact_npi` recall 0.841
against 0.980 for the graded rule. `MISSING` is never a contradiction; an
exclusion record with no date of birth is the normal case.

Two providers carrying the same valid NPI is a master-data defect, not a match,
and saying so is more useful than picking one.

### The probabilistic path

Comparison vector → match weight → posterior → calibrator → confidence, then:

- confidence ≥ `t_auto_accept` → `MATCH`
- confidence < `t_auto_reject` → `NO_MATCH`
- between → grey band, `AMBIGUOUS` until an adjudicator says otherwise

### The margin check

**If the top two candidates are within `margin_delta` (default 0.05) of each
other, the answer is `AMBIGUOUS` regardless of absolute confidence.**

Two providers at 0.97 and 0.96 are not a 0.97 match. They are a coin toss
between two people, and absolute confidence cannot see it because both genuinely
do look like the record. The planted twin, father/son and common-name clusters
in the generator exist to produce exactly this, and it is the one routing rule
that no amount of better calibration would substitute for.

Every result carries its full evidence: per-field agreement levels, per-field
weight contributions, the blocking keys that surfaced each candidate, and the
route and reason that produced the decision.

---

## 8. The baselines

The sweep runs four strategies so that "the probabilistic engine is better" is a
measurement:

- **`deterministic`** — valid NPI exact agreement only. Perfect precision,
  hopeless recall, because most exclusion records have no usable NPI.
- **`fuzzy`** — weighted RapidFuzz similarity, hand-picked weights, two
  hand-picked thresholds. The matcher most people build first, and a genuinely
  reasonable one. It has two structural weaknesses the sweep exposes: a missing
  field and a contradicting field both contribute zero to the weighted mean, and
  the weights never learn.
- **`probabilistic`** — the engine above.
- **`probabilistic_llm`** — the engine plus a grey-band adjudicator. The sweep
  still builds `NullAdjudicator` for this cell, which abstains on everything, so
  it completes with zero calls and numbers identical to `probabilistic` in every
  row of the table below. That equality is worth seeing: it proves the LLM stage
  is additive rather than load-bearing, and the baseline it establishes is real
  rather than asserted. **It also means this column does not yet measure Stage
  4.** The adjudicator exists and works - Stage 4's gate exercised it end to end
  - but wiring it into the sweep means 400 to 800 grey-band calls per level
  across ten levels, so the lift it produces is still unmeasured here rather
  than shown.

### Results

F1 by corruption level, 50k providers × 5k records, 40 cells in **417 seconds**:

| corruption | deterministic | fuzzy | probabilistic |
|---:|---:|---:|---:|
| 0.0 | 0.549 | 0.503 | **0.999** |
| 0.2 | 0.502 | 0.432 | **0.977** |
| 0.4 | 0.441 | 0.352 | **0.953** |
| 0.5 | 0.402 | 0.305 | **0.949** |
| 0.7 | 0.353 | 0.248 | **0.906** |
| 0.9 | 0.303 | 0.185 | **0.887** |

The learned model loses eleven points of F1 across the whole corruption range.
The hand-tuned one loses thirty-two, and the gap widens — which is the
robustness curve's entire argument.

Two things in that table are worth reading carefully rather than skimming. The
probabilistic column is now monotonic across all ten levels, which it was not
before: 0.7 used to come in below 0.8. That is worth no celebration — each
corruption level is an independent dataset with an independent fit, so a cell is
a sample, and neighbouring cells differ by more than the corruption dial alone.
The spread is around a point, which is the noise floor to keep in mind before
reading any single cell as a trend, and a column that happens to be ordered is
as much luck as signal. And the deterministic and fuzzy
baselines are both slightly *lower* than they were before the Stage 1 generator
fixes described in section 9 — the file now carries more genuine negatives and
fewer manufactured false positives, which the baselines have no way to exploit.

---

## 9. Known limitations

Recorded rather than hidden. Two of the three that Stage 3 originally surfaced
were properties of the synthetic generator rather than of the engine, and both
have since been fixed at Stage 1. They are kept here with their before-and-after
numbers, because what the fixes changed is the clearest evidence that the
measurements were reading the generator rather than the model.

**The `ambiguous` scenario used not to be ambiguous — fixed.** Its records were
stripped of NPI, date of birth and street address but kept the given name and
the ZIP, and they were drawn from twin clusters whose members differ precisely
by given name. Of 354 ambiguous records, 332 had more than 5 log2 units
separating their two plausible providers: the evidence genuinely picked one, the
engine answered `MATCH`, and it was scored wrong for being right. That capped
`ambiguous_accuracy` at **6.8%** and accounted for 370 of the 392 false
positives at corruption 0.5.

The fix was in two halves, because only doing the obvious half made things
worse. The scenario now draws only from `common_name` clusters and additionally
strips the licence — but stripping the city and ZIP as well, which was the first
attempt, left the record consistent with *every* same-named provider in the
state rather than with the planted cluster, and holdout ECE rose to 0.072. So
the cluster was tightened instead: `common_name` members now share city and ZIP
as well as name and state, and differ only in the four fields the scenario
strips. The record is then equally consistent with every member of its cluster
and with nobody else, and it stays inside the ZIP-and-name blocking key.
`ambiguous_accuracy` is now **0.9225**, overall precision **0.990** rather than
0.898, and F1 at corruption 0.5 **0.949** rather than 0.905.

**The organization model had no negatives — fixed.** `false_positive_bait` and
`unmatched` were individual-only, so every organization record had a true
provider, organization precision sat at 0.9964 at every threshold and the accept
cut was not identifiable from the data at all. Two organization negatives now
exist: `org_unmatched` (a plausible organization that is simply absent) and
`org_false_positive_bait` (same legal name, DBA and state as a real
organization, different EIN and type-2 NPI). The organization accept threshold
is now a real cut (**0.193** on this fit, with a 2.0% grey band), where before it
was an artefact. The config still emits a warning when a holdout has fewer than twenty
negatives, because on a small slice the condition can recur.

**`address_variation` is the weakest scenario at 0.664 recall — open, and
deliberately so.** Those records have no NPI *and* a corrupted address, and the
address fields carry the two heaviest non-identifier weights in the model, so
losing them drops the record into the grey band. Precision on the scenario is
1.000: every one of these is routed to review rather than decided wrongly, which
is the correct failure. The grey band is exactly the workload the Stage 4
adjudicator exists to take, so the honest place to measure whether more
comparator work would pay is after Stage 4, against that baseline, rather than
by guessing now.

*Answered at GATE 4.* On a 20-record `address_variation` sample from the
corruption-0.5 dataset, handing the grey band to the adjudicator moved recall
from 0.500 to 0.850 — 7 of the 10 records the engine alone could not resolve
came back as correct matches — while precision stayed at 1.000 and no record was
assigned the wrong provider. The engine's abstentions were recoverable evidence
problems rather than missing evidence, so the answer is that the adjudicator
pays and further comparator work on address is not the next thing to build. The
sample is 20 records, not the full 500: Groq's free tier caps at 8,000 tokens
per minute and one adjudication costs roughly 3,000, so a full-scenario
measurement is a paid-tier or overnight job, and the number above should be
re-measured there before it is quoted as the scenario's recall.

**Negatives mostly land in the grey band rather than in `NO_MATCH`.** Of 900
records whose true answer is `NO_MATCH`, 561 are rejected outright, 322 are
routed to review and 17 are wrongly matched. `false_positive_bait` is the
extreme case at 32/300 outright-correct and 6 wrongly matched: a record sharing
a name, city and state with a real provider and differing only in date of
birth is genuinely near the line, and holdout recall will not support a reject
threshold high enough to clear it. This is a property of the threshold
policy, not a defect — the cost of review is lower than the cost of a wrongly
cleared exclusion — but it is the single largest consumer of the grey band and
therefore the clearest thing for Stage 4 to be measured against.

---

## 10. After the first fit

Everything above describes one fit on one synthetic file. Two things happen to a
model after that, and both live in `matching/` because neither may know where its
data came from.

### Runs: the same engine, five thousand records at a time

`engine.py` wraps a strategy for a whole run. It adds three properties a single
`decide()` call does not need: a record that fails is recorded with its error and
the run carries on; counts and timings accumulate as the run goes, so a run that
dies half way still reports what it did; and every run records `ENGINE_VERSION`
beside the scoring config id, the prompt version and the two snapshot hashes. Those
five name everything that decided a number, which is what lets
`concordance run replay` reproduce a historical run exactly and say which of the
five changed when it does not. The engine imports nothing from `db/`:
`jobs/reconcile.py` feeds it from Postgres, the evaluation harness from Parquet.

### Retunes: learning from reviewers

The first fit is unsupervised: EM has no labels, only the shape of the candidate
pairs. Once reviewers start approving and rejecting, their verdicts are labels,
and a retune (`learning/retune.py`, told in full in `docs/feedback_loop.md`) uses
them in three places:

- **Semi-supervised EM.** `ClampedPatterns` carries the labelled comparison
  vectors, and `fit_em(..., clamped=...)` holds those pairs at their verdicts while
  EM estimates the class of every other pair in the run. A thousand labels
  collapse to a few hundred distinct (vector, verdict) pairs, so this costs
  nothing.
- **Recalibration on labels,** with `denoise` available for a known reviewer error
  rate ε: an observed label y becomes (y − ε) / (1 − 2ε), which has the true label
  as its expectation. The feedback-loop document measures what that does and does
  not fix.
- **Thresholds from the population, not the labels.** `expected_thresholds` cuts
  on every record a run scored. If confidences are calibrated, the precision of
  accepting everything above *t* is the mean confidence above *t*, so the 99%
  targets can be met on thousands of records instead of on a holdout of a few
  hundred labels, where one negative more or less moves the answer. The labels
  decide the calibration; the population decides where to cut it.

A retune never edits the config it started from and never activates the new one.
Activation is a separate, audited step, and the Models page shows parent and child
judged on the same holdout labels.

---

## 11. Files

| file | what it holds |
|---|---|
| `matching/comparators.py` | level tables, per-field comparators, vector assembly |
| `matching/fellegi_sunter.py` | EM, the guard rails, scoring, serialization |
| `matching/calibration.py` | reliability, ECE, Brier, isotonic, thresholds |
| `matching/scorer.py` | the two paths, routing, the margin check |
| `matching/strategies.py` | the four strategies the sweep compares |
| `matching/adjudication.py` | the grey-band seam and `NullAdjudicator` |
| `matching/engine.py` | a whole run: per-record errors, counters, `ENGINE_VERSION` |
| `matching/scoring_config.py` | the `scoring_configs` row, as JSON |
| `eval/pairs.py` | normalize and block a dataset once, reuse everywhere |
| `eval/fitting.py` | the four-step fit |
| `eval/harness.py` | metrics, per scenario and per model |
| `eval/sweep.py` | ten levels by four strategies, in parallel |
| `eval/report_html.py` | the standalone report |
| `learning/retune.py` | split labels, semi-supervised EM, recalibrate, place thresholds |
| `learning/simulate.py` | the review-rounds simulation behind the feedback-loop numbers |
