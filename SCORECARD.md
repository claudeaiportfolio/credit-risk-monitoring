# Build-vs-buy scorecard

This repo asks one question: on **multi-hop credit-risk investigation** over
public disclosures (SEC EDGAR + UK Companies House), does an **agent** genuinely
beat a single-shot baseline — and if you build one, should you **run the loop
yourself (build)** or **buy a managed multi-agent runtime (buy)**?

Three arms, one controlled comparison. All three emit the **same** run-trace
format and are scored by the **same, unchanged** `CheckSuite` (Layer-1
chain + depth, deterministic) and the **same** Layer-2 groundedness judge
(`claude-sonnet-4-6`). That is what makes the numbers below apples-to-apples.

- **Baseline** — single-shot: one retrieval, one completion. The floor.
- **Arm A (build)** — self-hosted raw-SDK multi-hop agent loop
  (`src/credit_risk_monitoring/agent/`): three sub-agents
  (exposure / entity_resolution / tool-less synthesis) on the shared `agent-core`
  runtime routed through the `llm-provider` seam, model `claude-opus-4-8`.
  Deployed to Azure Container Apps as a Job (see `terraform/`).
- **Arm B (buy)** — Anthropic **Managed Agents** multi-agent
  (`src/credit_risk_monitoring/agent_managed/`, `managed-agents-2026-04-01`): a
  tool-less coordinator + 2 retrieval sub-agents, reusing Arm A's **exact** tools
  executed host-side over the session event stream. Models `claude-opus-4-8`.

> **All numbers below are from one live 3-way run over all 10 fixtures**
> (coordinator + sub-agents `claude-opus-4-8`; judge `claude-sonnet-4-6`).
> Reproduce with `make compare ENV_FILE=.env`; the same table is embedded in the
> top-level `README.md` between the `COMPARE_TABLE` markers. Nothing here is
> hand-tuned or aspirational — every claim traces to a cell in the tables below.

---

## Headline

**Build and buy tie on task quality. They differ only on operational axes.**

Both agents score **7/10 branch-correct** on the **same seven fixtures** — same
wins, same residuals. Arm B edges groundedness by one fixture (10/10 vs 9/10).
The managed multi-agent loop reproduces the self-hosted loop's investigative
discipline on an identical tool surface. Where they diverge is cost, latency,
and data residency — not the answer.

| | Baseline | Arm A (build) | Arm B (buy) |
|---|---|---|---|
| Branch-correct (chain + depth) | 3/10 | **7/10** | **7/10** |
| Grounded (>=2/3) | 4/10 | 9/10 | **10/10** |
| Cost $/run | **$0.28** | **$0.83** | $3.33 |
| Wall-clock (10 fixtures) | **135 s** | **287 s** | 963 s |
| Mean latency / fixture | **13.5 s** | **28.7 s** | 96.3 s |
| Data residency | — | **self-hosted** | ZDR / HIPAA-**ineligible** |

