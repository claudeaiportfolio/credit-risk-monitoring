"""Typed Pydantic handoff contracts between the orchestrator and sub-agents.

Each handoff's *working set* is defined by a contract here — never a dumped
blob of the upstream conversation. The orchestrator hands a sub-agent exactly
the typed input it needs, and the sub-agent returns exactly the typed output
the next phase consumes. A sub-agent's free-text final answer is parsed and
**validated** into the relevant model at the boundary (see
``agent.subagents.parse_contract``); a validation failure is a contract
violation, surfaced rather than smuggled downstream.

Keeping the contracts in one module makes the data-flow auditable: read this
file and you know exactly what crosses each agent boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# A downstream reference is one of: a foreign company to resolve at a registry,
# a credit facility/instrument, or an issuer whose rating moved. ``rating_subject``
# routes to the external-rating tool; ``uk_company`` routes to Companies House.
LeadKind = Literal["uk_company", "rating_subject", "us_facility", "other"]


class _Strict(BaseModel):
    # Reject unknown keys so a drifting sub-agent output is caught, not absorbed.
    model_config = ConfigDict(extra="forbid")


class InvestigationRequest(_Strict):
    """Orchestrator -> exposure: the issuer and the question to investigate."""

    issuer_name: str
    cik: str
    question: str


class Lead(_Strict):
    """A named downstream reference the exposure phase surfaced for resolution."""

    name: str = Field(description="The named entity/instrument exactly as it appears in the source.")
    kind: LeadKind
    jurisdiction: str = ""
    rationale: str = Field(default="", description="Why this lead matters, citing the source hop.")
    source_locator: str = Field(default="", description="Where it was found (filing/exhibit).")


class ExposureFindings(_Strict):
    """exposure -> orchestrator: the SEC-side result of the investigation.

    ``recommend_stop`` is the control/trap guard: for a healthy issuer (one
    index glance) or a benign filing (read it, conclude benign), the exposure
    phase reports that nothing downstream needs resolving and the orchestrator
    must not delegate further.
    """

    issuer_name: str
    distress_summary: str
    leads: list[Lead] = Field(default_factory=list)
    recommend_stop: bool = False
    stop_rationale: str = ""


class ResolutionRequest(_Strict):
    """Orchestrator -> entity_resolution: one lead to resolve at its authority."""

    lead: Lead
    question: str


class RegistryFinding(_Strict):
    """entity_resolution -> orchestrator: what the external authority returned.

    Fields are intentionally generic across the two authorities: Companies House
    populates ``status`` / ``charges`` / ``secured_parties`` / ``identifier``
    (the CH number); the rating agency populates ``rating``. Unused fields stay
    empty rather than being forced.
    """

    entity_name: str
    authority: Literal["companies_house", "external_rating"]
    identifier: str = ""
    status: str = ""
    charges: list[str] = Field(default_factory=list)
    secured_parties: list[str] = Field(default_factory=list)
    rating: str = ""
    notes: str = ""
    source_locator: str = ""


class SynthesisRequest(_Strict):
    """Orchestrator -> synthesis: the typed evidence to compose the answer from."""

    question: str
    issuer_name: str
    exposure: ExposureFindings
    resolutions: list[RegistryFinding] = Field(default_factory=list)


class FinalAnswer(_Strict):
    """synthesis -> orchestrator: the grounded answer + the stop justification."""

    answer: str
    stopped_reason: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"
