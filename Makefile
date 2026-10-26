PY := .venv/bin/python
PORT ?= 8000
CHAOS_ALL := rename,expiry,flaky,slow
CHAOS_SEED ?= 42

INVOICE_REQUEST := Process the vendor invoices in the inbox.
ONBOARDING_REQUEST := Onboard Priya Sharma — she's joining Sales as an Account Executive this coming Monday, reporting to Daniel Ruiz.

.PHONY: setup reset sandbox sandbox-chaos demo-invoices demo-onboarding evals evals-quick test

setup:
	python3.12 -m venv .venv
	@# uv is much faster; fall back to pip if it isn't installed
	@if command -v uv >/dev/null 2>&1; then uv pip install --python $(PY) -r requirements.txt; \
	else $(PY) -m pip install -r requirements.txt; fi
	$(PY) -m playwright install chromium
	@test -f .env || cp .env.example .env
	@echo "Setup done. Fill in OPENAI_API_KEY in .env"

reset:
	$(PY) -m sandbox.seed

sandbox:
	CHAOS= $(PY) -m uvicorn sandbox.app:app --port $(PORT)

sandbox-chaos:
	CHAOS=$(CHAOS_ALL) CHAOS_SEED=$(CHAOS_SEED) $(PY) -m uvicorn sandbox.app:app --port $(PORT)

demo-invoices:
	$(PY) -m agent run "$(INVOICE_REQUEST)"

demo-onboarding:
	$(PY) -m agent run "$(ONBOARDING_REQUEST)"

evals:
	$(PY) -m evals.run_evals

evals-quick:
	$(PY) -m evals.run_evals --only invoices_clean,onboarding_clean

test:
	$(PY) -m pytest -q