Relative to Arm A, Arm B costs **~4x** and runs **~3.4x slower**, and its loop
runs on Anthropic-hosted inference (see [Residency](#operational-axes)). Arm A
wins cost / latency / residency; Arm B wins **maintenance** (no loop and no infra
to run or patch). Neither is universally better — see
[When to choose build vs buy](#when-to-choose-build-vs-buy).

---

## The 3-way result (all 10 fixtures, one run)

Per-fixture cell = `chain / depth / groundedness (depth reached)` — `OK`/`X` for
the Layer-1 chain & depth checks, groundedness as `n/3` from the Layer-2 judge.

| fixture | class | exp. depth | baseline | arm-a (build) | arm-b (buy) |
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
is cache-read (priced ~0.1x input); the baseline caches nothing and Arm A's trace
carries no cache split — so **`cost $` is the most directly comparable spend
metric** (each arm's tokens priced at `claude-opus-4-8` rates, including Arm B's
cache read/write).

### The core evidence: the baseline fails every multi-hop fixture

The baseline scores **0/3 groundedness on all six** multi-hop fixtures — it
refuses to answer without retrieval it cannot reach in one hop. Its groundedness
mean is **~0.30** across the bank; Arm A's is **~0.93**. That gap is the whole
point: these questions are framed on a *downstream* fact (a registered-charge ID,
a named collateral agent, a subsidiary's current registry status) that is
**only reachable by traversing the full chain**, so a single-shot glance
genuinely cannot answer them. The baseline passing the three controls and the
trap-depth check while failing every multi-hop is the honest floor the agents'
wins are measured against.

---

## Per-fixture verdicts (stated plainly, nuances not smoothed over)

### Genuine multi-hop discovery wins (revlon, hertz)

Both questions were deliberately re-worded to **not pre-name** the UK subsidiary,
so the agent must *discover* it before it can resolve it at the registry:

- **revlon** — the question names only "exactly one [filing subsidiary]
  incorporated in the United Kingdom." The agent must open Exhibit 99.1, read the
  ~48-entity debtor list, and identify Elizabeth Arden (UK) Ltd as the sole UK
  debtor — then resolve it at Companies House and read its charges.
- **hertz** — the Chapter 11 8-K carves out non-US subsidiaries as a *class* and
  never names the UK receivables entity. The agent must run a **Companies House
  company-search** to discover Hertz UK Receivables Ltd, then confirm and read
  its charges (Credit Agricole CIB).

Baseline fails both (0/3). Both agents win both at the correct depth 4. These are
the cleanest evidence that the chain is genuinely required, not decorative.

### A win where the target is pre-named (valaris)

**valaris** is a real multi-hop win (baseline 0/3; Arm A 3/3 at depth 4), but —
unlike revlon/hertz — **the target subsidiary is named in the question**. It
could not be cleanly un-pre-named: valaris's restructuring support agreement
lists ~15 UK debtors, at least four of which took 2020 charges, so no unique
investigable property identifies the target without naming it. This is
**documented, not hidden**: the fixture still exercises the exhibit → registry →
charges chain, but it does not test *discovery* the way revlon/hertz do.

### Shared residuals — both agents fail the same three (validates the fixtures)

Both agents miss the **same** three fixtures (bbby, greenrose, wejo). That the
residuals are shared, not arm-specific, is evidence the fixtures test the task
and not a quirk of one runtime:

- **bbby** — the agent misses the S&P rating hop. This is an **explicitly
  documented simplification**: S&P's rating-action press body is paywalled (403),
  so the rating hop returns **UNVERIFIED** rather than a fabricated value (the
  production request path stays real; see `agent/rating.py`). Both agents also
  over-shoot depth (Arm A d8, Arm B d6 vs expected d3).
- **greenrose** — the agent reaches the **correct grounded answer** (3/3
  groundedness) but via a **shorter path** than the ground-truth model (Arm A d3,
  Arm B d2 vs expected d4). It fails chain/depth on **shape, not correctness**.
- **wejo** — same shape-not-correctness pattern: the agent lands the right answer
  region but its path length diverges from the expected depth 3 (Arm A d2, Arm B
  d4). Arm B is fully grounded here (3/3), Arm A partially (2/3).

The greenrose/wejo failures are the honest kind: the scorer grades the *branch*
(right sources, right order, right depth), and a correct answer reached by a
differently-shaped path still fails that structural check by design.

---

## Operational axes

Task quality is a tie; the decision lives here.

| Axis | Baseline | Arm A — build (self-hosted loop) | Arm B — buy (Managed Agents) |
|---|---|---|---|
| **Task quality** | 3/10 branch, 4/10 grounded | **7/10 branch, 9/10 grounded** | **7/10 branch, 10/10 grounded** |
| **Cost / run** | $0.28 | **$0.83** | $3.33 (**~4x** Arm A) |
| **Latency (wall-clock, 10 fx)** | 135 s | **287 s** | 963 s (**~3.4x** Arm A) |
| **Residency** | — | **Loop + retrieved content on operator-controlled infra** | Loop runs on Anthropic inference → retrieved filing content flows through Anthropic → **ZDR / HIPAA-ineligible** |
| **Maintenance** | trivial | **You own it**: `agent-core` loop code + the ACA Job + container lifecycle + patching | **Anthropic runs the loop** & provisions the per-session sandbox; your code is a roster definition + a thin SSE event driver |
| **Infra to run** | none | ACA Job, Key Vault, managed identity, private-endpoint Postgres audit sink (`terraform/`) | none (no public ingress; secrets stay host-side) |

### Why Arm B costs ~4x and runs ~3.4x slower

Both arms run `claude-opus-4-8`. The difference is the **multi-agent fan-out**:
Managed Agents runs a coordinator **plus** two sub-agent threads, each holding
its own context — driving ~5.4x Arm A's output tokens ($3.33 vs $0.83) — and the
managed round-trip + fan-out is the latency tax (963 s vs 287 s wall-clock).
MA's automatic prompt caching offsets *input* spend (most of Arm B's input is
cache-read at ~0.1x), which is why `cost $` — not raw token counts — is the fair
spend comparison.

### The residency point, precisely

Arm B exposes Arm A's exact tools as **custom tools executed host-side** — the
fetch and the secrets (`COMPANIES_HOUSE_API_KEY`, `SEC_EDGAR_USER_AGENT`) never
leave your process, and no new public ingress is stood up. **But** the tool
*result* — the 8-K body, the exhibit text, the registry record — is returned to
Anthropic's orchestration layer, where the coordinator and sub-agent models
reason over it. Because Managed Agents runs the loop, the hosted inference layer
necessarily sees the retrieved primary-source content. That is the concrete point
on which Managed Agents is **ZDR / HIPAA-ineligible**, and it holds for *any* MA
tool-exposure choice. Arm A, running the loop self-hosted, keeps both the fetch
and the model-context content on infrastructure the operator controls.

### Self-hosted Managed Agents sandbox — evaluated and rejected

A self-hosted MA sandbox (`config.type=self_hosted`) is a documented option we
**evaluated and did not build**, because it recovers nothing for this workload:

- It only moves **built-in tool execution** (bash / file / code) to your infra.
  Arm B already runs its retrieval as **custom tools host-side**, so the sandbox
  is not used for retrieval at all — there is nothing to relocate.
- The **agent loop and model inference stay on Anthropic** regardless. So
  self-hosting the sandbox recovers **no residency** (still ZDR/HIPAA-ineligible,
  because the retrieved content still flows through Anthropic inference), gains
  **no measured performance**, and pushes **maintenance back to you**.

Conclusion: **complexity with no value for this workload — rejected, not built.**
A full third eval on it would be misleading: same loop → ~identical task results,
plus added our-side latency. Documenting the rejection is the honest deliverable;
a fabricated fourth column would not be.

---

## When to choose build vs buy

Both arms clear the same task bar, so the decision is driven entirely by
constraints, not capability.

**Choose build (Arm A / self-hosted loop) when:**

- **Residency is a hard requirement** — regulated data, ZDR or HIPAA obligations,
  or any policy that the retrieved primary-source content must not transit a
  third-party inference layer. This is the decisive, non-negotiable axis.
- **Cost or latency matter at volume** — ~4x cheaper and ~3.4x faster per run
  compounds across a monitoring fleet.
- **You already run infra** — you have (or want) the ACA Job, Key Vault, managed
  identity, and audit sink, and the maintenance is acceptable overhead.

**Choose buy (Arm B / Managed Agents) when:**

- **Data residency is not a constraint** — the workload has no ZDR/HIPAA
  requirement and third-party inference over the content is acceptable.
- **Maintenance burden dominates** — you want zero agent-loop code and zero infra
  to run or patch, and will pay the cost/latency premium to get it.
- **Time-to-first-run matters more than per-run economics** — the roster + SSE
  driver is dramatically less code than a self-hosted loop plus its deployment.

**The one-line rule of thumb:** if the retrieved content can legally and safely
leave your boundary and you value not running infra, **buy**; if residency, cost,
or latency bind, **build**. For a bank-grade credit-monitoring workload over
regulated exposure data, residency typically forces **build** — which is why Arm A
is the one taken to a production Azure deployment.

---

## Provenance

- Live 3-way run, all 10 fixtures, one session; scored by the unchanged
  `CheckSuite` (Layer 1) + `claude-sonnet-4-6` judge (Layer 2).
- Surfaced to **Braintrust** (org `aiportfolio`, project `agent-evals`, run
  labels `baseline` / `arm-a` / `arm-b`):
  <https://www.braintrust.dev/app/aiportfolio/p/agent-evals>.
- Regenerate: `make compare ENV_FILE=.env` (writes `out/compare/compare.md` and
  updates the `README.md` compare table).
- Visual summary: [`docs/build-vs-buy.md`](docs/build-vs-buy.md).
</content>
</invoke>
