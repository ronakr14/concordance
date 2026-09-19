# The Lab

The Lab page answers two questions with measurements rather than claims:

1. **Does the engine degrade gracefully as the data gets worse?** The robustness
   curve plots precision, recall or F1 against ground truth at corruption levels
   from 0% to 90%, for the probabilistic engine and the two baselines it has to
   beat (identifier-only matching and hand-tuned fuzzy matching).
2. **Is sending only the grey band to an LLM a good trade?** The cost panel
   compares the routed system with an LLM-on-everything baseline: model calls,
   tokens and dollars on one side, F1 with a 95% interval on the other.

Everything on the page is read from the database (`lab_sweeps`, `eval_runs`),
so the page is a view of experiments that were run, never a computation of its
own.

## Two experiments

| | Sweep | LLM sample |
|---|---|---|
| What it measures | Every level × deterministic, fuzzy, probabilistic | Routed vs LLM-on-everything at 0.3 / 0.5 / 0.7 |
| Cost | Minutes of CPU, no network | Seconds of CPU, hundreds of rate-limited model calls |
| Job kind | `lab_sweep` | `lab_llm` |
| Start it | `POST /lab/sweep` (admin), or `concordance lab sweep` | `POST /lab/llm` (admin), or `concordance lab llm` |

They are separate because their costs are unrelated. A sweep finishes in about
four minutes (50,000 providers × 5,000 sanction records × 10 levels, four
levels in parallel); a free-tier LLM run at 8,000 tokens per minute takes hours.
The LLM run reuses the sweep's datasets and seed (`parent_id`), so its points
land on the same axes, and every answer goes through the response cache, so an
interrupted run resumes without paying for the calls it already made.

Only one experiment runs at a time, across both kinds: four 50,000-provider
blocking indexes, or one provider's rate limit, is enough load.

## The reliability diagram

Measured on the fit's holdout, which is the only place the uncalibrated
posterior is measured, so the diagram shows what isotonic calibration changed
and not only where it ended. The unit is the **best candidate per record**, the
same quantity the thresholds are applied to. Calibrating over every candidate
pair would give a far better-looking ECE and mean nothing, because most pairs
are obvious non-matches that no decision is ever made about.

Each bin is drawn as a point sized by its record count, not as a line. Most
records sit near 0 or 1; the middle bins hold a handful each, and a line
through them draws their noise as a shape.

## The LLM estimator

A full LLM-on-everything run needs a call for nearly every record at every
level. The sample makes the comparison affordable, and the estimator keeps it
honest:

1. **Score everything with the engine first.** That splits the records into
   three strata of known size: no candidate (no strategy can ask a model, so all
   agree), grey band (the engine abstained), and decided.
2. **Sample the grey and decided strata** (100 each by default) and send only
   the sample to the model. A grey-band record produces the same request under
   either strategy, so one call serves both. Each stratum's sample is split
   across two **cells**, records that truly match and records that do not,
   in proportion to their size (each populated cell gets at least five).
3. **Combine exact counts with scaled sample counts.** Each strategy's true and
   false positives are the exact counts in every stratum it leaves to the
   engine, plus each cell's sample counts scaled to that cell's size where it
   asks the model. Recall's denominator (how many records truly match) comes
   from ground truth and is never estimated. Because a true positive can only
   come from a match cell, and each match cell is scaled to its exact size,
   estimated recall cannot exceed 1.
4. **Stratified bootstrap** for the interval, resampling within cells only.

Stratifying on ground truth is not using the answer key to flatter the model:
the calls are the same, and the Lab already uses ground truth for every
number it reports. It changes how the sample is drawn and weighted, which is
what auxiliary information is for in any stratified survey.

### When a run is cut short

Free tiers have daily quotas, and a run that exhausts them keeps going with
every remaining call failing. Two rules keep that from biasing the result:

- **Calls go out in a seeded random order, interleaved across all cells.**
  The synthetic files are written scenario by scenario, matches first. The
  first real run called its sample in file order, grey band first, and lost
  the tail to the quota: it kept the matches, dropped the rest, and reported an
  LLM-on-everything F1 of 1.05. With random order a cut leaves a smaller random
  sample, not a biased one.
- **A stratum with too few answers is withheld, not extrapolated.** Below 30
  (or below the whole planned sample, where fewer than 30 were asked for)
  answered calls in a stratum (or with a populated cell left empty), the
  strategies that depend on it are not estimated at that level: routed needs
  the grey band, LLM-on-everything needs both. The call counts are exact and
  are still reported; the panel says which strategies were withheld and why.

The test suite checks the estimator the hard way: with the sample set larger
than the strata (a scaling factor of one), the routed estimate must equal an
exhaustive run of the routed strategy, to the true positive.

Other rules the panel follows:

- **Tokens are metered through the cache.** A cached answer is free to replay
  but cost the same to produce, so a rerun reports the same cost.
- **A call that never completed is dropped, not scored.** When every provider
  is rate-limited, that says nothing about the record. It is reported as a
  dropped count, not counted as the model abstaining.
- **In the baseline, a model that declines means a person reviews the record.**
  The LLM-on-everything system has no engine decision to fall back on, and
  the extra review load is part of its cost.
- **Dollars use a reference price** (`LAB_PRICE_MODEL`, default
  `meta-llama/llama-3.3-70b-instruct`). The development models are free, and
  comparing $0 with $0 shows nothing. The price table entry is a placeholder
  and the page says so.

## Reading it

`GET /lab/results` returns the newest completed sweep, the LLM run that extends
it, and whichever experiment is live, so the page keeps showing the last good
curve while a new one runs. A live experiment whose job has died (a killed
worker never records its own failure) reads as failed, with the job's error.
