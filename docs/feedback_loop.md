# The feedback loop

Reviewers approve and reject what the engine proposes. Each verdict is stored
as a label (`feedback_events`), and a **retune** turns the labels into a new
scoring config. Two rules shape it:

- **A config is never edited.** A retune writes a new `scoring_configs` row whose
  `parent_id` is the config it started from, with its measurements in
  `metrics`. Runs keep pointing at the config that decided them, so replay
  still reproduces them.
- **A retune proposes; a person activates.** The live config is the newest row
  in `config_activations`. A retune does not score anything until someone
  activates it, and every activation is audited with who and why.

| Do this | With |
|---|---|
| Retune on every label so far | `concordance retune`, or `POST /scoring-configs/retune` (admin) |
| See every version, its lineage and its numbers | `concordance configs list`, `GET /scoring-configs`, the Models page |
| Make a version live | `concordance configs activate <version>`, `POST /scoring-configs/{id}/activate` (admin) |
| Watch the loop work on synthetic data | `concordance lab feedback`, `POST /lab/feedback` (admin), the Models page |

## What a retune does

1. **Collects labels.** One per match result, the newest verdict winning, each
   turned back into the exact comparison vector the reviewer saw (it was copied
   into the feedback row when they decided).
2. **Splits them** by a hash of the result id into a fit split (70%) and a
   holdout (30%).
3. **Refits m and u with semi-supervised EM** over the newest run's full
   candidate-pair tally (`run_patterns`), with the fit split's pairs carrying
   their verdicts. It starts from the parent's tables.
4. **Recalibrates** on the fit split: isotonic regression, with each label
   counted at its weight.
5. **Places the thresholds on the whole run**, rescored under the new model
   and calibrator (see below).
6. **Judges parent and new config on the holdout labels.** This is the only step
   that is evidence rather than fitting, and it is what the Models page shows
   beside each version.

A model kind with fewer than 30 labels of its own (usually the organization
model) keeps its parent's bundle unchanged, and the metrics say so.

## The label bias, and what does and does not correct it

Reviewers see what the engine sends them: every proposed match (a case needs an
approval) and the grey band. They never see auto-rejects, which is where a
missed sanction would be. The labels are a biased sample of the decisions.
Three things answer that:

- **For m and u, nothing needs correcting.** Whether a pair was reviewed depends
  on its scores and a random draw, both of which the system observed. So the
  labels are missing at random given the data, and the likelihood that
  semi-supervised EM maximizes is unbiased without reweighting.

  This is why labels are not simply added to the EM tables as pseudo-counts.
  `u` describes every candidate pair, and most of those are easy non-matches.
  Reviewed pairs are the hard ones, so adding their negatives would teach `u`
  that agreement is common among non-matches, and recall would pay for it.
- **For calibration and thresholds it does matter**, because those are claims
  about records, and the labelled records over-represent the top of the score
  range. A random **audit** draws `AUDIT_RATE` (2%) of auto-rejects with
  candidates into the review queue. Their labels have a known inclusion
  probability and are weighted by 1 / `AUDIT_RATE`. An auto-reject a reviewer
  opened on their own has no known inclusion probability. It is used in EM,
  where missing-at-random covers it, and left out of calibration, where its
  weight would have to be invented.
- **Thresholds come from the population, not the labels.** See below.

What the audit does not fix: at 2%, it produces few labels. Reviewers mostly
work the grey band, so in simulation the audit contributed a handful of labels
over five rounds. Each one carries a weight of 50, so the calibration below
the reject threshold is the noisiest part of the curve. The honest fix is a
larger audit rate. The cost of that is review time, and choosing it is a policy
decision.

## Reviewers make mistakes

`REVIEWER_ERROR_RATE` (default 2%) is the share of verdicts that are wrong. It
should be measured, for example as the share of verdicts a second reviewer
overturns on a random sample. Ignoring it breaks the loop rather than blurring
it. On labels that are 3% wrong, even a perfect model shows about 97%
precision, so a 99% target is only "met" in some thin top slice that happens to
hold no mistake. The accept threshold climbs there, and recall collapses. The
first simulation did exactly that.

The rate enters in two places:

- **EM treats a verdict as evidence, not truth.** A labelled pair's
  responsibility is P(match | vector, verdict). A verdict against strong
  evidence is weighed rather than obeyed.
