"""The parent orchestrator — sequences typed sub-agent handoffs, emits the trace.

The orchestrator is deliberately a **deterministic** driver, not a second LLM
loop: the intelligence (which filing to open, which exhibit, when to stop) lives
in the sub-agents' real agent-core loops; the orchestrator decides only the
*delegation sequence* from the typed contracts the sub-agents return. That makes
the control flow predictable, auditable, and unit-testable — the production
posture for an agent whose value is *branch correctness*.

Flow:

    exposure  ->  ExposureFindings
        |             |
        |   recommend_stop / no leads ? -> straight to synthesis
        v
    entity_resolution (per resolvable lead)  ->  RegistryFinding[]
        v
    synthesis (streamed)  ->  FinalAnswer

The whole investigation writes ONE scored trace via the C2 ``TraceWriter`` — one
``tool_use`` per retrieval hop, named by canonical source type, regardless of
which sub-agent made it — so the existing branch-correctness ``CheckSuite``
scores Arm A unchanged. Each sub-agent runs under a short-TTL, scope-limited
tool token from the shared :class:`AuthorityBroker`; every action is audited.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from llm_provider import LLMProvider

from credit_risk_monitoring.agent import subagents
from credit_risk_monitoring.agent.audit import AuditSink, make_audit_sink_from_env
from credit_risk_monitoring.agent.authz import AuthContext, AuthorityBroker
from credit_risk_monitoring.agent.companies_house import CompaniesHouseClient
from credit_risk_monitoring.agent.contracts import (
    FinalAnswer,
    InvestigationRequest,
    RegistryFinding,
    ResolutionRequest,
    SynthesisRequest,
)
from credit_risk_monitoring.agent.rating import RatingClient
from credit_risk_monitoring.agent.subagents import ContractError
from credit_risk_monitoring.agent.tools import Clients, ToolRouter
from credit_risk_monitoring.fixtures import Fixture
from credit_risk_monitoring.sources import EdgarClient, SourceType
from credit_risk_monitoring.trace import TraceWriter

logger = logging.getLogger(__name__)

# Per-sub-agent tool scopes (least privilege). These ARE the kill-switch
# boundaries: a token only grants its role's tools.
EXPOSURE_TOOLS = (
    SourceType.EDGAR_SUBMISSIONS_INDEX.value,
    SourceType.EDGAR_FILING.value,
    SourceType.EDGAR_EXHIBIT.value,
)
RESOLUTION_TOOLS = (
    SourceType.COMPANIES_HOUSE.value,
    SourceType.EXTERNAL_RATING.value,
)

_RESOLVABLE_LEAD_KINDS = {"uk_company", "rating_subject"}


@dataclass
class AgentRunResult:
    fixture_id: str
    trace_path: Path
    depth_reached: int
    final_answer: str
    stop_recommended: bool
    resolution_count: int
    audit_event_count: int
    error: str | None = None


def build_clients() -> Clients:
    """Build the retrieval spines from the environment.

    EDGAR + rating are always available (rating degrades to UNVERIFIED without a
    provider). Companies House is built only when its key is present; without it,
    registry hops fail loudly as tool errors rather than fabricating.
    """
    ch: CompaniesHouseClient | None = None
    if os.environ.get("COMPANIES_HOUSE_API_KEY"):
        ch = CompaniesHouseClient()
    return Clients(edgar=EdgarClient(), companies_house=ch, rating=RatingClient())


@dataclass
class Orchestrator:
    """Drives one investigation end-to-end and emits the scored trace.

    ``provider`` injects a fake/seam provider for both the sub-agent loops and
    the synthesis stream (None -> built from ``LLM_PROVIDER``). ``clients`` and
    ``broker`` are injectable for tests; defaults are built from the environment.
    """

    provider: LLMProvider | None = None
    clients: Clients | None = None
    broker: AuthorityBroker | None = None
    audit: AuditSink | None = None
    model: str = subagents.AGENT_MODEL
    debug_trace_dir: Path = field(default_factory=lambda: Path("traces/agent-debug"))

    def __post_init__(self) -> None:
        if self.audit is None and self.broker is None:
            self.audit = make_audit_sink_from_env()
        if self.broker is None:
            self.broker = AuthorityBroker(audit=self.audit)
        if self.clients is None:
            self.clients = build_clients()

    async def investigate(self, fixture: Fixture, *, trace_dir: Path) -> AgentRunResult:
        assert self.broker is not None and self.clients is not None
        tw = TraceWriter(
            run_id=f"arm-a/{fixture.id}",
            query_id=fixture.id,
            category=fixture.classification,
            question=fixture.question,
            model=self.model,
            expected_chain=fixture.expected_chain_values,
            skills_enabled=False,
            prompt_version="c3a-arm-a-v1",
            max_turns=subagents.MAX_TURNS,
            note="arm-a-orchestrator-subagents",
        )
        tw.start()
        turn = 0
        error: str | None = None
        final = FinalAnswer(answer="")
        resolutions: list[RegistryFinding] = []

        try:
            # --- Phase 1: exposure (SEC/EDGAR side) -----------------------
            turn += 1
            exposure_router = self._router(EXPOSURE_TOOLS, "exposure", tw, turn)
            findings, exp = await subagents.run_exposure(
                InvestigationRequest(
                    issuer_name=fixture.issuer_name, cik=fixture.cik, question=fixture.question
                ),
                router=exposure_router,
                debug_trace_dir=self.debug_trace_dir,
                provider=self.provider,
                model=self.model,
            )
            tw.record_turn(
                stop_reason="tool_use", input_tokens=exp.input_tokens, output_tokens=exp.output_tokens
            )

            # --- Phase 2: resolve each downstream lead --------------------
            if not findings.recommend_stop:
                resolution_router = self._router(RESOLUTION_TOOLS, "entity_resolution", tw, turn)
                for lead in findings.leads:
                    if lead.kind not in _RESOLVABLE_LEAD_KINDS:
                        continue
                    turn += 1
                    resolution_router.turn = turn
                    finding, res = await subagents.run_entity_resolution(
                        ResolutionRequest(lead=lead, question=fixture.question),
                        router=resolution_router,
                        debug_trace_dir=self.debug_trace_dir,
                        provider=self.provider,
                        model=self.model,
                    )
                    resolutions.append(finding)
                    tw.record_turn(
                        stop_reason="tool_use",
                        input_tokens=res.input_tokens,
                        output_tokens=res.output_tokens,
                    )

            # --- Phase 3: synthesis (streamed, no tools) ------------------
            turn += 1
            final, syn = await subagents.stream_synthesis(
                SynthesisRequest(
                    question=fixture.question,
                    issuer_name=fixture.issuer_name,
                    exposure=findings,
                    resolutions=resolutions,
                ),
                provider=self.provider,
                model=self.model,
            )
            tw.record_turn(
                stop_reason="end_turn", input_tokens=syn.input_tokens, output_tokens=syn.output_tokens
            )
            stop_recommended = findings.recommend_stop
        except ContractError as exc:
            error = f"contract violation: {exc}"
            logger.error("investigation %s failed: %s", fixture.id, error)
            tw.record_turn(stop_reason="end_turn")
            final = FinalAnswer(answer=f"[investigation incomplete: {error}]")
            stop_recommended = False
        except Exception as exc:  # noqa: BLE001 — always finish the trace
            # User-visible/trace fields carry only the exception TYPE: a crashing
            # httpx/psycopg error can embed a credential-bearing URL/DSN in its
            # message. The full detail goes to the log sink via logger.exception.
            error = type(exc).__name__
            logger.exception("investigation %s crashed", fixture.id)
            tw.record_turn(stop_reason="end_turn")
            final = FinalAnswer(answer=f"[investigation crashed: {error}]")
            stop_recommended = False

        tw.finish(final_text=final.answer, stop_reason="end_turn")
        path = tw.write(trace_dir)
        return AgentRunResult(
            fixture_id=fixture.id,
            trace_path=path,
            depth_reached=tw.depth_reached,
            final_answer=final.answer,
            stop_recommended=stop_recommended,
            resolution_count=len(resolutions),
            audit_event_count=_audit_count(self.broker),
            error=error,
        )

    def _router(
        self, tools: tuple[str, ...], agent_id: str, tw: TraceWriter, turn: int
    ) -> ToolRouter:
        assert self.broker is not None and self.clients is not None
        token = self.broker.mint(agent_id, set(tools))
        ctx = AuthContext(agent_id=agent_id, token=token, broker=self.broker, scopes=frozenset(tools))
        return ToolRouter(
            auth=ctx, allowed_tools=tools, clients=self.clients, trace=tw, turn=turn
        )

    def close(self) -> None:
        if self.clients is not None:
            self.clients.close()


def _audit_count(broker: AuthorityBroker) -> int:
    sink = broker.audit
    events = getattr(sink, "events", None)
    return len(events) if isinstance(events, list) else -1


# Exported for re-use by the package __init__.
__all__ = ["AgentRunResult", "Orchestrator", "build_clients"]
