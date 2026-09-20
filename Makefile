# Thin delegation to tasks.py, which is the real runner (Windows-first project).
# Usage: make seed CORRUPTION=0.5 PROVIDERS=50000 SEED=42
#        make up            one command from a prepared checkout to a running system
#        make up DETACH=1   the same, in the background; `make down` stops it

PY ?= python
RUN := $(PY) tasks.py

# Forward make-style variables through to tasks.py.
VARS := $(foreach v,CORRUPTION PROVIDERS SANCTIONS SEED LIMIT SCENARIO STRATEGY LEVELS STRATEGIES WORKERS MAX_CANDIDATES PORT WEB_PORT DETACH YES,$(if $($(v)),$(v)=$($(v))))

.PHONY: seed verify inspect fit eval sweep test test-unit cov lint fmt typecheck clean \
        up down ps logs preflight reset-db migrate load api worker web web-build client e2e help

help:
	@$(RUN) help

seed verify inspect fit eval sweep reconcile worker migrate load test test-unit cov lint fmt typecheck clean \
api web web-build client e2e up down ps logs preflight reset-db:
	@$(RUN) $@ $(VARS)
