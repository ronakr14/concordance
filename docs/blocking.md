# Blocking — design and measured recall

Brute force is 50,000 providers × 5,000 sanction records = 250 million comparisons. The
engine never does that. It takes the union of six cheap indexed blocks (nine, counting the
organization path), dedupes, and caps the result — and then **measures what that cost in
recall**, because blocking recall is the ceiling on system recall. A true pair that never
becomes a candidate cannot be scored, no matter how good the model downstream is.

Reproduce any number below with:

```
python tasks.py seed CORRUPTION=0.5
python -m concordance.cli match blocking-recall            # timing run
python -m concordance.cli match blocking-recall --memory   # footprint run
python -m concordance.cli match blocking-recall --corruption 0.9
```

## Measured results — 50,000 providers, 5,000 sanction records

| | corruption 0.5 | corruption 0.9 |
|---|---:|---:|
| **Blocking recall** | **0.9962** (4184/4200) | **0.9819** (4124/4200) |
| Mean candidates/record | 43.2 | 41.5 |
| p95 candidates/record | 50 | 50 |
| Cap (`MAX_CANDIDATES_PER_RECORD`) | 50 | 50 |
| Index build | 8.17 s | 8.48 s |
| Index memory | 144.2 MB resident, 145.3 MB peak | — |
| Query, 5,000 records | 11.3 s | 11.0 s |

Gate 2 asks for ≥98% recall at ≤100 candidates per record with an index build under 10
seconds. Both corruption levels clear it at half the candidate budget.

`--memory` is a separate run on purpose: `tracemalloc` taxes every allocation and inflates
the build to roughly 45 seconds, so a run that reports a footprint is not a run that can
report a build time.

### Recall by scenario at corruption 0.5

| Scenario | Recall |
|---|---:|
| exact_npi | 1.0000 |
| sentinel_npi | 1.0000 |
| ambiguous | 1.0000 |
| org_exact / org_acronym / org_dba | 1.0000 |
| missing_npi | 0.9971 |
| name_variation | 0.9957 |
| address_variation | 0.9840 |
| org_type_disagreement | 0.9700 |

The two weakest rows are the informative ones. `address_variation` loses pairs where the
address block was the only one left and the address itself was corrupted twice;
`org_type_disagreement` loses pairs where an organization filed as a person also lost its
EIN. Both are visible in the report's `misses_by_corruption_family` column, which is the
point of collecting it.

At corruption 0.9 the same rows fall furthest — `address_variation` to 0.9500,
`name_variation` to 0.9743, `missing_npi` to 0.9729 — and the identifier-led scenarios stay
near 1.0. That shape is the expected one: the dial removes evidence, and the blocks that
depend on the most-corrupted fields give way first.

## The blocks

Each block is an exact lookup into an inverted index, except the last.

| Block | Key | Carries |
|---|---|---|
| `npi` | the NPI, **only when `VALID`** | Every record whose identifier survived. A sentinel or checksum-failing NPI is deliberately not a key — treating one as an identifier is how a system produces confident false matches. |
| `state_dob` | `state | birth year` | Individuals with a date of birth. The key is the year alone, so an off-by-one day or a month/day swap costs nothing; a query also asks the two adjacent years, which carries the off-by-one-year corruption. |
| `phonetic_state` | `double metaphone(surname) | state` | Misspelled and transliterated surnames. **Both** metaphone codes are indexed, so a name with an English and a native reading is reachable by either. |
| `zip_name3` | `zip5 | first three letters of the surname` | Pairs with no NPI and no DOB. Also keyed on the given name, which covers a first/last swap. |
| `license` | `licence number | licence state` | The strongest block after the NPI, and the one most often overlooked — a licence number is an identifier too. |
| `trigram` | character trigrams over the sorted name form | Everything the exact blocks missed. In-house, because `pg_trgm` does not exist before Stage 5 and a fuzzy block that needs a database cannot be swept locally. |
| `ein` | the EIN | Organizations. |
| `org_token_state` | `significant name token | state` | Organizations that share a distinctive word. |
| `org_acronym` | initials of the significant tokens | `RFPG` against `Riverside Family Practice Group`. A name that already *is* an acronym is indexed under itself, which is what closes the loop. |

### Why the trigram block runs last

It is the expensive block and the one that over-returns, so it only fills the space the
exact blocks left: if the cap is 50 and the exact blocks already proposed 44, the trigram
query asks for at most 6 more. At query time any trigram whose posting list is longer than
`max_posting` (2,000) is skipped entirely — a trigram shared by a large share of the file
carries no information and would swamp the cap with noise.

### Determinism

Candidates are ranked by how many blocks found them, then by insertion order, and the
trigram results are sorted by similarity and then document id. No set iteration order
reaches the output, so the same dataset and the same record always produce the same
candidate list — which is what makes a Stage 3 sweep comparable between runs, and what the
integration test asserts directly.

## What the report gives you

`match blocking-recall` writes both a human-readable summary and a JSON report under
`reports/`. Beyond the headline recall it records:

- **per-block contribution** — how many true pairs each block found, and how many it found
  *alone*. A block whose sole-contribution column is zero is a block that could be deleted;
  at corruption 0.9 every block has a non-zero one, which is the argument for keeping all
  six.
- **recall by scenario** — where a regression actually landed.
- **misses by corruption family** — which corruptions defeat blocking, which is what drives
  the decision about what block to add next rather than guessing.
- **the missed pairs themselves** — record id, provider id, scenario, families, and how many
  candidates were returned. All the misses at corruption 0.5 returned a full 50 candidates,
  which says the cap was binding rather than the keys being absent: raising
  `MAX_CANDIDATES_PER_RECORD` trades precision work for recall, and the number to raise it
  to is now an observation rather than an opinion.
