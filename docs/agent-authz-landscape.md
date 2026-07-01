# Agent authorization: buy vs. build (Okta landscape)

**Thesis.** This multi-agent system hand-rolls a small runtime-authorization and
kill-switch primitive so that a rogue or compromised sub-agent can be refused
*mid-workflow*, not just at startup. I built the primitive to prove I understand
the mechanism end-to-end — token minting, a boundary check on every tool call, a
one-action admin revoke, and a durable audit trail. This document maps that
primitive onto the managed product a real regulated customer would reach for
(Okta), and makes the honest buy-vs-build call: **where the hand-rolled version
is genuinely sufficient, and where a regulated buyer should adopt a managed
identity-authorization platform instead — and why.**

It is deliberately not a sales pitch for Okta. The "buy" column has real costs
(spend, lock-in, over-engineering for a small blast radius) and the "build"
column has real limits (it governs only *local* tool calls and stops at the
process boundary). Both are stated.

---

## 1. What we actually built (the "build" side)

The primitive lives in three files under `src/credit_risk_monitoring/agent/`:

| Concern | Where | What it does |
| --- | --- | --- |
| Capability token | `authz.py` — `ToolToken`, `AuthorityBroker.mint()` | Mints a short-TTL, per-sub-agent token scoped to exactly the tools that role needs. |
| Auth boundary | `tools.py` — `ToolRouter.call_tool()` → `AuthorityBroker.authorize()` | Checks the token on **every** tool call before any retrieval runs. |
| Kill switch | `authz.py` — `revoke_agent()`, `revoke_token()`, `deny_tool()` | One-call admin revoke (by agent, by token, or a global tool deny-list). |
| Audit sink | `audit.py` — `AuditEvent`, `AuditSink`, `PostgresAuditSink` | Records every mint / authorize / execute / revoke / deny (who, what, when, revoked). |

### 1.1 Short-TTL capability tokens (least privilege)

`AuthorityBroker.mint(agent_id, scopes, ttl=...)`
(`src/credit_risk_monitoring/agent/authz.py:83`) issues a frozen `ToolToken`
carrying the agent id, a `frozenset` of tool scopes, an issue time, a TTL
(default `DEFAULT_TOKEN_TTL_SECONDS = 120.0`), and a random nonce. Each
sub-agent gets only its role's tools: the orchestrator scopes the exposure agent
to the EDGAR tools and the resolution agent to the Companies House / rating tools
(`orchestrator.py:58` `EXPOSURE_TOOLS`, `orchestrator.py:63` `RESOLUTION_TOOLS`;
minted per phase in `Orchestrator._router()` at `orchestrator.py:228`). The TTL
is short by design — a sub-agent's tools are only needed for the seconds its
phase runs, so a leaked or replayed token expires almost immediately.

### 1.2 A deny check at the boundary, on *every* tool call

The enforcement point is `ToolRouter.call_tool()`
(`src/credit_risk_monitoring/agent/tools.py:469`). Step 1 of every call is
`self._auth.broker.authorize(self._auth.token, name)` — before any retrieval
runs. `AuthorityBroker._evaluate()` (`authz.py:124`) checks, in order:

1. agent revoked (admin kill),
2. token nonce revoked,
3. tool on the global deny-list,
4. token expired,
5. tool out of the token's scope.

Because the deny-list and revoked-agent sets are consulted live on every call
(not cached into the token), **adding an agent to the revoked set or a tool to
the deny-list refuses the very next in-flight call** — the agent is stopped
mid-workflow, not merely blocked from a fresh start. The check order is
deliberate: admin revoke is tested *before* expiry and scope, so an admin kill
always wins over an otherwise-valid unexpired token. A refusal returns an
`ERROR:` string to the agent loop and records **no** retrieval hop (a policy
refusal is not a retrieval).

### 1.3 The admin kill switch

`AuthorityBroker.revoke_agent(agent_id)` (`authz.py:138`) flips one agent to
denied in a single call — no need to first prove compromise, cancel a token, or
restart anything. There is also `revoke_token(token)` (revoke one specific
nonce) and `deny_tool(tool)` (kill a tool for *every* agent at once — e.g.
disable `companies_house` fleet-wide during an incident). This is the "what's
your plan when an agent goes rogue" answer in code: one call, effective on the
next tool invocation.

