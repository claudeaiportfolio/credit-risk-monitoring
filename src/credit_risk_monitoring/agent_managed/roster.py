"""Arm B roster — the Managed Agents control plane (create once, reference by ID).

This module builds the persisted Managed Agents resources: one **environment**
and the **agent roster**. Agents are persistent, versioned objects — created
once and referenced by ID on every session, NEVER in the request path (the #1
Managed Agents anti-pattern). :func:`ensure_roster` therefore reuses IDs passed
via the environment (``ARM_B_ENV_ID`` / ``ARM_B_COORDINATOR_ID`` / …) when
present, and only creates what's missing.

Roster count — derived from the fixtures, justified to the same standard as
Arm A
-----------------------------------------------------------------------------
Arm A justifies THREE sub-agents: two retrieval *domains* (US SEC vs. an
external authority — different clients, auth, rate limits) + a synthesis step
that must *not* retrieve (the over-investigation guard the trap control
catches). Managed Agents changes the arithmetic in exactly one place: the
**coordinator is itself a tool-less LLM** that receives the sub-agents' results
and composes the final answer. So MA's coordinator *natively fills the
synthesis role* — and, having no retrieval tools, it structurally cannot add a
hop, which is precisely the over-investigation guard Arm A implemented as a
separate tool-less ``synthesis`` sub-agent.

The minimum roster is therefore **two sub-agents under one coordinator**:

* **coordinator** (no tools) — sequences delegation AND synthesizes the grounded
  answer. Maps to Arm A's {deterministic orchestrator + tool-less synthesis
  sub-agent}, collapsed into one agent because in MA the coordinator is an LLM,
  not code you write.
* **exposure** (EDGAR custom tools) — the SEC/EDGAR retrieval domain. Identical
  role, prompt, and tool scope as Arm A's exposure sub-agent.
* **entity_resolution** (Companies House + rating custom tools) — the external
  authority retrieval domain. Identical role, prompt, tool scope as Arm A's.

A separate ``synthesis`` sub-agent would be a redundant extra ``opus-4-8``
thread for zero branch-correctness gain, so it is folded into the coordinator —
the honest "derive the minimum" answer for the MA topology. The two retrieval
domains stay distinct sub-agents for the same reasons Arm A gives (different
clients/auth/rate-limits + least-privilege per-sub-agent tool scoping).

Tool-exposure decision (documented per the "record deviations/decisions in the
artifact" convention)
-----------------------------------------------------------------------------
The tools are exposed as **custom tools** executed **host-side** over the SSE
stream (Managed Agents Pattern 9), NOT as a sandbox code-execution pip shim and
NOT as a remote MCP server. Rationale, against the task's constraints (keep the
tool surface equivalent to Arm A, reuse the tool code, no new public ingress):

1. **Reuses Arm A's EXACT tool code** — the custom-tool handlers are Arm A's own
   ``TOOL_REGISTRY`` handlers (the EDGAR spine + the Companies House / rating
   clients), imported and called directly. No repackaging into a pip shim, no
   re-implementation behind an MCP server — the strongest possible satisfaction
   of the portfolio centralise rule and the "same tool surface both arms" bar.
2. **No new public ingress** — the orchestrator holds the SSE stream open with
   the Anthropic API key and answers ``agent.custom_tool_use`` events with
   ``user.custom_tool_result`` over that same authenticated channel. It is a
   client, not a server; nothing unauthenticated listens (remote-MCP would have
   required Anthropic's orchestration layer to reach a public MCP endpoint).
3. **Secrets stay host-side** — ``COMPANIES_HOUSE_API_KEY`` /
   ``SEC_EDGAR_USER_AGENT`` never leave this process; no vault is needed. The
   Managed Agents sandbox container is not used for retrieval at all.

Residency implication (a comparison data point, not a blocker)
-----------------------------------------------------------------------------
Custom tools keep the *fetch and the secrets* host-side, but the *fetched
filing content* still flows through Anthropic: the tool RESULT (the 8-K body,
the exhibit text, the registry record) is returned via ``user.custom_tool_result``
to Anthropic's orchestration layer, where the coordinator and sub-agent
``opus-4-8`` models reason over it. Because Managed Agents runs the loop, the
hosted model inference layer necessarily sees the retrieved primary-source
content — this is the concrete point on which Managed Agents is
**ZDR / HIPAA-ineligible**, and it holds for ANY MA tool-exposure choice (the
sandbox-shim option would additionally run the fetch on Anthropic infra; the
custom-tools option does not, but the model-inference residency is unavoidable
under MA). Arm A, running the loop self-hosted, keeps both the fetch and the
model-context content on infrastructure the operator controls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from credit_risk_monitoring.agent.orchestrator import EXPOSURE_TOOLS, RESOLUTION_TOOLS
from credit_risk_monitoring.agent.subagents import (
    _EXPOSURE_SYSTEM as EXPOSURE_SYSTEM,
)
from credit_risk_monitoring.agent.subagents import (
    _RESOLUTION_SYSTEM as RESOLUTION_SYSTEM,
)
from credit_risk_monitoring.agent.tools import TOOL_REGISTRY

# Same model policy as Arm A: coordinator + sub-agents on opus-4-8, env-overridable.
ARM_B_MODEL = os.environ.get("ARM_B_MODEL", os.environ.get("AGENT_MODEL", "claude-opus-4-8"))

# The Managed Agents multi-agent beta surface. The SDK sets the
# `managed-agents-2026-04-01` beta header automatically on all
# client.beta.{agents,environments,sessions}.* calls, so we don't pass it here.


# --- the coordinator system prompt (orchestration + synthesis) --------------
# Mirrors Arm A's deterministic orchestrator flow (agent/orchestrator.py) AND
# its tool-less synthesis discipline (agent/subagents._SYNTHESIS_SYSTEM). The
# coordinator has NO retrieval tools, so it structurally cannot add a hop — the
# over-investigation guard the trap control exists to catch.
COORDINATOR_SYSTEM = """You are the COORDINATOR of a credit-deterioration investigation. You have \
NO retrieval tools of your own. You have two sub-agents you delegate to, and you compose the final \
grounded answer. Branch correctness is everything: the investigation must take the RIGHT sources in \
the RIGHT order to the RIGHT depth and STOP — the WRONG number of hops fails the eval.

