# Scenario catalogue

Every synthetic sanction record is generated *for* a scenario and tagged with it in
`ground_truth.scenario_tag`. Evaluation therefore reports precision and recall per
scenario, not only in aggregate — which is the difference between "F1 dropped two points"
and "the engine stopped finding people whose NPI is missing".

Counts below are for the default file of **5,000 records**; the shares are fixed in
`synth/sanctions.py::SCENARIOS` and scale with `--sanctions`.

## The eight scenarios from the source spec

| Tag | Count | Expected outcome | What the record looks like | What it is designed to break |
|---|---:|---|---|---|
| `exact_npi` | 700 | `MATCH` | The sanction record carries the provider's real, checksum-valid NPI. Other fields may still be corrupted. | The deterministic path. If this is not ~100% precise and recallable, nothing downstream is trustworthy. |
| `missing_npi` | 700 | `MATCH` | NPI absent entirely; identity must come from name, DOB, address and licence. | Any engine that quietly relies on the identifier. This is the bulk of real exclusion data. |
| `sentinel_npi` | 400 | `MATCH` | NPI present but meaningless: `0000000000`, `9999999999`, `1111111111`, or the text `UNKNOWN` / `N/A` / `NONE`. | The distinction between *absent* and *disagreeing*. A sentinel treated as a real value produces confident false negatives, because "NPI disagrees" is strong evidence against a match. |
| `name_variation` | 700 | `MATCH` | No NPI, plus a forced name change: nickname substitution, a keyboard typo, or a first name reduced to an initial. | Exact-string matching, and any comparator that scores "Bob" against "Robert" as a disagreement rather than a known equivalence. |
| `address_variation` | 500 | `MATCH` | No NPI, plus a forced address change: USPS abbreviation (`Street` → `St`) or a wrong-but-in-state ZIP. | Address comparators that treat formatting as evidence. Also the blocking keys: a ZIP-based block must not lose these. |
| `ambiguous` | 400 | `AMBIGUOUS` | Drawn from a planted `common_name` cluster, then stripped of NPI, DOB, street address and licence. What is left — given name, surname, city, state, ZIP — is shared by every member of the cluster, so several providers fit *equally* well rather than merely closely. | Overconfidence. The correct behaviour is to *refuse* to auto-decide and route to review; `corruption_profile.plausible_provider_ids` lists every defensible answer. |
| `false_positive_bait` | 300 | `NO_MATCH` | Same first and last name, same city and state as a real provider — but a different person, with a different DOB (9–25 years apart) and a different valid NPI. The person is **not** in the master. | Recall-chasing. Every one of these that is matched is a provider wrongly flagged as excluded, which is the expensive error in this domain. |
| `unmatched` | 250 | `NO_MATCH` | A complete, plausible, checksum-valid record for an individual who simply is not in the provider file. Individuals only — organizations have `org_unmatched`, so each model owns its own negatives. | The threshold. A system with no `NO_MATCH` outcome scores beautifully and is useless. |

## Organization scenarios (PLAN §11.2)

Organizations are matched by their own model, so they need their own scenarios.

| Tag | Count | Expected outcome | What the record looks like | What it is designed to break |
|---|---:|---|---|---|
| `org_exact` | 250 | `MATCH` | The organization as the master holds it, with ordinary corruption. | The baseline for the organization path — and proof the two paths are actually separate. |
| `org_acronym` | 200 | `MATCH` | The file carries `RFPG`; the master carries `Riverside Family Practice Group LLC`. | Name comparators built for people. Token-set similarity scores an acronym against its expansion at nearly zero. |
| `org_dba` | 150 | `MATCH` | The file carries the trading name, the master the legal name. | Single-name matching. An organization legitimately has two names, and either may appear. |
| `org_type_disagreement` | 100 | `MATCH` | An organization filed as though it were a person: the org name sits in the surname column and `is_organization` is false. | The routing decision itself. `is_organization` is a *claim* by the source, not a fact, so the router must be able to overrule it. |
| `org_unmatched` | 200 | `NO_MATCH` | A complete, plausible organization — valid type-2 NPI, valid EIN — that is not in the provider file. | The organization reject threshold. Without organization negatives the organization model's precision is flat across the whole confidence range and no cut is identifiable from the data. |
| `org_false_positive_bait` | 150 | `NO_MATCH` | Same legal name, same DBA and same state as a real organization, but a different entity: different EIN, different type-2 NPI, another city. | The organization accept threshold, and the weight the model puts on EIN. A branch of the same group looks like this too, so the EIN disagreement is what has to settle it. |

## Planted near-duplicate clusters

Clusters live in the provider master (`cluster_id`, `cluster_role`), independent of which
records are sanctioned. They are what make `ambiguous` genuinely ambiguous.

