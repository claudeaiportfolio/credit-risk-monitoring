# Skills experiment — does a Managed Agents Skill improve Arm B?

**Question (decided by measurement on THIS eval, not assumed):** does attaching a
Managed Agents **Skill** — a progressive-disclosure investigation-discipline
document — to the Arm B roster measurably improve investigation quality
(branch-correctness + groundedness) on the credit-risk eval?

**Verdict: NOT adopted.** Measured on the 6 multi-hop fixtures, attaching the
Skill *lowered* investigation quality — branch-correct **3/6 → 2/6**, grounded
**6/6 → 4/6** — while adding **~16% cost** and **~18s latency**. Its "stop when
the picture resolves" guidance made the agent **under-investigate** (hertz: depth
4 / 3-of-3 grounded without the Skill → depth 2 / 0-of-3 with it). Per the scoping
rule "measured, not assumed," the Skill does not go in: **Arm B ships Skill-free.**
A genuine negative result — the honest outcome the scoping doc anticipates.

This document records the experiment, the measured with/without numbers, the
keep/drop decision, and how to reproduce it. It follows the scoping requirement
that this decision be made by measurement, and the honesty bar that a null result
is a valid outcome.

## What the Skill is

A single progressive-disclosure Skill,
[`src/credit_risk_monitoring/agent_managed/skills/investigation_discipline/SKILL.md`](../src/credit_risk_monitoring/agent_managed/skills/investigation_discipline/SKILL.md).
Its `name` + `description` frontmatter sits in each agent's context by default;
the model reads the full body on demand when a task calls for it (that is what
"progressive disclosure" means, and it is why the Skill adds context — hence
cost — only when consulted).

The body encodes the **general** multi-hop credit-risk methodology this eval
rewards:

- follow the hop the *content of the last source* generates (filing index → 8-K →
  on-path exhibit → named/derived subsidiary → Companies House → …);
- open the on-path exhibit rather than guessing its contents;
- resolve a named **or derived** UK entity at Companies House, including a
  company-search discovery step when the entity isn't named upstream;
- **stop** the instant the deterioration picture resolves;
- do **not** chase off-path subsidiaries (over-investigation) and do **not** skip
  the covenant/charge/status hop (under-investigation);
- the benign case resolves in one hop — read, conclude benign, stop.

It is deliberately written as **general investigation discipline, not fixture
answers**: it names no issuer, subsidiary, charge, agent, court, or rating from
the fixtures. Baking fixture answers into the Skill would be gaming the eval (it
would inflate the score without reflecting a real capability and would not
generalise) — the honesty bar forbids it, and the with/without comparison would
be meaningless.

## How it's wired (and why Arm B still defaults to no-Skill)

A Skill is bound to an agent at **create** time
(`beta.agents.create(..., skills=[{"type":"custom","skill_id":…,"version":"latest"}])`,
`managed-agents-2026-04-01` beta, SDK-set). So the two variants are two distinct
agent rosters over the **same** environment, custom tools, sub-agent system
prompts, and model (`claude-opus-4-8`) — the attached Skill is the *only*
difference.

- [`skill.py`](../src/credit_risk_monitoring/agent_managed/skill.py) authors and
  registers the Skill via the Skills API (`skills-2025-10-02`, SDK-set), reusing
  `ARM_B_SKILL_ID` when present (control-plane: upload once, reference by ID).
- [`roster.py`](../src/credit_risk_monitoring/agent_managed/roster.py) →
  `ensure_roster(..., skill_id=...)` attaches the Skill to **all three** agents
  (coordinator + exposure + entity_resolution) when a `skill_id` is supplied, and
  to none otherwise.
- [`orchestrator.py`](../src/credit_risk_monitoring/agent_managed/orchestrator.py)
  → `ManagedOrchestrator(use_skill=…)` gates it behind the **off-by-default**
  `ARM_B_USE_SKILL` flag. **Arm B ships with no Skill.** The Skill is kept in-repo
  as this measured experiment, behind that off-by-default flag; the production
  `run-agent-b` / `compare` paths are unchanged and Skill-free unless the flag is
  set.

**A Managed Agents coupling worth noting (a small buy-vs-build data point).** A
Skill's files are loaded from the session sandbox via the built-in `read` tool, so
attaching a Skill **400s at *session*-create** unless `read` is enabled on the
agent's `agent_toolset` (`"skills require the read tool to be usable ... on the
session's agent_toolset"`). Arm B's agents otherwise use *only* host-side custom
tools (no built-in toolset), so the Skill roster additionally enables **only**
`read` (the rest of the built-in toolset stays disabled). `read` is inert for this
custom-tool investigation — there are no sandbox files to read besides the Skill
itself — so the measured delta stays attributable to the Skill, not a wider tool
surface. The coupling itself is the finding: the managed Skills feature assumes the
managed sandbox toolset, which a pure host-side-custom-tool design doesn't use.

## Method

