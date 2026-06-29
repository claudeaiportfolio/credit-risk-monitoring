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
| **C2 — eval surface + single-shot baseline** | this PR | **done** |
| C3 — Arm A agent loop | the investigating agent itself | not in this repo yet |

**C2 (this workstream)** builds the evaluation surface the agent will be graded
on, plus a deliberately weak single-shot baseline that establishes the floor.
It does **not** build the agent loop (C3) — but the run-trace format and the
scorer are agent-agnostic, so the agent drops straight in and is scored by the
same `CheckSuite`.

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
| `ANTHROPIC_API_KEY` | baseline completion + Layer-2 judge | for live runs / Layer 2 |
| `SEC_EDGAR_USER_AGENT` | descriptive UA SEC requires | recommended (SEC may 403 a generic UA) |
| `BRAINTRUST_API_KEY` | Braintrust surfacing (else silent no-op) | optional |
| `COMPANIES_HOUSE_API_KEY` | C3 agent UK-registry hops | not used in C2 |

Without `ANTHROPIC_API_KEY` the baseline uses a clearly-labelled offline answerer
(stamped into the trace `note`) so Layer-1 branch-correctness still runs in
credential-less CI; Layer-2 is skipped with a note.

## Layout

```
src/credit_risk_monitoring/
  fixtures.py        # typed loader; derives each fixture's expected source chain
  sources.py         # SourceType taxonomy + classifier + minimal EDGAR client
  trace.py           # agent-agnostic run-trace writer (agent-evals JSONL)
  baseline.py        # the deliberately single-retrieval baseline
  evals/
    checks.py        # Layer-1 chain + depth checks; build_suite()
    criteria.py      # Layer-2 groundedness criteria (per-fixture gold answer)
    score.py         # load -> score -> report -> Braintrust fan-out
  cli.py             # `credit-risk-eval run-baseline | score`
tests/               # loader, classifier, trace round-trip, checks (incl. over-investigation), judge, harness
fixtures/            # the 10-fixture bank (from feat/eval-fixtures)
```

## CI

- `python-ci` — ruff + mypy + the offline unit tests on every PR/push.
- `secret-scan` — the shared org security action (blocking gitleaks + advisory
  Claude review) on every PR/push.
- `eval` — manual dispatch; live baseline run + score, uploads the report.