### 1.4 The audit trail

Every action is written to a pluggable `AuditSink` as an `AuditEvent`
(`audit.py:35`) capturing **who** (`agent_id`), **what** (`action` ∈
`mint | authorize | execute | revoke_agent | deny_tool`, plus `tool`), **when**
(`ts`), and **whether it was blocked** (`allowed`, `revoked`, `reason`). In
production the sink is `PostgresAuditSink` (`audit.py:105`) — append-only, lazily
connected, table auto-created, DSN sourced only from `AUDIT_DATABASE_URL`
(injected from Key Vault via workload identity, never committed). Without a DSN it
falls back to `InMemoryAuditSink` mirrored to stderr, so a credential-less run
still leaves a readable trail. An audit-write failure is logged, never raised —
an audit outage must not take down the investigation, but it must be visible.

### 1.5 Honest scope of the build

This is a **self-contained authority broker for local, in-process tool calls** —
not an identity provider. It deliberately does *not* do (and is documented in
`authz.py` as not doing):

- **Machine identity / authentication.** It governs *which local tool* an
  already-authenticated sub-agent may invoke. It is not Auth0 / OIDC; it does not
  authenticate the agent process to a remote MCP server (that is `agent-core`'s
  `Auth0M2MClient` job).
- **Cross-process / cross-app authorization.** The revoked/deny sets are
  process-local in-memory state (fast, single-orchestrator). There is no shared
  control plane a second service would consult.
- **Verifiable delegation.** The token says "agent X may call tool Y"; it does
  not carry a cryptographically verifiable chain of *on whose behalf* X is acting
  through multiple hops.
- **Resource-level, context-aware policy.** Scope is coarse (tool name). It does
  not evaluate "may THIS agent, for THIS user, act on THIS specific record, right
  now."

Those four gaps are exactly what the managed products below sell.

---

## 2. The managed alternative (the "buy" side — Okta)

All product facts below are cited to Okta's live documentation. Where a
capability the brief asked about could **not** be verified from official docs,
that is stated explicitly rather than asserted (see §2.4).

### 2.1 Okta Cross App Access (XAA) — the agent-to-app / A2A connection layer

Okta's productized answer to agent-to-agent / agent-to-app authorization is
**Cross App Access (XAA)**, an extension of OAuth. An AI tool requests access
from Okta; Okta evaluates the request against enterprise policy and, if
permitted, issues a token the tool presents to the target ("resource") app,
which validates it and grants access under enterprise-defined controls — no extra
user interaction. Mechanically it uses **token exchange**: an ID token is
exchanged for an **ID-JAG (Identity Assertion JWT)**, which is then exchanged for
a scoped access token at the resource app. Per-agent access is constrained by
**"Resource connection" configurations that define which resources an agent is
allowed to access**, and the final access token's permissions are set by the
**scopes requested at the resource app**. Access tokens issued by Okta can be
**revoked** via standard Okta revocation.

Maps to our build: XAA is the managed, cross-app, standards-based version of our
mint + scope + boundary — but with a verifiable delegation chain (ID-JAG) and an
identity provider behind it, spanning *separate applications* rather than local
in-process tools.