The harness
[`skill_experiment.py`](../src/credit_risk_monitoring/agent_managed/skill_experiment.py)
(`credit-risk-eval run-skill-experiment`, `make skill-experiment`):

1. creates one shared environment, then a no-Skill roster and a Skill roster;
2. runs each variant over the fixtures via the **unchanged** Arm B data plane
   (`run_arm_b_metrics`), emitting the same C2 traces + tokens/$/latency;
3. scores **both** variants with the **unchanged** shared scorer — it reuses
   `compare.score_arm` verbatim: Layer-1 `build_suite` (chain-correctness,
   depth-correctness) + the Layer-2 groundedness judge, the exact surface every
   other arm is scored by. The `CheckSuite`, the fixtures, and the shared
   packages are untouched.

Coverage: the **6 multi-hop fixtures** (`--limit 6`). That is where an
investigation-discipline Skill can actually change the trajectory (follow / open /
resolve / stop decisions). The 3 single-hop controls and the trap already pass in
both arms, and a multi-hop-discipline Skill cannot change a one-hop lookup, so
they are out of scope for this measurement (running them would only add spend
without discriminating power). A single-fixture smoke was run first to verify the
Skill attaches and the multi-agent loop runs.

## Results

One live run, both variants over the 6 multi-hop fixtures, scored by the unchanged
suite. Per-fixture cell = `chain / depth / groundedness (depth reached)`.

| fixture | exp. depth | no-skill | skill |
| --- | --- | --- | --- |
| valaris-uk-subsidiary-charge | 4 | OK/OK/3/3 (d4) | OK/OK/3/3 (d4) |
| hertz-uk-receivables-charge-holder | 4 | OK/OK/3/3 (d4) | OK/**X**/**0**/3 (d2) |
| revlon-elizabeth-arden-uk-charge | 4 | OK/OK/3/3 (d4) | OK/OK/3/3 (d4) |
| bbby-default-to-sp-rating | 3 | X/X/2/3 (d4) | X/X/1/3 (d5) |
| greenrose-foreclosure-recipient-and-liquidation | 4 | X/X/3/3 (d2) | X/OK/3/3 (d4) |
| wejo-uk-administration | 3 | X/X/3/3 (d5) | X/X/3/3 (d4) |

| variant | branch-correct (chain+depth) | grounded (>=2/3) | input tok | output tok | cost $ | wall-clock s | mean latency s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| no-skill | **3/6** | **6/6** | 727,825 | 67,666 | **$2.98** | 846 | 141.0 |
| skill | 2/6 | 4/6 | 1,022,158 | 70,314 | $3.44 | 953 | 158.8 |

**Measured delta (skill − no-skill):** branch-correct **−1** (3→2), grounded
**−2** (6→4), cost **+$0.46 (+15.6%)**, mean latency **+17.8s**. (`input tok` =
uncached + prompt-cache read/write; the Skill body loads into context on demand,
adding ~300k input-side tokens across the run.)

## Verdict and rationale

**Drop the Skill.** It did not improve investigation quality on the fixtures where
it could — it *reduced* it (branch-correct 3→2, grounded 6→4) at higher cost and
latency. The clearest regression is **hertz**: without the Skill the agent runs the
full chain to depth 4 and grounds the answer (3/3); with the Skill it stops at
depth 2 with nothing grounded (0/3) — the Skill's explicit "stop the instant the
picture resolves" instruction made it stop *before* the picture had resolved
(under-investigation, exactly the §0(c) risk the eval exists to catch). greenrose's
depth check flips OK with the Skill, but its chain is still wrong and no fixture is
newly *won*. Net: the Skill trades a real win (hertz) for no gains and added spend.

Per the scoping doc's rule — the Skill decision is made by fresh measurement, and a
Skill that doesn't help doesn't go in — **Arm B ships with no Skill.** The Skill,
the harness, and this record stay in-repo (behind the off-by-default
`ARM_B_USE_SKILL` flag) as the documented measurement, not as shipped behaviour.
This is a null/negative result, and reporting it honestly is the point.

## Reproduce

```bash
# keys from localdevenv KV -> gitignored .env (see Makefile header)
make skill-experiment SKILL_ARGS="--limit 1"   # smoke first (spend-aware)
make skill-experiment SKILL_ARGS="--limit 6"   # the measurement: 6 multi-hop, both variants
# outputs: out/skill-exp/experiment.md (table) + experiment.json (machine-readable)
```

> Run the experiment detached (`nohup … & disown`) with a lightweight poll for
> `out/skill-exp/experiment.md`: a full both-variants run is ~15–20 min of live
> multi-agent sessions.

## Guardrails honoured

- scorer (`CheckSuite` / `build_suite`) and fixtures **unchanged**; shared
  packages unmodified — the experiment composes existing pieces;
- no fixture answers baked into the Skill (general discipline only);
- no secrets committed (keys via gitignored `.env` from Key Vault);
- Skill kept in-repo behind an off-by-default flag; Arm B defaults to no-Skill.