YOUR SUB-AGENTS:
- exposure — works the SEC/EDGAR side for the issuer and returns an ExposureFindings JSON object with \
{distress_summary, leads:[{name, kind, jurisdiction, rationale, source_locator}], recommend_stop, \
stop_rationale}. It has the EDGAR tools; you do not.
- entity_resolution — resolves ONE named downstream lead at its external authority (UK Companies \
House, or a rating agency) and returns a RegistryFinding JSON object {entity_name, authority, \
identifier, status, charges, secured_parties, rating, notes}. It has the registry/rating tools.

DELEGATION SEQUENCE (strictly sequential — do NOT run sub-agents in parallel; the hop ORDER is \
scored):
1. FIRST delegate to exposure with the issuer name, CIK, and the question. Wait for its \
ExposureFindings.
2. Read recommend_stop:
   - recommend_stop = TRUE (healthy issuer / benign filing): do NOT delegate to entity_resolution. \
There is nothing downstream to resolve. Go straight to synthesis and report no credit deterioration \
warranting escalation. Delegating further here is the over-investigation failure the controls catch.
   - recommend_stop = FALSE: for EACH resolvable lead (kind uk_company or rating_subject), delegate \
to entity_resolution ONE lead at a time (pass the lead's name/kind/jurisdiction/rationale and the \
question), and collect the RegistryFinding. Do not resolve us_facility/other leads.
3. THEN synthesize: compose the final analyst answer STRICTLY from the typed findings (the \
ExposureFindings and any RegistryFindings). Rules:
   - Ground every claim in the findings; do NOT add facts, charge ids, dates, parties, or ratings \
that are not present in them. You have no tools — you cannot and must not retrieve anything yourself.
   - If exposure recommended stopping, the correct answer is that there is no credit deterioration \
warranting escalation and further investigation is not warranted — do NOT invent a credit event.
   - Be specific and concise: name the concrete downstream fact (the charge id/holder, the registry \
status, the named agent, the rating) when the findings contain it.

Emit your FINAL grounded answer as your LAST message, in plain prose (no JSON). That final message \
is the analyst's answer and the only thing scored for groundedness — make it self-contained: state \
the concrete downstream fact directly, with NO meta-commentary about delegation, sub-agents, or the \
protocol (do not write "the exposure sub-agent returned…" or "I'll go to synthesis")."""


@dataclass(frozen=True)
class Roster:
    """The persisted Managed Agents resource IDs for one Arm B deployment.

    ``coordinator_id`` is what a session references; the sub-agent IDs are wired
    into the coordinator's ``multiagent`` roster and are here for inspection /
    teardown. ``created`` records which resources this process created (vs.
    reused from the environment) so a caller can decide whether to archive.
    """

    environment_id: str
    coordinator_id: str
    exposure_id: str
    entity_resolution_id: str
    model: str = ARM_B_MODEL
    created: tuple[str, ...] = ()
    # The investigation-discipline Skill attached to every agent in this roster,
    # or "" when the roster has no Skill (the default). Set only by the measured
    # Skills experiment — see agent_managed/skill.py and docs/skills-experiment.md.
    skill_id: str = ""


def custom_tools_for(names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """Convert Arm A's ``ToolSpec``s (by tool name) into Managed Agents custom-tool
    definitions — REUSING the exact name/description/input_schema Arm A exposes,
    so a Managed sub-agent sees the identical tool surface as an Arm A sub-agent."""
    tools: list[dict[str, Any]] = []
    for name in names:
        spec = TOOL_REGISTRY[name].spec
        tools.append(
            {
                "type": "custom",
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
        )
    return tools


def _create_agent(
    client: Any,
    *,
    name: str,
    system: str,
    tools: list[dict[str, Any]],
    model: str,
    multiagent: dict[str, Any] | None = None,
    skills: list[dict[str, Any]] | None = None,
) -> Any:
    if skills:
        # Managed Agents load a Skill's files from the sandbox via the built-in
        # `read` tool, so attaching a Skill 400s at session-create unless `read`
        # is enabled on the agent's toolset. Enable ONLY `read` (the rest of the
        # built-in toolset stays disabled) — it is otherwise inert for this
        # custom-tool investigation (there are no sandbox files to read besides
        # the Skill), so the measured with/without delta remains attributable to
        # the Skill, not to a wider tool surface.
        tools = [
            *tools,
            {
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": False},
                "configs": [{"name": "read", "enabled": True}],
            },
        ]
    kwargs: dict[str, Any] = {"name": name, "model": model, "system": system, "tools": tools}
    if multiagent is not None:
        kwargs["multiagent"] = multiagent
    if skills:
        kwargs["skills"] = skills
    return client.beta.agents.create(**kwargs)


def ensure_roster(
    client: Any,
    *,
    model: str = ARM_B_MODEL,
    environment_id: str | None = None,
    coordinator_id: str | None = None,
    exposure_id: str | None = None,
    entity_resolution_id: str | None = None,
    skill_id: str | None = None,
) -> Roster:
    """Create (or reuse) the Arm B environment + agent roster and return the IDs.

    This is the **control plane** (create once, then reference by ID on every
    session). IDs may be supplied explicitly (or via ``ARM_B_ENV_ID`` /
    ``ARM_B_COORDINATOR_ID`` / ``ARM_B_EXPOSURE_ID`` /
    ``ARM_B_ENTITY_RESOLUTION_ID``) to reuse a previously-created roster across
    runs; anything missing is created. When the coordinator must be created, its
    two sub-agents are created first so their IDs can populate the coordinator's
    ``multiagent`` roster.

    ``skill_id`` (the measured Skills experiment; default: none) attaches the
    investigation-discipline Skill to EVERY agent in the roster — the coordinator
    (sequencing + synthesis discipline) and both retrieval sub-agents (the hop /
    exhibit / registry / stop discipline). Because a Skill is bound at agent-create
    time, a Skill-enabled roster is a distinct set of agents from the no-Skill
    roster, which is exactly what the with/without measurement compares.
    """
    environment_id = environment_id or os.environ.get("ARM_B_ENV_ID")
    coordinator_id = coordinator_id or os.environ.get("ARM_B_COORDINATOR_ID")
    exposure_id = exposure_id or os.environ.get("ARM_B_EXPOSURE_ID")
    entity_resolution_id = entity_resolution_id or os.environ.get("ARM_B_ENTITY_RESOLUTION_ID")

    from credit_risk_monitoring.agent_managed.skill import skill_reference

    skills = [skill_reference(skill_id)] if skill_id else None
    created: list[str] = []

    if environment_id is None:
        env = client.beta.environments.create(
            name=f"credit-risk-arm-b-{os.getpid()}",
            config={"type": "cloud", "networking": {"type": "unrestricted"}},
        )
        environment_id = env.id
        created.append("environment")

    # The coordinator needs its sub-agents' IDs, so create the sub-agents first
    # whenever the coordinator has to be (re)created.
    if coordinator_id is None:
        if exposure_id is None:
            exposure = _create_agent(
                client,
                name="credit-risk-exposure",
                system=EXPOSURE_SYSTEM,
                tools=custom_tools_for(EXPOSURE_TOOLS),
                model=model,
                skills=skills,
            )
            exposure_id = exposure.id
            created.append("exposure")
        if entity_resolution_id is None:
            resolution = _create_agent(
                client,
                name="credit-risk-entity-resolution",
                system=RESOLUTION_SYSTEM,
                tools=custom_tools_for(RESOLUTION_TOOLS),
                model=model,
                skills=skills,
            )
            entity_resolution_id = resolution.id
            created.append("entity_resolution")
        coordinator = _create_agent(
            client,
            name="credit-risk-coordinator",
            system=COORDINATOR_SYSTEM,
            tools=[],  # tool-less: delegates + synthesizes, structurally cannot retrieve
            model=model,
            multiagent={
                "type": "coordinator",
                "agents": [exposure_id, entity_resolution_id],
            },
            skills=skills,
        )
        coordinator_id = coordinator.id
        created.append("coordinator")

    # If a coordinator ID was supplied but not its sub-agents, we don't know
    # them; they're already wired into the coordinator's stored roster, so the
    # blanks are informational only.
    return Roster(
        environment_id=environment_id,
        coordinator_id=coordinator_id,
        exposure_id=exposure_id or "",
        entity_resolution_id=entity_resolution_id or "",
        model=model,
        created=tuple(created),
        skill_id=skill_id or "",
    )
