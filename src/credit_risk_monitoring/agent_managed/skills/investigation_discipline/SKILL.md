---
name: credit-investigation-discipline
description: >-
  Methodology for multi-hop credit-deterioration investigations across primary
  sources (regulatory filings, on-path exhibits, and foreign company registries).
  Use it whenever you are tracing whether an issuer's credit has deteriorated and
  need to decide which source to open next, when a downstream fact lives at a
  registry rather than in a filing, and — critically — when to stop. It encodes
  general branch-correctness discipline: follow the hop the last source's content
  generates, open the exhibit that source points to instead of guessing, resolve a
  named or derived counterparty at the authority that actually holds the fact, and
  stop the instant the deterioration picture resolves.
---

# Credit-investigation discipline

A credit-deterioration signal is usually a *downstream* fact: a filing names an
exhibit, the exhibit names a subsidiary, the subsidiary resolves at a registry,
the registry exposes a charge or an insolvency status. The value of the
investigation is **branch correctness** — taking the right sources, in the right
dependency order, to the right depth, and then stopping. Too few hops leaves the
signal unconfirmed (under-investigation); too many chases facts that add nothing
(over-investigation). Both fail. This skill is the discipline for getting the
number and order of hops right. It is method, not answers — never assume a
specific entity, charge, agent, or rating; establish each from a source.

## The core rule: let the last source's content pick the next hop

Do not plan the whole chain up front and do not follow a fixed template. After
each source, read what it actually says and let *its content* generate the next
hop:

- A filing that says the operative terms live in an **exhibit** (a debtor
  schedule, a restructuring-support agreement, a security or credit agreement)
  means the next hop is **that exhibit** — open it, do not infer its contents
  from the filing body.
- A source that names (or describes) an entity **incorporated in another
  jurisdiction** means the next hop is **that jurisdiction's registry**, not
  another domestic filing — the registered status, number, charges, and
  insolvency state live there, not in the home-country filing.
- A confirmed, dated, named default event is the trigger a **rating action**
  reacts to — if the question asks about the rating, the next hop is the rating
  authority, not another filing.

If the last source did not generate a next hop, the branch is complete. Stop.

## Open the on-path exhibit — do not guess it, do not skip it

When a filing points to an exhibit that carries the fact you need (the list of
filing debtors, the schedule of subsidiaries, the security agreement), open that
specific exhibit:

- **Do not guess** its contents, its identifiers, or which subsidiary it names
  from the filing body alone. The exhibit is the primary-source grounding; guessing
  is how a wrong entity enters the chain.
- **Do open it even when the question already names the entity** — the exhibit is
  the confirmation that the named entity is actually on the official list, and it
  is an on-path hop.
- **Do not open an off-path exhibit** — one that names a *different* thread's
  parties (e.g. a domestic facility's administrative agent when the question is
  about a foreign subsidiary already established from the filing body). That is an
  extra hop that fails.

## Resolve the entity at the authority that holds the fact

A registry fact about a foreign subsidiary — its registered number, its status
(active / in administration / dissolved), its registered charges and who holds
them — is resolved at the **registry**, never fetched from a home-country filing.
Two cases, and they decide your first registry call:

- **The entity is named** (an exact registered name is in hand): look it up
  directly — resolve the name to the registered record in one step, then fetch
  **at most one** further endpoint, the one the question actually needs (charges
  for a charge/secured-party question; the company record alone already carries
  status and number for a status/insolvency question).
- **The entity is only described** (the upstream source established a *class* of
  subsidiaries or a distinguishing property but not the registered name — e.g.
  "the group's UK receivables-financing subsidiary", "the sole UK-incorporated
  debtor"): you must **discover** it. First **search the registry** with a query
  built from the brand plus the distinguishing keyword, read the candidates, pick
  the one matching the described property, then confirm it with the company
  record, then fetch at most one further endpoint. Discovery is a genuine extra
  hop *at the registry* — do not instead open more filings hunting for the name;
  the specific entity is found downstream, not upstream.

Never resolve entities the question does not ask about, and never probe extra
registry endpoints (officers, filing history) the question does not require.

## Stop when the deterioration picture resolves

"Stop" means: stop opening new sources and give the grounded answer. Stop the
instant you have (a) confirmed the distress event(s) the question is about, (b)
grounded any on-path exhibit the filing pointed to, and (c) resolved every
downstream lead the question asks about, to the depth that answers it.

- **A fact that lives at a registry or a rating authority is not yours to chase
  through more filings.** Surface it as a lead and resolve it once at its
  authority; do not open the emergence / plan-confirmation / closing filing to
  hunt for it.
- **Open a further filing only** when the question asks about a distinct event
  whose answer lives *only* in a filing and you do not already have it (e.g. a
  foreclosure closing and a later liquidation filing are two separate facts).
- **Do not skip the covenant / charge / status hop.** Under-investigation is as
  much a failure as over-investigation: a distressed issuer with a question about
  a subsidiary's registered charge, its insolvency status, or a post-default
  rating is *not* resolved until that final registry or rating hop is taken.
  Stopping one hop short — reporting the distress event but not the downstream
  registry fact — leaves the question unanswered.

## The benign case: read, conclude, and stop at one hop

Not every filing is deterioration. A routine screen of recent filings with
nothing distressed, or a benign event (a maturity extension, a commitment
upsize, a refinancing with no covenant breach, waiver, forbearance, or
going-concern language), resolves in a single hop: read it, conclude there is no
credit deterioration warranting escalation, and stop. Chasing the subsidiaries,
agents, or lenders of a *benign* filing is the classic over-investigation
failure — the healthy issuer generates no downstream hop.

## Ground the answer in what you retrieved

State the concrete downstream fact directly — the charge and its holder, the
registered status and number, the appointed administrators, the rating and its
date — using only what the sources returned. Do not add identifiers, dates,
parties, or ratings that are not in the retrieved sources. If a hop could not be
retrieved, say so rather than inventing the fact.
