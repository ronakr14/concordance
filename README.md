# Concordance

Provider Sanctions & Exclusions Reconciliation.

Matches sanction/exclusion records against a provider master file using a
Fellegi–Sunter probabilistic record-linkage engine with EM-learned weights,
isotonic calibration, and an LLM adjudicator for the grey band.

> Stub. Filled properly in Stage 10 — see `docs/PLAN.md`.

## Quick start (local, no Docker, no Postgres)

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -e backend[dev]

python tasks.py seed CORRUPTION=0.5   # 50k providers, 5k sanction records
python tasks.py fit                   # EM fit, calibration, thresholds
python tasks.py sweep                 # 10 corruption levels x 4 strategies
python tasks.py eval                  # writes reports/eval_probabilistic_0.5.html
```

Open `reports/eval_probabilistic_0.5.html` in a browser. It is self-contained:
no server, no network, no build step.

## Where it stands

F1 against the corruption dial, 50k providers x 5k records, 40 cells in 355s:

| corruption | deterministic | fuzzy (hand-tuned) | probabilistic (EM) |
|---:|---:|---:|---:|
| 0.0 | 0.549 | 0.503 | **0.999** |
| 0.5 | 0.402 | 0.305 | **0.945** |
| 0.9 | 0.303 | 0.185 | **0.886** |

Holdout Expected Calibration Error: **0.087 before isotonic calibration, 0.037
after** for the individual model, and **0.032 to 0.020** for the organization
model.

## Layout

- `backend/src/concordance/` — the Python package
- `docs/PLAN.md` — architecture and staged build plan
- `docs/CHECKLIST.md` — implementation checklist
- `docs/matching_engine.md` — the engine: level tables, the EM derivation, the
  guard rails, and why learned weights beat hand-tuned ones
- `docs/blocking.md`, `docs/scenario_catalogue.md` — candidate generation and
  the synthetic dataset

## Licence

MIT.
