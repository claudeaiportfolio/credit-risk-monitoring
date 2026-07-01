# credit-risk-monitoring

A credit-deterioration **monitoring agent** that investigates distressed issuers
by traversing a chain of primary sources — an SEC EDGAR filing names an exhibit,
the exhibit names a subsidiary, the subsidiary resolves at a foreign registry
(UK Companies House), the registry exposes a charge. The thesis the repo exists
to prove: a real credit signal is a *downstream* fact reachable only by walking
that chain, and the agent's value is **branch correctness** — taking the right
sources, in the right dependency order, to the right depth, and stopping.

## The build-vs-buy thesis and headline

This repo is a controlled **build-vs-buy** comparison. Once you accept that an
agent beats a single-shot baseline on this task, the real question is whether to
**run the agent loop yourself (build)** or **buy a managed multi-agent runtime
(buy)**. Three arms answer it, all scored the same way:

- **Baseline** — single-shot (one retrieval). The floor.
- **Arm A (build)** — a self-hosted raw-SDK multi-hop loop on `agent-core`.
- **Arm B (buy)** — the **same** investigation on **Anthropic Managed Agents**.

**Headline (measured, all 10 fixtures, one run):** build and buy **tie on task
quality** — both **7/10 branch-correct** on the **same** seven fixtures (same
wins, same residuals; Arm B edges groundedness 10/10 vs 9/10). They differ only
on **operational** axes: buy costs **~4x**, runs **~3.4x slower**, and is **ZDR /
HIPAA-ineligible** (Anthropic hosts the loop, so retrieved content flows through
Anthropic inference). Build wins cost / latency / residency; buy wins maintenance
(no loop or infra to run). Neither is universally better — it is a
residency / cost / latency vs maintenance trade.

➡️ **Full numbers, per-fixture verdicts, and the "when to choose" guidance:
[`SCORECARD.md`](SCORECARD.md)** (visual summary in
[`docs/build-vs-buy.md`](docs/build-vs-buy.md)).

## Status — what's built (all merged to `main`)

| Workstream | Scope | State |
|---|---|---|
| Fixtures | 10 verified credit-deterioration fixtures (`fixtures/`) | done |
| C2 — eval surface + single-shot baseline | the floor | done |
| C3a — Arm A agent (**build**) | the self-hosted multi-hop investigation agent (`src/credit_risk_monitoring/agent/`) | done |
| C3b — Arm A production hosting | Terraform: Azure Container Apps **Job** + Key Vault + private-endpoint Postgres audit sink (`terraform/`) | done — **deployed, smoke-passed, torn down** |
| C4 — Arm B agent (**buy**) | the SAME investigation on **Anthropic Managed Agents** (`src/credit_risk_monitoring/agent_managed/`) | done |
| Capstone | build-vs-buy scorecard + this README | done ([`SCORECARD.md`](SCORECARD.md)) |