- **Calibration counts de-noised labels.** Each verdict y becomes
  (y − ε) / (1 − 2ε), whose sum over any set is an unbiased estimate of the
  true count.

Both are necessary and neither is sufficient. At 3% and a few hundred labels
the observed label rate at the top of the score range - 0.94 to 1.00 across
bins - is as consistent with "every one of these is a match" as with "97% of
them are", so no amount of arithmetic can certify a 99% threshold from them.
The retune ends up proposing a cautious config that reviews far more, and the
gate below is what stops that reaching production. Things that were tried and
did not help: a two-parameter logistic calibration in place of isotonic (it
underfits the top of the range, where the thresholds live), and coarser
isotonic pooling (0.5 to 8 log2 units changed nothing that mattered).

## Why thresholds come from the population

A retune has a few hundred labels, nineteen in twenty of them matches. Placing
a 99% precision threshold from them comes down to whether one or two negatives
happened to fall in the holdout. With labels 3% wrong, the standard error of a
precision estimate from 100 labels is about 1.8 points, larger than the whole
1-point error budget. In simulation, the accept threshold jumped round to round
and took recall with it.

A run has thousands of records. If the calibrated confidences are right, the
precision of auto-accepting everything above t is the mean confidence of those
records, so the thresholds can be placed on the whole run with no labels
(`calibration.expected_thresholds`). The labels shape the calibration curve,
and the population decides where to cut it. In production, the population is
the tally run's stored candidates, rescored under the new config.

## The simulation

`concordance lab feedback` (or the Models page) runs review rounds on a
finished sweep's datasets:

- **The starting config is fitted at one corruption level (30%) and deployed on
  a messier one (70%).** That is the realistic starting point: a config fitted
  on last year's data meets a harder file. Calibrated on its own data, it would
  already be as good as the loop can make it.
- **Each round, a simulated reviewer labels 200 records from the queue** (proposed
  matches, the grey band and the audit sample). It uses ground truth and is
  wrong at the configured noise rate. It names the right candidate where one
  is offered and rejects the top one otherwise.
- **The config is retuned on every label so far**, and replaces the current one
  only if the retune's own holdout comparison recommends it - the decision an
  admin makes on the Models page. The table below also shows what happens when
  every retune is activated regardless.
- **Every round is judged against ground truth on a fixed 30% of records no
  reviewer ever sees.**

### Results

50,000 providers × 5,000 sanction records, seed 20260914, 5 rounds × 200
labels. Judged on 1,500 held-out records.

| Round | Labels | Clean labels (ε = 0) | 3% wrong, gate on | 3% wrong, gate off |
|---|---|---|---|---|
| 0 (start) | 0 | 0.906 | 0.906 | 0.906 |
| 1 | 200 | 0.931 | 0.906 | 0.904 |
| 2 | 400 | 0.945 | 0.906 | 0.465 |
| 3 | 600 | 0.947 | 0.906 | 0.800 |
| 4 | 800 | 0.947 | 0.906 | 0.804 |
| 5 | 1,000 | 0.947 | 0.906 | 0.805 |

F1 against ground truth on the evaluation split. Read across the row:

- **With clean labels the loop works.** F1 0.906 → 0.947, recall 0.841 → 0.915,
  and the grey band 20.9% → 14.0% - fewer records to review *and* more of them
  right. The last two rounds change nothing: the gate finds no measurable
  difference on the holdout and keeps the parent, which is the loop reaching
  the end of what these labels can tell it rather than drifting.
- **With 3% of verdicts wrong, every retune is refused**, always for the same
  reason: the proposed config would send far more records to review (56%, 36%,
  71% against the parent's 15%) without holding precision. It cannot certify
  the thresholds it wants from labels this thin, so it proposes a cautious
  config, and the gate declines it. The system stays exactly where it was. That
  is the honest outcome, not a good one: at this noise level the loop needs
  either more labels or a lower precision target to make progress.
- **Without the gate the same labels make it worse than useless**: F1 0.465 in
  round 2, still 0.805 by round 5, against 0.906 for doing nothing.

The gate is the finding. A feedback loop that retrains on human labels is only
safe if something compares the result against what is already running and can
say no.

## What is still open

- The reviewer error rate is configured, not measured. The table above shows
  how much a mis-set rate costs. A QA re-review workflow would measure it.
- The audit rate trades review time for unbiased labels below the reject
  threshold. At 2% it yields few labels. Raising it is a policy decision, not
  something the code should choose.
