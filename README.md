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
python tasks.py seed CORRUPTION=0.5
```

## Layout

- `backend/src/concordance/` — the Python package
- `docs/PLAN.md` — architecture and staged build plan
- `docs/CHECKLIST.md` — implementation checklist

## Licence

MIT.
