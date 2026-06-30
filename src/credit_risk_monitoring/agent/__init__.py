"""Arm A — the raw-SDK multi-hop credit-investigation agent (Workstream C3a).

A parent orchestrator delegates to the *minimum* set of sub-agents the
credit-deterioration investigation needs, each behind a typed Pydantic handoff
contract (``agent.contracts``):

* **exposure** — works the SEC/EDGAR side: from the issuer it opens the distress
  filing(s) and their exhibits, and extracts the named *downstream* references
  (a foreign subsidiary, a credit facility, a rating subject). Tools:
  ``edgar_submissions_index`` / ``edgar_filing`` / ``edgar_exhibit``.
* **entity_resolution** — resolves a named downstream reference at its external
  authority: a UK company at Companies House, or a defaulted issuer at the
  rating agency. Tools: ``companies_house`` / ``external_rating``.
* **synthesis** — composes the final grounded answer (and the stop decision)
  from the accumulated, typed findings. No tools; streams its answer through the
  provider seam.

Three sub-agents, justified by the investigation's structure (see the repo
README): two distinct retrieval *domains* with different clients, auth, and
rate limits (US SEC vs. UK registry / rating agency), plus a synthesis step
that must *not* retrieve (the over-investigation guard the trap control
catches). One sub-agent per domain gives least-privilege tool scoping, which is
also what makes the per-sub-agent kill-switch tokens (``agent.authz``)
meaningful.

The loop itself is the shared ``agent-core`` runtime routed through the
``llm-provider`` seam; this package supplies only the domain (tools, prompts,
contracts, authz, audit, trace emission). The scored trace is the *same* C2
JSONL the single-shot baseline emits, so the existing branch-correctness
``CheckSuite`` scores Arm A unchanged.
"""

from __future__ import annotations

from credit_risk_monitoring.agent.contracts import (
    ExposureFindings,
    FinalAnswer,
    InvestigationRequest,
    Lead,
    RegistryFinding,
    ResolutionRequest,
    SynthesisRequest,
)
from credit_risk_monitoring.agent.orchestrator import AgentRunResult, Orchestrator

__all__ = [
    "AgentRunResult",
    "ExposureFindings",
    "FinalAnswer",
    "InvestigationRequest",
    "Lead",
    "Orchestrator",
    "RegistryFinding",
    "ResolutionRequest",
    "SynthesisRequest",
]
