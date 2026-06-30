# credit-risk-monitoring

A credit-deterioration **monitoring agent** that investigates distressed issuers
by traversing a chain of primary sources — an SEC EDGAR filing names an exhibit,
the exhibit names a subsidiary, the subsidiary resolves at a foreign registry
(UK Companies House), the registry exposes a charge. The thesis the repo exists
to prove: a real credit signal is a *downstream* fact reachable only by walking
that chain, and the agent's value is **branch correctness** — taking the right
sources, in the right dependency order, to the right depth, and stopping.

## Status — what's built

This repo is delivered in workstreams:

| Workstream | Scope | State |
|---|---|---|
| Fixtures | 10 verified credit-deterioration fixtures (`fixtures/`) | done (branch `feat/eval-fixtures`) |
| C2 — eval surface + single-shot baseline | the floor | done |
| **C3a — Arm A agent** | the multi-hop investigation agent (`src/credit_risk_monitoring/agent/`) | **done (this branch)** |

**C2** built the evaluation surface the agent is graded on, plus a deliberately
weak single-shot baseline that establishes the floor. **C3a (this branch)** adds
**Arm A** — the raw-SDK multi-hop investigation agent that should *win* the
multi-hop fixtures the baseline fails. The run-trace format and the scorer are
agent-agnostic, so Arm A emits the **same** trace the baseline does and is scored
by the **same** `CheckSuite`, unchanged. (Arm B and the ACA/Terraform deploy are
out of scope for this branch.)

## What the eval measures

Each fixture (see `fixtures/README.md` for the schema and the downstream-question
design) carries a verified `ground_truth_path`, an `expected_stop_depth`, and an
`expected_answer`. A run is scored on three axes:

- **chain-correctness (Layer 1, deterministic).** Did the run traverse the right
  *source types* in the right dependency order? Scored as: the run's source chain
  is a non-empty order-preserving **prefix** of the expected chain. This isolates
  "every hop taken was the correct next source" from depth — a run that walked
  the right path but stopped short still passes *chain*; a wrong source type, or
  going *past* the chain, fails.
- **depth-correctness (Layer 1, deterministic).** Did it stop at
  `expected_stop_depth`? Equal → pass; **fewer** → fail (under-investigation);
  **more** → fail (over-investigation). For the controls/trap
  (`expected_stop_depth: 1`) any deeper traversal fails here — that is the
  over-investigation the controls exist to catch.
- **groundedness (Layer 2, LLM judge).** Does the final answer match the verified
  `expected_answer` without synthesising past the retrieved sources? A per-fixture
  `JudgeCriterion` carries that fixture's gold answer (and its verification
  caveats, so excluded/inferred facts aren't penalised or rewarded).

Layer 1 is the cheap, offline, gating signal (no API key); Layer 2 confirms the
answer quality and needs `ANTHROPIC_API_KEY`.

### How it's wired (shared packages, not re-implemented)

The eval is built **on** the portfolio's shared packages, consumed as pinned
git-subdirectory dependencies — never re-implemented locally:

- [`agent-evals`](https://github.com/claudeaiportfolio/ai-infra-templates)
  (`agent-evals-v0.2.0`) — the trace-driven framework. `evals/checks.py` extends
  its `CheckSuite` with the chain/depth checks (Layer 1); `evals/criteria.py`
  builds the groundedness `JudgeCriterion`s (Layer 2); its Braintrust adapter
  surfaces results.
- [`llm-provider`](https://github.com/claudeaiportfolio/ai-infra-templates)
  (`llm-provider-v0.2.0`) — the provider seam. The baseline's single completion
  goes through `get_provider()` (swap Claude/OpenAI with `LLM_PROVIDER`).
- [`agent-core`](https://github.com/claudeaiportfolio/ai-infra-templates)
  (`agent-core-v0.2.0`) — the agent runtime. Arm A's sub-agents run their
  multi-turn tool loop on `agent_core.AgentLoop` (routed through the
  `llm-provider` seam); the project supplies only the domain (tools, prompts,
  contracts, authz, audit, trace emission).

The run-trace format (`src/credit_risk_monitoring/trace.py`) emits exactly the
JSONL schema `agent-evals` consumes. **One retrieval hop == one `tool_use`
event whose tool name is the canonical source type**, so depth is the tool-call
count and the source chain is `tools_called` — no changes to the shared package,
and the C3 agent emits the identical format.

## The single-shot baseline

`src/credit_risk_monitoring/baseline.py` does, per fixture, exactly **one**
retrieval — the issuer's EDGAR recent-filings index (a real `data.sec.gov` call)
— then **one** `llm-provider` completion, and emits a trace. The single-retrieval
limit is the point: a filings-index glance is the right move for a *healthy*
issuer but cannot reach a depth-3/4 downstream registry fact. Measured locally:

| classification | n | baseline branch verdict |
|---|---|---|
| `single_hop_control` | 3 | **PASS** (index glance, no distress, stop) |
| `multi_hop` | 6 | **FAIL** (one hop can't reach the downstream fact) |
| `trap_control` | 1 | FAIL (chain) — an index glance is the wrong altitude for the trap's specific 8-K amendment |

That control-pass / multi-hop-fail gap is the honest floor the agent's win is
measured against.

## Arm A — the multi-hop investigation agent

`src/credit_risk_monitoring/agent/` is **Arm A**: a parent **orchestrator** that
delegates to the *minimum* set of sub-agents the investigation needs, each behind
a **typed Pydantic handoff contract** (`agent/contracts.py`). The loop is the
shared **`agent-core`** runtime routed through the **`llm-provider`** seam — real
multi-turn tool round-trips, plus a streamed synthesis — consumed as pinned
git-subdirectory deps, never re-implemented. **Agent model: `claude-opus-4-8`.**

### Why three sub-agents (and not more)

| Sub-agent | Role | Tools (scoped) | Handoff out |
|---|---|---|---|
| **exposure** | Work the SEC/EDGAR side: open the distress filing(s) + exhibits; extract named *downstream* references. | `edgar_submissions_index`, `edgar_filing`, `edgar_exhibit` | `ExposureFindings` |
| **entity_resolution** | Resolve one named reference at its external authority. | `companies_house`, `external_rating` | `RegistryFinding` |
| **synthesis** | Compose the grounded answer + stop decision. No retrieval. | *(none — streams)* | `FinalAnswer` |

The count maps to the investigation's actual structure, not an org chart:

1. **Two distinct retrieval *domains*** with different clients, auth, rate
   limits, and reasoning — US SEC (`data.sec.gov` / `www.sec.gov`, ~10 req/s,
   descriptive UA) versus an external *authority* (UK Companies House: Basic
   auth, 600/5min; or a rating agency). One sub-agent per domain.
2. **A synthesis step that must *not* retrieve** — the over-investigation guard
   the trap control exists to catch. Making it tool-less (and stream-only)
   structurally forbids it from adding a hop.

One sub-agent per domain also gives **least-privilege tool scoping**, which is
exactly what makes the per-sub-agent kill-switch tokens meaningful (below). A
fourth sub-agent would split one of these coherent roles for no branch-
correctness gain, so the count is held at three.

The orchestrator (`agent/orchestrator.py`) is a **deterministic** driver, not a
second LLM loop: the intelligence (which filing/exhibit to open, when to stop)
lives in the sub-agents' real loops; the orchestrator decides only the
*delegation sequence* from the typed contracts they return. That keeps the
control flow predictable, auditable, and unit-testable — the right posture for
an agent whose entire value is *branch correctness*.

### One trace, scored by the existing suite

Every retrieval hop — in whichever sub-agent — is recorded into a single C2
`TraceWriter` as one `tool_use` named by its canonical `SourceType`. So Arm A
emits the **exact** JSONL the baseline emits, and the existing
branch-correctness `CheckSuite` (chain + depth + the package universals) and the
Layer-2 groundedness judge score it **unchanged**. The goal: Arm A passes the
multi-hop fixtures (chain + depth + groundedness) where the single-shot baseline
fails, and still stops at depth 1 on the controls/trap.

### Production spines

- **EDGAR** (`sources.py`, extends C2's `EdgarClient`): submissions index ->
  filing document directory -> 8-K body -> exhibit, with the SEC `User-Agent`,
  ~10 req/s rate limiting, retry/backoff (honouring `Retry-After`), and
  submissions pagination for older filings.
- **Companies House** (`agent/companies_house.py`): HTTP Basic auth with the API
  key as username and a blank password (from `COMPANIES_HOUSE_API_KEY`), 600/5min
  rate limit, and paginated company record / officers / charges / filing history.
- **External rating** (`agent/rating.py`): authenticated provider lookup; the one
  *stated* demo simplification — rating-agency press bodies are paywalled, so
  without a configured provider the hop returns the rating as **UNVERIFIED**
  (never fabricated) while the production request path stays real.

### Runtime authz / kill-switch + audit

`agent/authz.py` mints a **short-TTL, scope-limited tool token** per sub-agent.
Every tool call is checked at the auth boundary (`agent/tools.py`) against the
broker — token scope + TTL, a global **deny-list**, and any **admin revoke** —
**on every call**, so a revoke or deny takes effect **mid-workflow**, on the next
call, not just at startup. The broker supports a one-call admin `revoke_agent`
and a `deny_tool` kill-switch. Every authorization decision, tool execution, and
revoke is written to an **audit log** (`agent/audit.py`: who / what / when /
revoked) — Postgres in production (`AUDIT_DATABASE_URL`, injected from Key Vault;
the connection is lazy so the build never depends on a live DB), in-memory
otherwise.

### Run it

```bash
make run-agent ENV_FILE=.env   # agent over all fixtures -> traces/agent
make score-agent ENV_FILE=.env # score with the SAME CheckSuite (+ Layer 2)
# or in one step:
make eval-agent ENV_FILE=.env
```

To run Arm A **live** you need `ANTHROPIC_API_KEY` (the agent loop + judge) and,
for the UK-registry hops, `COMPANIES_HOUSE_API_KEY`; `SEC_EDGAR_USER_AGENT` is
recommended (SEC may 403 a generic UA). `RATING_API_BASE`/`RATING_API_KEY` and
`AUDIT_DATABASE_URL` are optional (the rating hop degrades to UNVERIFIED and the
audit log falls back to in-memory without them). The offline unit tests cover the
orchestrator, both clients, the kill-switch, the audit log, and the full trace
round-trip through the suite — no keys or network required.

## Run it

```bash
make sync                 # uv sync --extra dev

# Offline: real EDGAR retrievals, deferred (stub) answers — Layer 1 fully scores.
make run-baseline
make score                # Layer 1 + branch-correctness verdict table

# Live: provide keys via an env file (loaded by `uv run --env-file`, never .env loading in code)
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
SEC_EDGAR_USER_AGENT=your-name your-email
# BRAINTRUST_API_KEY=...        # optional — activates the Braintrust surface
# COMPANIES_HOUSE_API_KEY=...   # used by the C3 agent's UK-registry hops, not C2
EOF
make eval ENV_FILE=.env   # run-baseline + score, with Layer-2 groundedness
```

Keys the eval reads (all from `os.environ`, via the invocation surface):

| Var | Used by | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | baseline + Arm A agent loop + Layer-2 judge | for live runs / Layer 2 |
| `SEC_EDGAR_USER_AGENT` | descriptive UA SEC requires | recommended (SEC may 403 a generic UA) |
| `COMPANIES_HOUSE_API_KEY` | Arm A UK-registry hops (Basic-auth username) | for live Arm A multi-hop UK fixtures |
| `AUDIT_DATABASE_URL` | Arm A audit-log Postgres sink | optional (in-memory fallback) |
| `RATING_API_BASE` / `RATING_API_KEY` | Arm A rating hop provider | optional (else UNVERIFIED) |
| `AGENT_MODEL` / `BASELINE_MODEL` / `JUDGE_MODEL` | model overrides (defaults: opus-4-8 / opus-4-8 / sonnet-4-6) | optional |
| `BRAINTRUST_API_KEY` | Braintrust surfacing (else silent no-op) | optional |

Without `ANTHROPIC_API_KEY` the baseline uses a clearly-labelled offline answerer
(stamped into the trace `note`) so Layer-1 branch-correctness still runs in
credential-less CI; Layer-2 is skipped with a note.

## Layout

```
src/credit_risk_monitoring/
  fixtures.py        # typed loader; derives each fixture's expected source chain
  sources.py         # SourceType taxonomy + classifier + EDGAR production spine
  trace.py           # agent-agnostic run-trace writer (agent-evals JSONL)
  baseline.py        # the deliberately single-retrieval baseline
  agent/             # Arm A — the multi-hop investigation agent
    contracts.py     # typed Pydantic handoff contracts between agents
    orchestrator.py  # parent: sequences typed delegations, emits the scored trace
    subagents.py     # exposure / entity_resolution / synthesis on agent-core
    tools.py         # the tool surface + the enforced auth boundary
    authz.py         # short-TTL scoped tool tokens, deny-list, admin revoke
    audit.py         # audit log: in-memory + Postgres sinks
    companies_house.py  # UK registry client (Basic auth, pagination, retries)
    rating.py        # external rating-agency hop (degrades to UNVERIFIED)
  evals/
    checks.py        # Layer-1 chain + depth checks; build_suite()
    criteria.py      # Layer-2 groundedness criteria (per-fixture gold answer)
    score.py         # load -> score -> report -> Braintrust fan-out
  cli.py             # `credit-risk-eval run-baseline | run-agent | score`
tests/               # loader, classifier, trace round-trip, checks, judge, harness,
                     # + EDGAR/CH clients, authz kill-switch, audit, orchestrator round-trip
fixtures/            # the 10-fixture bank (from feat/eval-fixtures)
Dockerfile           # Arm A agent container image (env-only config)
```

## CI

- `python-ci` — ruff + mypy + the offline unit tests on every PR/push.
- `secret-scan` — the shared org security action (blocking gitleaks + advisory
  Claude review) on every PR/push.
- `eval` — manual dispatch; live baseline run + score, uploads the report.
