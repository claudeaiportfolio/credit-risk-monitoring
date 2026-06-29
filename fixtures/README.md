# Eval fixtures — credit-risk monitoring

These fixtures are the load-bearing input to the branch-correctness evaluation
(`fixtures.yaml`). Each one is a real, public credit-deterioration story whose
investigation path was verified hop-by-hop against **primary sources** (SEC EDGAR
via `efts.sec.gov` / `data.sec.gov` / `www.sec.gov/Archives`, and UK Companies
House). The set is **10 fixtures**: 6 multi-hop, 3 healthy single-hop controls, and
1 benign "trap" control.

## The load-bearing design choice: downstream question-framing

> **Frame every multi-hop question on a fact at the *end* of the chain — never on
> the headline event at the start.**

### Why (the problem this defends against)

The multi-hop fixtures are real distressed issuers, and several are **famous
bankruptcies** (Valaris, Hertz, Revlon, Bed Bath & Beyond). Framing the obvious,
*upstream* question — "What happened to Hertz in 2020?" — breaks the eval two ways:

1. **It is answerable in one hop.** The headline ("filed Chapter 11") is in the very
   first document, so no investigation is exercised — the fixture tests nothing.
2. **Training-memory contamination.** Because the event is famous, the model can
   answer from parametric memory *without investigating at all*. That silently
   contaminates **both arms and the single-shot baseline** — all three "succeed" by
   recall, and the multi-hop traversal we exist to measure goes unmeasured. This is
   the determinacy thesis collapsing into theatre.

### The fix

Anchor the question on a **downstream** fact: specific, obscure, and not memorable —
a registered charge ID and date, a subsidiary's current registry status, a named
collateral agent. Such a fact has two properties that make the eval honest:

- **It can only be reached by traversing the whole chain** (8-K → read the exhibit to
  discover the subsidiary → query the foreign registry → read its charges). That forces
  the genuine multi-hop investigation the branch-correctness scorer measures.
- **No model has it memorised** — nobody's training data contains "MR01 charge
  070985310001 dated 2020-09-25." So the agent *must* retrieve, and the single-shot
  baseline genuinely fails (one retrieval cannot reach a depth-4 registry fact).

Worked contrast (Valaris):

| | Question | Problem / property |
|---|---|---|
| Upstream (rejected) | "What happened to Valaris plc in 2020?" | one hop; answerable from memory |
| Downstream (used) | "...for its UK subsidiary Ensco Global Resources Limited, what registered charge was created during the bankruptcy, and what is its current Companies House status?" | only reachable via the full chain; memory-proof |

The discipline matters most on the famous four. The **obscure** Greenrose fixture
barely needs it (a sub-penny OTC issuer whose specifics no model recalls), and Wejo —
a partially-memorable ex-"unicorn" — is deliberately anchored on its UK registry facts
rather than its headline. The **controls** invert the framing: the *correct* answer is
"nothing further to find — stop," which is how they test against over-investigation.

## What each classification tests

- `multi_hop` — the agent should **win**; the single-shot baseline should **fail**. Scored on
  branch-correctness (right sources, right dependency order, right depth) + groundedness.
- `single_hop_control` — a healthy issuer; correct behaviour is to resolve in **one hop and
  stop**. Catches over-investigation.
- `trap_control` — looks like a credit event (a credit-agreement amendment) but is benign;
  correct behaviour is to **read the exhibit, conclude benign, and stop**. The sharpest
  over-investigation test.

## Verification discipline

Every hop in `ground_truth_path` was fetched from a primary source. **Facts that could
not be fetched are excluded from `expected_answer` and recorded under
`verification_caveats`** — an inferred or unconfirmed fact is never asserted as ground
truth. (E.g. Revlon's charge *purpose* is inferred and so excluded; BBBY's S&P press body
is paywalled; Greenrose's Connecticut appellate opinion text was not opened.)

## How the eval consumes this (C2)

The branch-correctness eval (built on the shared `agent-evals` package, surfaced to
Braintrust) scores each agent run against the fixture's `ground_truth_path` and
`expected_answer`:

- **Branch-correctness** — did the run traverse the right sources in the right dependency
  order, to `expected_stop_depth`, without under- or over-investigating?
- **Groundedness** — does the answer match `expected_answer` without synthesising past the
  sources?
- **Single-shot baseline** — the same questions answered with one retrieval; shown to fail
  the multi-hop cases. That gap is the honest measure of the agent's win.
