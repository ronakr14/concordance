# Thin delegation to tasks.py, which is the real runner (Windows-first project).
# Usage: make seed CORRUPTION=0.5 PROVIDERS=50000 SEED=42

PY ?= python
RUN := $(PY) tasks.py

# Forward make-style variables through to tasks.py.
VARS := $(foreach v,CORRUPTION PROVIDERS SANCTIONS SEED LIMIT SCENARIO STRATEGY LEVELS STRATEGIES WORKERS MAX_CANDIDATES PORT,$(if $($(v)),$(v)=$($(v))))

.PHONY: seed verify inspect fit eval sweep test test-unit cov lint fmt typecheck clean \
        up down migrate load api worker web web-build client help

help:
	@$(RUN) help

seed verify inspect fit eval sweep reconcile worker migrate load test test-unit cov lint fmt typecheck clean \
api web web-build client:
	@$(RUN) $@ $(VARS)

# Declared now, real at the stage named. Failing loudly beats a confusing error.
up down:
	@$(RUN) $@
