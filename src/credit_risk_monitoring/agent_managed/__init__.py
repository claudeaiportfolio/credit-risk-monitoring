"""Arm B — the SAME multi-hop credit investigation as Arm A, on **Anthropic
Managed Agents** (multi-agent) instead of a self-hosted raw-SDK loop.

This is the *buy* side of the build-vs-buy comparison. Arm A (``agent/``) runs
the agent loop and hosts tool execution itself (a self-hosted ``agent-core``
loop, deployable to ACA); Arm B hands both to Anthropic's Managed Agents
service: a persisted **coordinator** agent delegates to a **sub-agent roster**,
Anthropic runs the multi-agent loop, and the session streams events back over
SSE.

What is held **identical** across the two arms so the comparison is fair:

* **The decomposition** — the same three roles: exposure (SEC/EDGAR), entity
  resolution (external authority), synthesis (compose + stop). See the README
  and :mod:`~credit_risk_monitoring.agent_managed.roster` for how MA's
  coordinator subsumes the synthesis role, so the *minimum sub-agent roster* is
  two (exposure + entity_resolution) under one tool-less coordinator.
* **The tool surface** — the EXACT Arm A retrieval tools (the EDGAR spine +
  the Companies House / rating clients), reused unmodified (portfolio
  centralise rule; a fair build-vs-buy needs the same tool surface on both
  arms). They are exposed to the Managed Agent as **custom tools** executed
  host-side over the SSE stream (see the tool-exposure decision in
  :mod:`~credit_risk_monitoring.agent_managed.roster`).
* **The scored trace** — every retrieval hop is recorded into the SAME C2
  :class:`~credit_risk_monitoring.trace.TraceWriter` (one ``tool_use`` per hop,
  named by canonical ``SourceType``), so the UNCHANGED ``agent-evals``
  ``CheckSuite`` scores Arm B identically to Arm A and the baseline.
* **The models** — coordinator + sub-agents on ``claude-opus-4-8``; the judge
  on ``claude-sonnet-4-6`` (both env-overridable, matching current policy).
"""

from __future__ import annotations

from credit_risk_monitoring.agent_managed.orchestrator import (
    ManagedOrchestrator,
    ManagedRunResult,
)
from credit_risk_monitoring.agent_managed.roster import Roster, ensure_roster

__all__ = [
    "ManagedOrchestrator",
    "ManagedRunResult",
    "Roster",
    "ensure_roster",
]