| Cluster kind | Shape | Why it is hard |
|---|---|---|
| `twins` | Same surname, same date of birth, same address; different given names. | Every field agrees except the one that distinguishes them. |
| `father_son` | Identical first, middle and last name at one address, `Sr`/`Jr`, born 28 years apart. | Only the DOB and the generational suffix separate them — and the suffix is exactly what a credential-suffix corruption deletes. |
| `common_name` | Two or three people sharing a first name, surname, city, state and ZIP, differing only in NPI, date of birth, street address and licence. | Blocking on ZIP and name returns all of them, and the fields that separate them are exactly the ones `ambiguous` strips. This is what makes that scenario provably ambiguous rather than merely hard. |
| `branch` (org) | One legal name, two cities in one state. | Name agreement is perfect and wrong. |
| `acronym` (org) | One organization stored expanded, a related one stored as initials. | Feeds `org_acronym`. |
| `dba` (org) | Legal name and trading name swapped between the two records. | Feeds `org_dba`. |

## Corruption families

One dial, `--corruption` in `[0.0, 0.9]`, scales all five families. Each family has its own
rate and its own random stream, so a family can be retuned or disabled without reshuffling
the rest of the dataset. Corruption is applied to **both** sides — the provider master at
30% of the sanction-side rate, because a master file is dirty but not spreadsheet-dirty.

| Family | Operations | What it simulates in the real world |
|---|---|---|
| `name` | token order swap, first name → initial, nickname substitution, keyboard-adjacency typo, diacritic loss, credential suffix added or dropped, married-name change, hyphenation split or joined | Keyed-in data, forms with a single "name" box, marriage and divorce, transliterated names losing their accents, and the endless inconsistency of whether `MD` belongs in the name field. |
| `npi` | missing, sentinel value, free-text placeholder, checksum-failing digit, adjacent digit transposition, wrong length | Identifier columns that a source authority never reliably populated. The distinction between the six is the whole point: only *one* of them is evidence that this is a different provider. |
| `dob` | missing, off by one day or year, month/day swap, wrong century, alternate string format | Two-digit years, US vs ISO date order, and spreadsheets that store dates as text. |
| `address` | USPS abbreviation, unit/suite dropped, ZIP+4 instead of ZIP5, wrong ZIP, PO box substituted, whole address missing (every one of them perturbs a value that is present — none fabricates one into an empty field, because `MISSING` is its own comparator level) | Practice addresses that change, mailing vs service addresses, and normalization applied by one system and not the other. |
| `license` | missing, wrong state, formatting variation | Multi-state licensure and licence numbers written with or without their prefix, dashes and leading zeros. |

Two operations are **sanction-side only**, because a typed database column cannot hold
them: the alternate DOB *format* and the free-text NPI placeholder. Both arrive with a
spreadsheet, never from a master file.

Every applied corruption is recorded per record in `ground_truth.corruption_profile.applied`
as `{family, op, side, before, after}`. That is what makes a failure explicable: when the
engine misses a record, the profile says exactly what was done to it.

## How the engine does on each

The probabilistic strategy with no adjudicator, corruption 0.5, config
`config_c0.50_s20260914` (`reports/eval_probabilistic_0.5.json`). For a scenario
whose right answer is `MATCH` the column that matters is recall, and precision
shows whether a miss was a wrong provider or a referral; for the others it is the
share answered correctly, and the wrongly-matched count.

| Scenario | Right answer | Correct | Wrongly matched | Notes |
|---|---|---:|---:|---|
| `exact_npi` | `MATCH` | 97.7% | 0 | the rest go to review, not astray |
| `org_type_disagreement` | `MATCH` | 99.0% | 0 | |
| `org_dba` | `MATCH` | 98.7% | 0 | |
| `org_exact` | `MATCH` | 98.0% | 0 | |
| `org_acronym` | `MATCH` | 98.0% | 0 | |
| `sentinel_npi` | `MATCH` | 93.8% | 1 | the only wrong provider in the file |
| `name_variation` | `MATCH` | 93.7% | 0 | |
| `missing_npi` | `MATCH` | 91.9% | 0 | |
| `address_variation` | `MATCH` | 66.4% | 0 | weakest; every miss is a referral (see `matching_engine.md` §9) |
| `ambiguous` | `AMBIGUOUS` | 92.3% | 16 | declines to guess, as it should |
| `org_unmatched` | `NO_MATCH` | 94.5% | 6 | |
| `org_false_positive_bait` | `NO_MATCH` | 88.0% | 5 | |
| `unmatched` | `NO_MATCH` | 83.2% | 0 | the rest go to review |
| `false_positive_bait` | `NO_MATCH` | 10.7% | 6 | most go to review: near the line by construction |

A low "correct" on a negative scenario mostly means *referred*, not *wrong*: the
wrongly-matched column is the one that costs an innocent provider.

## Reading the dataset

```
python tasks.py seed CORRUPTION=0.5      # 50k providers, 5k records, ~16s
python tasks.py verify                   # invariants, machine-checked
python tasks.py inspect SCENARIO=ambiguous LIMIT=20
```

`inspect` prints each sanction record beside the provider it was derived from and the list
of corruptions applied, which is how a hand spot-check is done without opening Parquet.
