.PHONY: sync test lint typecheck run-baseline run-agent run-agent-b score score-agent score-agent-b eval eval-agent eval-agent-b compare secret-scan

# Secrets live in the invocation surface, never in application code. Point
# ENV_FILE at a .env (gitignored) carrying the keys the eval/agent needs:
#   ANTHROPIC_API_KEY          — llm-provider completion + agent loop + Layer-2 judge
#   SEC_EDGAR_USER_AGENT       — descriptive UA SEC requires (e.g. "name email")
#   COMPANIES_HOUSE_API_KEY    — Arm A agent UK-registry hops (key as Basic-auth user)
#   AUDIT_DATABASE_URL         — optional; Postgres audit sink (else in-memory)
#   AGENT_MODEL / BASELINE_MODEL / JUDGE_MODEL — optional model overrides
#   BRAINTRUST_API_KEY         — optional; activates the Braintrust surface
# Usage: `make run-baseline ENV_FILE=.env`. Without it, run-baseline falls back
# to the offline answerer and Layer-2 is skipped (Layer-1 still scores fully).
ENV_FILE ?= .env
UV_RUN := uv run $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE),)

TRACE_DIR ?= traces/baseline
TRACE_DIR_AGENT ?= traces/agent
TRACE_DIR_AGENT_B ?= traces/agent-b
OUT_DIR ?= out
OUT_DIR_AGENT ?= out/agent
OUT_DIR_AGENT_B ?= out/agent-b

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

# Run the Arm A multi-hop investigation agent over all fixtures -> traces.
# Needs ANTHROPIC_API_KEY (loop) and COMPANIES_HOUSE_API_KEY (UK-registry hops).
run-agent:
	$(UV_RUN) credit-risk-eval run-agent --trace-dir $(TRACE_DIR_AGENT)

# Score traces with the branch-correctness CheckSuite (+ Layer-2 judge when
# ANTHROPIC_API_KEY is set; + Braintrust when BRAINTRUST_API_KEY is set).
score:
	$(UV_RUN) credit-risk-eval score --trace-dir $(TRACE_DIR) --out-dir $(OUT_DIR)

# Score the Arm A agent traces (same suite, agent label + its own out dir).
score-agent:
	$(UV_RUN) credit-risk-eval score --trace-dir $(TRACE_DIR_AGENT) --out-dir $(OUT_DIR_AGENT) --label arm-a

# Run the Arm B Managed Agents investigation over all fixtures -> traces + metrics.
# Needs ANTHROPIC_API_KEY (the Managed Agents session) and COMPANIES_HOUSE_API_KEY.
run-agent-b:
	$(UV_RUN) credit-risk-eval run-agent-b --trace-dir $(TRACE_DIR_AGENT_B) --out-dir $(OUT_DIR_AGENT_B)

# Score the Arm B traces (SAME unchanged suite, arm-b label + its own out dir).
score-agent-b:
	$(UV_RUN) credit-risk-eval score --trace-dir $(TRACE_DIR_AGENT_B) --out-dir $(OUT_DIR_AGENT_B) --label arm-b

# Full local eval: baseline run then score. The baseline is EXPECTED to fail the
# multi-hop fixtures (that gap is the measure), so `score`'s non-zero exit here
# is the intended signal, not a build break — hence the leading `-`.
eval: run-baseline
	-$(MAKE) score

# Full Arm A eval: agent run then score. The agent is the arm that should WIN the
# multi-hop fixtures the baseline fails.
eval-agent: run-agent
	-$(MAKE) score-agent

# Full Arm B eval: Managed Agents run then score with the SAME suite.
eval-agent-b: run-agent-b
	-$(MAKE) score-agent-b

# The live 3-way build-vs-buy comparison: baseline vs Arm A vs Arm B. Runs and
# scores all three arms and writes the comparison table to out/compare/compare.md.
# Spend-aware: smoke first with `make compare COMPARE_ARGS="--limit 1"`.
COMPARE_ARGS ?=
compare:
	$(UV_RUN) credit-risk-eval compare --trace-root traces --out-dir out/compare $(COMPARE_ARGS)

secret-scan:
	gitleaks dir . --redact --no-banner --exit-code 1