**C2** built the evaluation surface the agent is graded on, plus a deliberately
weak single-shot baseline that establishes the floor. **C3a** added **Arm A** —
the *self-hosted* raw-SDK multi-hop investigation agent (the **build** side) —
and **C3b** took it to a real **Azure Container Apps** deployment (see
[ACA production deployment](#arm-a-production-deployment-aca)). **C4** added
**Arm B** — the **same** multi-hop investigation on **Anthropic Managed Agents**
(the **buy** side): a coordinator + sub-agent roster where Anthropic runs the
multi-agent loop and hosts tool execution. The run-trace format and the scorer
are agent-agnostic, so all three arms — baseline, Arm A, Arm B — emit the
**same** trace and are scored by the **same** `CheckSuite`, unchanged, which is
what makes the build-vs-buy comparison apples-to-apples. A **self-hosted Managed
Agents sandbox** was **evaluated and rejected** (it recovers no residency for
this workload — see [that finding](#self-hosted-managed-agents-sandbox--rejected)).

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

## Arm A production deployment (ACA)

Arm A is the **build** side, so it is taken all the way to a real cloud
deployment. `terraform/` provisions Arm A onto **Azure Container Apps as a Job**
(`workload_kind = "job"`, manual on-demand trigger), VNet-integrated on an
internal environment with **no public ingress**, its secrets read from this
solution's **own Key Vault** via a **user-assigned managed identity**, and its
audit log written to a **private-endpoint Postgres** sink. Per portfolio
convention the dir holds only module *invocations* + wiring; the module bodies
(`postgres`, `aca`) live in `portfolio-infra` and are consumed at pinned
`tf-modules-vX.Y.Z` refs.

**A Job, not a Container App, on purpose.** Arm A is *episodic*: the entrypoint
(`credit-risk-eval run-agent`) runs one investigation to completion and exits. A
scale-to-zero Container App with no ingress would deploy green but never wake — a
facade. A Job with a manual trigger is the correct primitive and lets an operator
start a real run with `az containerapp job start`.

**Deployment status: applied, smoke-tested, torn down.** The stack was deployed
live to Azure, an on-demand job execution was started and **ran to completion
(Succeeded)** — reading its Key Vault secrets and reaching Postgres over the
private endpoint — and then torn down via `make teardown-full` (which deletes
only this solution's *own* resource group; this piece uses **no AKS**, so there
is no shared cluster to stop). Design decisions and documented deviations (secret
values passing through Terraform state; KV public-network-access default;
plain UAMI vs the shared `identity` module) are recorded in
[`terraform/README.md`](terraform/README.md), which also carries the full deploy
runbook.

## Arm B — the same investigation on Anthropic Managed Agents (build-vs-buy)

`src/credit_risk_monitoring/agent_managed/` is **Arm B**: the **same** multi-hop
investigation as Arm A, but on **Anthropic Managed Agents (multi-agent)** instead
of a self-hosted `agent-core` loop. Arm A is the **build** side (you run the loop
and host tool execution — deployable to ACA); Arm B is the **buy** side
(Anthropic runs the multi-agent loop and provisions the per-session container).
Everything the comparison must hold constant is held constant: the same
three-role decomposition, the **exact** Arm A tools (reused, not re-implemented),
the same scored trace, the same unchanged `CheckSuite`, and the same models
(coordinator + sub-agents `claude-opus-4-8`; judge `claude-sonnet-4-6`).

### Architecture — a coordinator + a two-sub-agent roster

Managed Agents is **persisted + versioned**: you create an **environment** and an
**agent roster** once (the control plane, `agent_managed/roster.py`), store the
IDs, and reference them on every **session** (the data plane,
`agent_managed/orchestrator.py`) — never creating agents in the request path. The
roster:

| Agent | Role | Tools (custom) |
|---|---|---|
| **coordinator** | delegates to the sub-agents **and** synthesizes the grounded answer; has **no retrieval tools** | *(none)* |
| **exposure** | SEC/EDGAR retrieval domain (identical role/prompt/scope as Arm A's exposure) | `edgar_submissions_index`, `edgar_filing`, `edgar_exhibit` |
| **entity_resolution** | external-authority retrieval domain (identical role/prompt/scope as Arm A's) | `companies_house`, `external_rating` |

**Roster count — derived from the fixtures, justified to the same standard as
Arm A.** Arm A justifies *three* sub-agents: two retrieval *domains* (US SEC vs.
an external authority — different clients, auth, rate limits) plus a synthesis
step that must *not* retrieve (the over-investigation guard the trap control
catches). Managed Agents changes the arithmetic in exactly one place: **the
coordinator is itself a tool-less LLM** that receives the sub-agents' results and
composes the answer — so it *natively fills the synthesis role*, and, having no
retrieval tools, it *structurally cannot add a hop* (the same guard Arm A
implemented as a separate tool-less `synthesis` sub-agent). The minimum roster is
therefore **two sub-agents under one coordinator**: MA collapses Arm A's
{deterministic orchestrator + tool-less synthesis sub-agent} into the single
coordinator (in MA the orchestrator is an LLM, not code you write). A separate
`synthesis` sub-agent would be a redundant extra `opus-4-8` thread for zero
branch-correctness gain, so it is folded in. The two retrieval domains stay
distinct sub-agents for the same reasons Arm A gives.

### Tool exposure — custom tools, host-side (and its residency implication)

The tools are exposed as **custom tools executed host-side** over the SSE stream
(Managed Agents' custom-tool pattern), **not** a sandbox code-execution pip shim
and **not** a remote MCP server. The custom-tool handlers **are Arm A's own
`TOOL_REGISTRY` handlers** (the EDGAR spine + the Companies House / rating
clients), imported and called directly — the strongest satisfaction of the
"reuse the tool code / same tool surface both arms" bar and the portfolio
centralise rule. When a sub-agent emits `agent.custom_tool_use`, the orchestrator
(holding the SSE stream open with the Anthropic API key) runs the fetch locally
and answers with `user.custom_tool_result`: **no new public ingress** is stood up
(the orchestrator is a client, not a server), and **secrets stay host-side**
(`COMPANIES_HOUSE_API_KEY` / `SEC_EDGAR_USER_AGENT` never leave the process; no
vault needed). The MA sandbox container is not used for retrieval at all.

**Residency implication (a comparison data point, not a blocker).** Custom tools
keep the *fetch and the secrets* host-side, but the *fetched filing content*
still flows through Anthropic: the tool **result** (the 8-K body, the exhibit
text, the registry record) is returned to Anthropic's orchestration layer, where
the coordinator and sub-agent `opus-4-8` models reason over it. Because Managed
Agents runs the loop, the hosted model-inference layer necessarily sees the
retrieved primary-source content — this is the concrete point on which Managed
Agents is **ZDR / HIPAA-ineligible**, and it holds for *any* MA tool-exposure
choice (the model inference is unavoidable under MA; the custom-tools choice just
avoids *also* running the fetch on Anthropic infra). Arm A, running the loop
self-hosted, keeps both the fetch and the model-context content on infrastructure
the operator controls.

### One trace, scored by the same suite

Every retrieval hop — from whichever sub-agent thread, cross-posted to the
primary session stream — is recorded into the **same C2 `TraceWriter`** as one
`tool_use` named by its canonical `SourceType`. So Arm B emits the **exact** JSONL
Arm A and the baseline emit, and the existing branch-correctness `CheckSuite`
(chain + depth) and the Layer-2 groundedness judge score it **unchanged**. Token
usage is read authoritatively from `sessions.retrieve().usage` (which aggregates
all coordinator + sub-agent threads and includes prompt-cache read/write — MA
auto-enables caching); latency is wall-clock; cost is priced at `opus-4-8`.

### The live 3-way comparison

`make compare` (or `credit-risk-eval compare`) runs and scores all three arms and
writes the table to `out/compare/compare.md`. Each per-fixture cell is
`chain / depth / groundedness` — `OK`/`X` for the Layer-1 chain & depth checks and
the Layer-2 groundedness score `n/3`.

<!-- COMPARE_TABLE_START -->
Measured live over all 10 fixtures (coordinator + sub-agents `claude-opus-4-8`;
judge `claude-sonnet-4-6`; regenerate with `make compare`):

| fixture | class | exp. depth | baseline | arm-a | arm-b |
| --- | --- | --- | --- | --- | --- |
| valaris-uk-subsidiary-charge | multi_hop | 4 | X/X/0/3 (d1) | OK/OK/3/3 (d4) | OK/OK/2/3 (d4) |
| hertz-uk-receivables-charge-holder | multi_hop | 4 | X/X/0/3 (d1) | OK/OK/3/3 (d4) | OK/OK/3/3 (d4) |
| revlon-elizabeth-arden-uk-charge | multi_hop | 4 | X/X/0/3 (d1) | OK/OK/3/3 (d4) | OK/OK/3/3 (d4) |
| bbby-default-to-sp-rating | multi_hop | 3 | X/X/0/3 (d1) | X/X/0/3 (d8) | X/X/2/3 (d6) |
| greenrose-foreclosure-recipient-and-liquidation | multi_hop | 4 | X/X/0/3 (d1) | X/X/3/3 (d3) | X/X/3/3 (d2) |
| wejo-uk-administration | multi_hop | 3 | X/X/1/3 (d1) | X/X/2/3 (d2) | X/X/3/3 (d4) |
| control-microsoft | single_hop_control | 1 | OK/OK/3/3 (d1) | OK/OK/3/3 (d1) | OK/OK/3/3 (d1) |
| control-johnson-and-johnson | single_hop_control | 1 | OK/OK/2/3 (d1) | OK/OK/3/3 (d1) | OK/OK/3/3 (d1) |
| control-costco | single_hop_control | 1 | OK/OK/2/3 (d1) | OK/OK/3/3 (d1) | OK/OK/3/3 (d1) |
| trap-toll-brothers-benign-amendment | trap_control | 1 | X/OK/2/3 (d1) | OK/OK/3/3 (d1) | OK/OK/3/3 (d1) |

| arm | branch-correct (chain+depth) | grounded (>=2/3) | input tok\* | output tok | cost $ | wall-clock s | mean latency s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 3/10 | 4/10 | 12,839 | 8,750 | $0.28 | 135 | 13.5 |
| arm-a (self-hosted loop) | 7/10 | 9/10 | 98,164 | 13,720 | $0.83 | 287 | 28.7 |
| arm-b (Managed Agents) | 7/10 | 10/10 | 842,089 | 74,462 | $3.33 | 963 | 96.3 |

\* *input tok* = total input-side tokens (uncached input + prompt-cache read +
write). Managed Agents (Arm B) auto-enables prompt caching, so most of its input
is cache-read (priced ~0.1x); the baseline caches nothing and Arm A's trace
carries no cache split — so **`cost $` is the most directly comparable spend
metric** (each arm's tokens priced at `opus-4-8` rates, incl. Arm B's cache
read/write). Cell = `chain / depth / groundedness (depth reached)`.

**Read of the result.** Branch correctness is a **tie: both agents 7/10 on the
same seven fixtures** — both win the three UK-registry multi-hops (valaris,
hertz, revlon) at the correct depth 4, both stop at depth 1 on the three controls
and the trap, and both miss the same three harder US-only / discovery chains
(bbby, greenrose, wejo — over- or under-shooting depth identically). Arm B edges
groundedness (10/10 vs 9/10). That branch parity is the point: **the managed
multi-agent loop reproduces the self-hosted loop's investigative discipline on
the identical tool surface** — the arms differ on the *operational* axes, not the
answer. Arm B costs **~4x** Arm A ($3.33 vs $0.83, driven by ~5.4x the output
tokens — the coordinator + two sub-agent threads each hold their own context) and
runs **~3.4x slower** (963s vs 287s wall-clock — the managed round-trip +
fan-out): the price of not running the loop yourself.
<!-- COMPARE_TABLE_END -->

All three arms are surfaced to **Braintrust** (project `agent-evals`, run labels
`baseline` / `arm-a` / `arm-b`):
<https://www.braintrust.dev/app/aiportfolio/p/agent-evals>.

### Build-vs-buy observations (consolidated in [`SCORECARD.md`](SCORECARD.md))

- **Maintenance** — Arm B is *no loop and no infra to run*: no `agent-core`
  loop, no ACA service, no container lifecycle — Anthropic runs the multi-agent
  loop and provisions the sandbox. Arm A owns all of that (the loop code + the
  ACA deploy). The Arm B code is the roster definition + a thin SSE event driver.
- **Residency** — Arm B's Anthropic-hosted model-inference layer sees the
  retrieved filing content → **ZDR / HIPAA-ineligible** (see above). Arm A keeps
  the loop and the retrieved content on operator-controlled infrastructure.
- **Price** — see the per-arm `cost $` column. Both arms run `opus-4-8`; the
  multi-agent fan-out (coordinator + two sub-agent threads, each its own context)
  makes Arm B's token/$ profile visibly heavier than Arm A's single-loop
  delegation, partly offset by MA's automatic prompt caching (most input is
  cache-read).
- **Performance** — see `wall-clock s` / branch-correctness. Both arms should
  win the multi-hop fixtures the baseline fails and stop at depth 1 on the
  controls/trap; the latency delta is the managed round-trip + fan-out.

### Self-hosted Managed Agents sandbox — rejected

A self-hosted Managed Agents sandbox (`config.type=self_hosted`) was
**evaluated and deliberately not built** — a documented finding, not a gap. It
only moves **built-in tool execution** (bash / file / code) onto your infra; the
**agent loop and model inference stay on Anthropic**. Arm B already runs its
retrieval as **custom tools host-side**, so the sandbox is not used for retrieval
at all — there is nothing to relocate. Self-hosting it therefore recovers **no
residency** (the retrieved content still flows through Anthropic inference, so
still ZDR / HIPAA-ineligible), gains **no measured performance**, and pushes
**maintenance back to you**. Conclusion: **complexity with no value for this
workload.** A full third eval on it would be misleading — same loop → ~identical
task results plus our-side latency — so the honest deliverable is the documented
rejection, not a fabricated fourth column. (See `agent_managed/roster.py` and the
[scorecard](SCORECARD.md#self-hosted-managed-agents-sandbox--evaluated-and-rejected).)

### Managed Agents limitations / surprises hit

- **`session.usage` reports mostly cache, not raw input.** With prompt caching
  auto-enabled, `usage.input_tokens` is only the *uncached* remainder (tiny); the
  real prompt volume is under `cache_read_input_tokens` + a **nested**
  `cache_creation` dict (`ephemeral_5m_input_tokens`). The token accounting sums
  all of these (see `_usage_tokens`).
- **The primary session stream is condensed for sub-agents.** Summing
  `span.model_request_end` off the primary stream under-counts sub-agent thread
  tokens — `sessions.retrieve().usage` is the authoritative aggregate.
- **The coordinator narrates.** `opus-4-8` tends to narrate its delegation; the
  coordinator prompt explicitly forces the final message to be a clean,
  self-contained answer with no protocol meta-commentary.

### Run it

```bash
# Arm B alone (Managed Agents): needs ANTHROPIC_API_KEY + COMPANIES_HOUSE_API_KEY.
make run-agent-b ENV_FILE=.env    # -> traces/agent-b + out/agent-b/metrics.json
make score-agent-b ENV_FILE=.env  # score with the SAME CheckSuite (+ Layer 2)

# The full live 3-way comparison (baseline vs Arm A vs Arm B):
make compare ENV_FILE=.env                              # all 10 fixtures
make compare ENV_FILE=.env COMPARE_ARGS="--limit 1"    # spend-aware smoke first
```

The agent roster is created once per run and reused across all fixtures; set
`ARM_B_ENV_ID` / `ARM_B_COORDINATOR_ID` / `ARM_B_EXPOSURE_ID` /
`ARM_B_ENTITY_RESOLUTION_ID` to reuse a previously-created roster across runs.
Offline unit tests (`tests/test_agent_managed.py`) drive a scripted fake Managed
Agents client + mocked EDGAR/CH, asserting the emitted trace scores through the
unchanged suite — no keys or network required.

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
| `ANTHROPIC_API_KEY` | baseline + Arm A agent loop + Arm B Managed Agents session + Layer-2 judge | for live runs / Layer 2 |
| `SEC_EDGAR_USER_AGENT` | descriptive UA SEC requires | recommended (SEC may 403 a generic UA) |
| `COMPANIES_HOUSE_API_KEY` | Arm A + Arm B UK-registry hops (Basic-auth username) | for live multi-hop UK fixtures |
| `AUDIT_DATABASE_URL` | Arm A audit-log Postgres sink | optional (in-memory fallback) |
| `RATING_API_BASE` / `RATING_API_KEY` | Arm A + Arm B rating hop provider | optional (else UNVERIFIED) |
| `AGENT_MODEL` / `ARM_B_MODEL` / `BASELINE_MODEL` / `JUDGE_MODEL` | model overrides (defaults: opus-4-8 / opus-4-8 / opus-4-8 / sonnet-4-6) | optional |
| `ARM_B_ENV_ID` / `ARM_B_COORDINATOR_ID` / `ARM_B_EXPOSURE_ID` / `ARM_B_ENTITY_RESOLUTION_ID` | reuse a previously-created Arm B roster across runs | optional (else created once per run) |
| `BRAINTRUST_API_KEY` | Braintrust surfacing (else silent no-op) | optional |

Without `ANTHROPIC_API_KEY` the baseline uses a clearly-labelled offline answerer
(stamped into the trace `note`) so Layer-1 branch-correctness still runs in
credential-less CI; Layer-2 is skipped with a note.

## Residency & compliance posture

The headline judgment of this piece is that **residency is an architected
choice**, not an afterthought (full treatment in
[`SCORECARD.md`](SCORECARD.md#residency-as-an-architected-choice-the-fde-judgment)):

- **Persisted state is public data only.** The one durable store is the Postgres
  **audit sink** (`agent/audit.py`), which records *operational metadata*
  (`agent_id` / `action` / `tool` / `allowed` / `revoked` / `ts`) over retrievals
  of **public** sources — never the retrieved content, and no standing watchlist.
- **The sensitive input is transient.** Which issuers you monitor / the specific
  question is supplied **per run** and is not persisted agent-side (here that
  input is a fixture `question`; a production deployment passes a watchlist item
  the same transient way).
- **The managed arm's residency line.** Arm B (Managed Agents) runs the loop on
  **Anthropic-side inference**, so retrieved content flows through Anthropic →
  **not ZDR / HIPAA-eligible**. For *this public-data* workload that trade can go
  toward buy; for a **private-data variant** it disqualifies Arm B and residency
  forces build. The honest headline: *managed lowers engineering cost but raises
  governance cost.*
- **Compliance posture (awareness-only, not a certification):** workload identity
  → Key Vault secret refs (no secrets in code or image; `terraform/`); audit
  logging on **every** agent/tool/authz action, which is what makes the
  **kill-switch auditable** (revokes are checked on every call and recorded).

**Scoped deviations from the scoping doc** (documented, not papered over):

- Arm B exposes tools as **host-side custom tools** (Arm A's own `TOOL_REGISTRY`
  over the session event stream), **not an MCP connector** — reasoned: no new
  public ingress + an identical tool surface across both arms.
- This piece does **live-API investigation** over EDGAR / Companies House rather
  than reusing piece 1's hybrid + rerank corpus retrieval — reasoned: the task
  shape is **API navigation** (traverse live registries in dependency order), not
  corpus search over a fixed document set.

## Honest limitations

Stated plainly so nothing here reads as more than it is (full detail in
[`SCORECARD.md`](SCORECARD.md)):

- **S&P rating hop is UNVERIFIED (bbby).** Rating-agency press bodies are
  paywalled (403), so without a configured provider the rating hop returns
  **UNVERIFIED** rather than a fabricated value. The production request path in
  `agent/rating.py` is real; only the demo default degrades. This is why both
  agents miss the bbby fixture.
- **valaris is a win but pre-named.** Its restructuring support agreement lists
  ~15 UK debtors (≥4 with 2020 charges), so no unique property identifies the
  target without naming it. valaris exercises the exhibit → registry → charges
  chain but, unlike revlon/hertz, it does **not** test *discovery*. Documented,
  not hidden.
- **greenrose / wejo fail on path shape, not correctness.** Both agents reach the
  *correct grounded answer* but via a *shorter* path than the ground-truth model,
  so they fail the chain/depth check by design (the scorer grades the branch, not
  just the final answer).
- **The 3-way table is one run, not an averaged distribution.** LLM outputs
  vary; the numbers are a single measured session (`make compare`), surfaced to
  Braintrust, not a mean over seeds. Treat magnitudes (~4x cost, ~3.4x latency,
  the branch-correct tie) as the signal, not the last digit.
- **Clean stand-in corpus.** The fixtures are a curated bank of verified public
  disclosures — a deliberately clean corpus, not messy production inputs. The
  retrieval/agent *paths* are production-real (real EDGAR + Companies House
  clients, rate limits, retries, auth); the corpus is the stated simplification.
- **Self-hosted MA sandbox is a reasoned rejection, not a measured third build**
  (see [above](#self-hosted-managed-agents-sandbox--rejected)).

## Layout

```
src/credit_risk_monitoring/
  fixtures.py        # typed loader; derives each fixture's expected source chain
  sources.py         # SourceType taxonomy + classifier + EDGAR production spine
  trace.py           # agent-agnostic run-trace writer (agent-evals JSONL)
  baseline.py        # the deliberately single-retrieval baseline
  agent/             # Arm A (build) — the self-hosted multi-hop investigation agent
    contracts.py     # typed Pydantic handoff contracts between agents
    orchestrator.py  # parent: sequences typed delegations, emits the scored trace
    subagents.py     # exposure / entity_resolution / synthesis on agent-core
    tools.py         # the tool surface + the enforced auth boundary
    authz.py         # short-TTL scoped tool tokens, deny-list, admin revoke
    audit.py         # audit log: in-memory + Postgres sinks
    companies_house.py  # UK registry client (Basic auth, pagination, retries)
    rating.py        # external rating-agency hop (degrades to UNVERIFIED)
  agent_managed/     # Arm B (buy) — the SAME investigation on Managed Agents
    roster.py        # control plane: environment + coordinator/exposure/resolution agents
    orchestrator.py  # data plane: session + SSE loop; reuses Arm A tools as custom tools
    metrics.py       # per-arm tokens / $ cost / latency capture (opus-4-8 pricing)
    compare.py       # the live 3-way build-vs-buy table + Braintrust fan-out
  evals/
    checks.py        # Layer-1 chain + depth checks; build_suite()
    criteria.py      # Layer-2 groundedness criteria (per-fixture gold answer)
    score.py         # load -> score -> report -> Braintrust fan-out
  cli.py             # `credit-risk-eval run-baseline | run-agent | run-agent-b | score | compare`
tests/               # loader, classifier, trace round-trip, checks, judge, harness,
                     # + EDGAR/CH clients, authz kill-switch, audit, orchestrator round-trip,
                     # + Arm B (fake Managed Agents client, roster, trace scoring, cost)
fixtures/            # the 10-fixture bank
terraform/           # Arm A (build) production hosting: ACA Job + KV + private-endpoint Postgres
Dockerfile           # Arm A agent container image (env-only config)
SCORECARD.md         # the build-vs-buy scorecard (headline result)
docs/build-vs-buy.md # visual summary (arm architecture + cost/latency/quality charts)
```

## CI

- `python-ci` — ruff + mypy + the offline unit tests on every PR/push.
- `secret-scan` — the shared org security action (blocking gitleaks + advisory
  Claude review) on every PR/push.
- `eval` — manual dispatch; live baseline run + score, uploads the report.