Sources:
[Cross App Access solution](https://www.okta.com/solutions/cross-app-access/) ·
[XAA developer blog](https://developer.okta.com/blog/2025/09/03/cross-app-access) ·
[AI agent token exchange guide](https://developer.okta.com/docs/guides/ai-agent-token-exchange/-/main/)

### 2.2 Okta FGA (Fine-Grained Authorization) — the runtime "can this, now?" check

**Okta FGA** (the productized Auth0 FGA) is authorization-as-a-service using
**Relationship-Based Access Control (ReBAC)**, modeled on Google's Zanzibar. The
runtime primitive is the **Check API** (`POST /stores/{STORE_ID}/check`), which
evaluates a query of the form **"can user X do relation Y on object Z"** against
stored relationship tuples plus the authorization model, and returns
`{"allowed": true|false}` in real time. It supports **contextual (and
time-based) tuples** — dynamic context passed at check time and not persisted —
so a decision can factor in transient conditions alongside stored relationships.
Authorization logic is centralized outside application code, which is what makes
it **auditable** and consistent across services.

Maps to our build: FGA is the managed version of `AuthorityBroker.authorize()`,
but far richer. Our check answers "is tool Y in this agent's scope, and is the
agent/tool not killed?" FGA answers the harder regulated-finance question: "may
*this* agent, acting *for this user*, take *this action* on *this specific
resource*, *right now*?" — the resource- and delegation-aware check our
tool-name scope cannot express.

Sources:
[FGA getting started / Check API](https://docs.fga.dev/getting-started) ·
[FGA product page](https://www.okta.com/products/fine-grained-authorization/) ·
[Intro to Auth0 FGA](https://docs.fga.dev/fga)

### 2.3 Managed kill switch / revocation

Two managed mechanisms map to our kill switch:

- **Token revocation (XAA):** Okta-issued access tokens can be revoked through
  standard Okta revocation, cutting off an agent's access to resources reached
  via that token
  ([token exchange guide](https://developer.okta.com/docs/guides/ai-agent-token-exchange/-/main/)).
- **Relationship removal (FGA):** because access is decided by relationship
  tuples, *deleting the tuple* immediately flips subsequent Check calls to
  `allowed: false` — a per-relationship revoke consistent with the Zanzibar
  model
  ([FGA getting started](https://docs.fga.dev/getting-started)).

XAA also advertises, at the solution level, **"more visibility, auditability,
and command over inter-app communication"** for security teams
([Cross App Access solution](https://www.okta.com/solutions/cross-app-access/)) —
i.e. an admin console for oversight of agent-to-app connections.

### 2.4 Capabilities I could NOT verify (honesty bar)

The brief asked me to map to a "managed kill switch — one-action revoke across
**all connected resources** from an admin console." I could **not** verify a
single, named, one-click "kill switch" feature with that exact
blast-radius-wide semantics in Okta's official docs. What the docs *do* support
is: (a) standard **token revocation** of Okta-issued tokens, (b) **relationship
(tuple) removal** in FGA, and (c) general admin **"visibility, auditability, and
command"** over inter-app communication. Whether those compose into a literal
one-action "revoke this agent everywhere" button is not something the public docs
I read state explicitly — so I am not asserting it. A buyer should confirm the
exact revoke-blast-radius and admin-console workflow with Okta directly.

Additional note: XAA is documented as a **self-service Early Access** feature
that must be enabled in the Admin Console
([XAA developer blog](https://developer.okta.com/blog/2025/09/03/cross-app-access)),
so its maturity/GA status should be checked before a production commitment.

---

## 3. Buy-vs-build comparison

| Capability | Our primitive (build) | Okta XAA / FGA (buy) |
| --- | --- | --- |
| Per-agent scoping / allowlist | Tool-name scopes in a minted token (`mint()` + `EXPOSURE_TOOLS`/`RESOLUTION_TOOLS`) | XAA resource-connection config; FGA relationship tuples |
| Runtime check granularity | Tool name + TTL + revoke/deny (coarse) | FGA Check API: user × relation × object, contextual/time tuples (fine, resource-level) |
| "Acting for THIS user on THIS record" | Not expressible | Native to FGA ReBAC |
| Verifiable delegation chain | No (token asserts agent→tool only) | XAA ID-JAG token exchange (verifiable) |
| Kill switch | `revoke_agent` / `revoke_token` / `deny_tool`, effective next call, in-process | Token revocation + tuple removal; blast-radius-wide one-click not verified (§2.4) |
| Cross-process / cross-app scope | No — single orchestrator, in-memory posture | Yes — centralized identity/authorization plane |
| Audit trail | Postgres append-only, every action, who/what/when/revoked | Centralized, auditable authz log (managed) |
| Operational cost | ~360 lines, no external dependency, we maintain it | Subscription + integration + IdP dependency; no maintenance of the mechanism |
| Failure mode | Process-local; dies with the process | Managed availability SLA; network dependency on the check path |

---

## 4. "What breaks if this is absent" — why a regulated buyer cares

A multi-agent workflow touching financial data is precisely where the weak form
of agent authorization fails. The common failure is: **the token gets the agent
through the door but does not govern what it does inside.** A sub-agent
authenticates once, then makes many tool calls over the life of a task — and if
authorization is only checked at the door, a prompt-injected or misbehaving agent
runs unchecked until the task ends.

That is the question a CISO or risk committee asks before approving an agentic
system on regulated data: *"What is your plan when an agent goes rogue
mid-task?"* If the answer is "we'd notice in the logs and restart it," the system
does not clear the bar. Our primitive answers it minimally but concretely: an
admin `revoke_agent` call denies the **next** tool call, and every action —
including the denial — is in the audit log with who/what/when. That is enough to
*demonstrate* the mechanism and to defend a small, single-orchestrator system.

Where it stops being enough is scale and delegation. In a real deployment the
questions become "may this agent read *this client's* filings but not another's?"
and "prove this agent was acting for *this* analyst when it pulled that record" —
resource-scoped and delegation-verified checks our tool-name scope and
process-local posture cannot express. That is the FGA / XAA territory.

---

## 5. Recommendation — honest call

**When the hand-rolled primitive is sufficient**

- Single orchestrator, in-process tools, small and well-known blast radius (this
  repo).
- The governance need is *demonstrable control + a defensible audit trail*, not
  cross-app or per-record policy.
- You want zero external dependency on the hot path and full control of the
  mechanism. Building it is also the better *learning/portfolio* signal — it
  proves the mechanism is understood rather than delegated.

Adding Okta here would be over-engineering: a subscription, an IdP dependency,
and a network round-trip on every tool call to replace ~360 lines whose entire
job is refusing local, in-process calls. Coarse tool-name scope is adequate when
the tool set *is* the resource granularity.

**When a real (regulated) customer should adopt Okta XAA / FGA**

- Agents span **multiple applications / services** (not one process) — you need a
  shared control plane and a **verifiable delegation chain** (XAA ID-JAG), not
  process-local in-memory state.
- Authorization must be **resource- and user-scoped**: "this agent, for this
  user, on this record, now" — the FGA Check API, not a tool-name allowlist.
- Auditors/regulators require **centralized, externally-managed** authorization
  and revocation with an admin console, decoupled from application code.
- The blast radius is large enough that "we maintain the auth mechanism
  ourselves" is itself a risk finding.

**The genuine trade.** Buy gets you resource/user-level policy, verifiable
delegation, cross-app reach, and a managed audit/revoke plane you don't maintain
— at the cost of spend, an IdP dependency on the authorization hot path, lock-in
to a vendor's model, and real over-kill for a single-process, small-scope system.
Build gets you a zero-dependency, fully-owned, easily-audited primitive that
proves the mechanism — at the cost of coarse (tool-name) granularity, no verified
delegation, and a boundary that stops at the process edge. **For this repo,
build is correct. For a bank running many agents across many apps on many
clients' data, buy is correct** — and the deciding factor is not "which is
better software" but *whether the authorization question you must answer is
resource-and-delegation-shaped or merely tool-shaped.*

---

### Sources

- Okta Cross App Access (solution): https://www.okta.com/solutions/cross-app-access/
- Cross App Access developer blog: https://developer.okta.com/blog/2025/09/03/cross-app-access
- AI agent token exchange guide: https://developer.okta.com/docs/guides/ai-agent-token-exchange/-/main/
- Okta / Auth0 FGA getting started (Check API): https://docs.fga.dev/getting-started
- Okta FGA product page: https://www.okta.com/products/fine-grained-authorization/
- Introduction to Auth0 FGA: https://docs.fga.dev/fga

*Code references are to `src/credit_risk_monitoring/agent/{authz,tools,audit,orchestrator}.py` at the commit this doc was authored against.*
