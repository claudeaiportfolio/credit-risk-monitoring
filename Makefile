.PHONY: sync test lint typecheck run-baseline score eval secret-scan

# Secrets live in the invocation surface, never in application code. Point
# ENV_FILE at a .env (gitignored) carrying the keys the eval needs:
#   ANTHROPIC_API_KEY          — llm-provider baseline completion + Layer-2 judge
#   SEC_EDGAR_USER_AGENT       — descriptive UA SEC requires (e.g. "name email")
#   BRAINTRUST_API_KEY         — optional; activates the Braintrust surface
#   COMPANIES_HOUSE_API_KEY    — used by the C3 agent's UK-registry hops (not C2)
# Usage: `make run-baseline ENV_FILE=.env`. Without it, run-baseline falls back
# to the offline answerer and Layer-2 is skipped (Layer-1 still scores fully).
ENV_FILE ?= .env
UV_RUN := uv run $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE),)

TRACE_DIR ?= traces/baseline
OUT_DIR ?= out

sync:
	uv sync --extra dev

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy

# Run the deliberately single-retrieval baseline over all fixtures -> traces.
run-baseline:
	$(UV_RUN) credit-risk-eval run-baseline --trace-dir $(TRACE_DIR)

# Score traces with the branch-correctness CheckSuite (+ Layer-2 judge when
# ANTHROPIC_API_KEY is set; + Braintrust when BRAINTRUST_API_KEY is set).
score:
	$(UV_RUN) credit-risk-eval score --trace-dir $(TRACE_DIR) --out-dir $(OUT_DIR)

# Full local eval: baseline run then score. The baseline is EXPECTED to fail the
# multi-hop fixtures (that gap is the measure), so `score`'s non-zero exit here
# is the intended signal, not a build break — hence the trailing `|| true`.
eval: run-baseline
	-$(MAKE) score

secret-scan:
	gitleaks dir . --redact --no-banner --exit-code 1
