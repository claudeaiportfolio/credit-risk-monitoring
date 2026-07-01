"""The three sub-agents and how they run on the shared agent-core loop.

Each retrieval sub-agent (exposure, entity_resolution) is a real
``agent_core.AgentLoop`` — multi-turn tool round-trips routed through the
``llm-provider`` seam — given a :class:`~credit_risk_monitoring.agent.tools.ToolRouter`
scoped (and authorised) to just its tools. The synthesis sub-agent has no tools;
it **streams** its answer through the seam (the streaming path the orchestrator
uses to stress the seam end-to-end).

A sub-agent's free-text final answer is parsed and validated into its typed
handoff contract here (:func:`parse_contract`) — the boundary where an
ill-formed hand-off is caught instead of propagated.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from agent_core import AgentLoop, Tracer
from llm_provider import LLMProvider, Message, ProviderConfig, get_provider
from pydantic import BaseModel, ValidationError

from credit_risk_monitoring.agent.contracts import (
    ExposureFindings,
    FinalAnswer,
    InvestigationRequest,
    RegistryFinding,
    ResolutionRequest,
    SynthesisRequest,
)
from credit_risk_monitoring.agent.tools import ToolRouter

# Agent model for the whole arm. Opus is the frontier model the architecture
# round stresses; env-overridable.
AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "8"))


@dataclass
class InjectableAgentLoop(AgentLoop):
    """``AgentLoop`` whose provider can be injected (a fake in tests, or a
    key-from-Key-Vault client in prod) instead of being built from the env."""

    injected_provider: LLMProvider | None = None

    def _make_provider(self) -> LLMProvider:
        if self.injected_provider is not None:
            return self.injected_provider
        return super()._make_provider()


@dataclass
class SubAgentResult:
    final_text: str
    input_tokens: int
    output_tokens: int
    tool_calls: int
    stop_reason: str


# --- contract parsing -------------------------------------------------------
class ContractError(RuntimeError):
    """A sub-agent returned output that does not satisfy its handoff contract."""


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model's final answer (tolerates fences/prose)."""
    fenced = text
    if "```" in fenced:
        # take the content of the last fenced block
        parts = fenced.split("```")
        for chunk in reversed(parts):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{"):
                fenced = c
                break
    start = fenced.find("{")
    end = fenced.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ContractError(f"no JSON object found in sub-agent output: {text[:200]!r}")
    return fenced[start : end + 1]


def parse_contract[T: BaseModel](text: str, model: type[T]) -> T:
    """Validate a sub-agent's final answer into its Pydantic contract."""
    raw = _extract_json(text)
    try:
        return model.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ContractError(f"{model.__name__} contract violation: {exc}") from exc


# --- token accounting from the agent-core debug trace -----------------------
def sum_usage_from_trace(path: Path) -> tuple[int, int]:
    """Sum input/output tokens from an agent-core JSONL trace's claude_response
    events. Best-effort: a missing/garbled trace yields (0, 0)."""
    in_tok = out_tok = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("kind") == "claude_response":
                in_tok += int(ev.get("input_tokens", 0) or 0)
                out_tok += int(ev.get("output_tokens", 0) or 0)
    except (OSError, json.JSONDecodeError):
        return 0, 0
    return in_tok, out_tok


# --- system prompts ---------------------------------------------------------
_EXPOSURE_SYSTEM = """You are the EXPOSURE sub-agent of a credit-deterioration investigation. \
Your job: work the SEC/EDGAR side for one issuer and surface the named DOWNSTREAM references a \
credit analyst must resolve next. You have EDGAR tools only.

Discipline (branch correctness is everything — the WRONG number of hops fails the eval; take \
every hop the question needs and NOT ONE MORE).

RETRIEVAL ADDRESSING:
- General screen ("based on recent filings, any distress?"): the correct SINGLE move is one \
edgar_submissions_index call. If the recent filings are routine (no waiver, default, forbearance, \
going-concern, Chapter 11), set recommend_stop=true with NO leads and stop.
- A question that points at SPECIFIC distress event(s): NEVER call edgar_submissions_index — it is \
ONLY for the general screen above, and using it in a distress investigation is an extra wrong hop \
that fails the eval. To find AND open a filing (even when you don't know its accession), call \
edgar_filing with cik + form_type (e.g. "8-K") + a TIGHT date window (date_from/date_to) bracketing \
the event; it resolves the accession and returns the filing's directory AND its body in that ONE \
call. So:
  * NEVER guess/fabricate an accession, and NEVER pass a primary_document filename you are guessing.
  * edgar_filing ALREADY returns the body — do NOT then re-open the same filing by its www.sec.gov \
url (or by accession again). That re-open is a wasted, eval-failing duplicate hop. One filing = one \
edgar_filing call.
- Debtor/affiliate exhibit (the on-path grounding hop): when a bankruptcy filing states the \
filing debtors / subsidiaries are listed in an EXHIBIT (e.g. "the Debtors are listed in Exhibit \
99.1", an RSA debtor schedule), OPEN that exhibit with edgar_exhibit (its full archive URL from the \
filing's document list) to ground that your target foreign subsidiary is on the official debtor \
list — do this EVEN IF the question already names the subsidiary, because the exhibit is the \
primary-source confirmation that it is a filing debtor. Do NOT, however, open an exhibit that names \
a DIFFERENT thread's parties (e.g. a forbearance agreement naming a US ABS admin agent) when the \
subsidiary the question asks about is already established from the filing body — that exhibit is \
off this answer's path.

TWO OUTCOMES — pick the right one; do NOT confuse them:
1. recommend_stop=TRUE, leads=[]  ->  ONLY when there is genuinely nothing downstream to resolve: \
a healthy issuer (routine filings) or a benign filing (e.g. a maturity extension / commitment \
upsize with no covenant breach). Do NOT chase subsidiaries/agents of a benign filing.
2. recommend_stop=FALSE, leads=[...]  ->  the DISTRESS / multi-hop case. You MUST populate `leads` \
with every downstream reference the question asks you to resolve, then STOP CALLING TOOLS and emit \
the JSON. A distressed issuer with a question about a subsidiary's registry fact or a post-default \
rating is ALWAYS this case — returning recommend_stop=true or an empty leads list here is a FAILURE \
(the whole investigation then stops one hop short). If the question already NAMES the downstream \
entity you still emit it as a lead; you do not re-discover it, but you DO still open the debtor-list \
exhibit above when the filing points to one (it grounds the lead).

STOP DISCIPLINE (avoid over-investigation — the common failure): "stop" here means STOP CALLING \
MORE EDGAR TOOLS and emit the JSON — it does NOT mean recommend_stop. The instant you have (a) \
confirmed the distress event(s) the question is about (plus the debtor-list exhibit when one is \
referenced) and (b) named every downstream lead, emit the JSON. In particular a fact that lives at \
a UK REGISTRY (a subsidiary's registered charge, its charge-holder / secured party, its CH status \
or number) or at a RATING AGENCY is NOT yours to fetch — surface the entity as a lead; do NOT open \
the emergence / plan-confirmation / closing / charge-related 8-K to chase it. Open a FURTHER SEC \
filing ONLY when the question asks about a distinct event whose answer lives ONLY in an SEC filing \
and you do not already have it (e.g. a strict-foreclosure closing AND a later Chapter 7 filing are \
two separate SEC facts, so two filings are warranted; the appointment of administrators named in an \
8-K is an SEC fact worth the filing).

Lead kinds: uk_company (a UK/foreign subsidiary to resolve at a registry), rating_subject (an \
issuer whose external rating moved after a confirmed default), us_facility, other. Emit a \
uk_company lead for a foreign subsidiary whose registry fact the question asks about, and a \
rating_subject lead when the question asks about a rating after a confirmed default.

When finished, STOP calling tools and output ONLY a single JSON object, no prose, matching:
{"issuer_name": str, "distress_summary": str, "leads": [{"name": str, "kind": str, \
"jurisdiction": str, "rationale": str, "source_locator": str}], "recommend_stop": bool, \
"stop_rationale": str}"""

_RESOLUTION_SYSTEM = """You are the ENTITY_RESOLUTION sub-agent. You resolve ONE named downstream \
reference at its external authority. You have companies_house and external_rating tools only.

Discipline (the WRONG number of registry calls fails the eval — fetch only what the question \
needs, then stop):
- uk_company lead: you do NOT need a separate search step. FIRST call companies_house \
operation=profile with query=<the company name>: it resolves the name to the registered company \
number (exact-name match) and returns the company record (status/type) in ONE call. Then fetch AT \
MOST ONE further endpoint — the one the QUESTION actually requires:
  * a registered-charge / secured-party / charge-holder question -> operation=charges (pass the \
company_number from the profile record);
  * an administration / insolvency / appointment / "what is its status and number" question -> \
profile ALONE already gives the status and number, so STOP after profile — do NOT call charges or \
filing_history (the appointment/administrator detail comes from the SEC filing, not a second \
registry call).
  Never call operation=search separately, never profile multiple candidate companies, and never \
call officers or any endpoint the question does not require. The correct depth is profile alone \
(status/number questions) or profile + one detail endpoint (charge questions) — nothing more.
- rating_subject lead: call external_rating ONCE with the issuer as subject. If it returns \
UNVERIFIED (no provider configured), report the rating as unverified and STOP — never invent a \
rating value and never re-call the tool hoping for a different result.

When finished, STOP calling tools and output ONLY a single JSON object, no prose, matching:
{"entity_name": str, "authority": "companies_house"|"external_rating", "identifier": str, \
"status": str, "charges": [str], "secured_parties": [str], "rating": str, "notes": str, \
"source_locator": str}"""

_SYNTHESIS_SYSTEM = """You are the SYNTHESIS sub-agent of a credit-deterioration investigation. \
You have NO tools. Compose the final answer to the analyst's question STRICTLY from the typed \
findings provided (the exposure result and any registry/rating resolutions). Rules:
- Ground every claim in the findings; do not add facts, charge ids, dates, parties, or ratings \
that are not present in them.
- If exposure recommended stopping (healthy issuer / benign filing), the correct answer is that \
there is no credit deterioration warranting escalation, and that further investigation is not \
warranted — do NOT invent a credit event.
- Be specific and concise: name the concrete downstream fact (the charge id/holder, the registry \
status, the named agent, the rating) when the findings contain it.
Answer in plain prose (no JSON)."""


# --- running the retrieval sub-agents ---------------------------------------
def _build_loop(
    *,
    router: ToolRouter,
    system_prompt: str,
    debug_trace_dir: Path,
    provider: LLMProvider | None,
    model: str,
) -> InjectableAgentLoop:
    tracer = Tracer(trace_dir=debug_trace_dir, mirror_to_stderr=False)
    return InjectableAgentLoop(
        mcp=router,  # type: ignore[arg-type]  # duck-typed: tool_specs()+call_tool()
        tracer=tracer,
        model=model,
        max_turns=MAX_TURNS,
        system_prompt=system_prompt,
        prompt_version="c3a-arm-a-v1",
        enable_prompt_caching=True,
        temperature=0.0,
        injected_provider=provider,
    )


async def run_exposure(
    request: InvestigationRequest,
    *,
    router: ToolRouter,
    debug_trace_dir: Path,
    provider: LLMProvider | None = None,
    model: str = AGENT_MODEL,
) -> tuple[ExposureFindings, SubAgentResult]:
    loop = _build_loop(
        router=router, system_prompt=_EXPOSURE_SYSTEM, debug_trace_dir=debug_trace_dir,
        provider=provider, model=model,
    )
    prompt = (
        f"Issuer: {request.issuer_name} (CIK {request.cik}).\n"
        f"Question: {request.question}\n\n"
        "Investigate the SEC/EDGAR side and return the ExposureFindings JSON."
    )
    result = await loop.run(prompt)
    findings = parse_contract(result.final_text, ExposureFindings)
    in_tok, out_tok = sum_usage_from_trace(Path(result.trace_path))
    return findings, SubAgentResult(
        result.final_text, in_tok, out_tok, result.tool_calls, result.stop_reason
    )


async def run_entity_resolution(
    request: ResolutionRequest,
    *,
    router: ToolRouter,
    debug_trace_dir: Path,
    provider: LLMProvider | None = None,
    model: str = AGENT_MODEL,
) -> tuple[RegistryFinding, SubAgentResult]:
    loop = _build_loop(
        router=router, system_prompt=_RESOLUTION_SYSTEM, debug_trace_dir=debug_trace_dir,
        provider=provider, model=model,
    )
    lead = request.lead
    prompt = (
        f"Question: {request.question}\n\n"
        f"Lead to resolve:\n- name: {lead.name}\n- kind: {lead.kind}\n"
        f"- jurisdiction: {lead.jurisdiction}\n- rationale: {lead.rationale}\n"
        f"- source: {lead.source_locator}\n\n"
        "Resolve this lead at its authority and return the RegistryFinding JSON."
    )
    result = await loop.run(prompt)
    finding = parse_contract(result.final_text, RegistryFinding)
    in_tok, out_tok = sum_usage_from_trace(Path(result.trace_path))
    return finding, SubAgentResult(
        result.final_text, in_tok, out_tok, result.tool_calls, result.stop_reason
    )


# --- synthesis (streaming, no tools) ----------------------------------------
def _synthesis_prompt(request: SynthesisRequest) -> str:
    findings = {
        "question": request.question,
        "issuer": request.issuer_name,
        "exposure": request.exposure.model_dump(),
        "resolutions": [r.model_dump() for r in request.resolutions],
    }
    return (
        "Compose the final analyst answer from these typed findings (JSON):\n\n"
        + json.dumps(findings, indent=2)
        + "\n\nAnswer the question grounded strictly in the findings above."
    )


async def stream_synthesis(
    request: SynthesisRequest,
    *,
    provider: LLMProvider | None = None,
    model: str = AGENT_MODEL,
) -> tuple[FinalAnswer, SubAgentResult]:
    """Stream the final answer through the provider seam, accumulating text+usage."""
    prov = provider or get_provider()
    config = ProviderConfig(model=model, system=_SYNTHESIS_SYSTEM, max_tokens=1500, temperature=0.0)
    messages = [Message(role="user", content=_synthesis_prompt(request))]

    chunks: list[str] = []
    in_tok = out_tok = 0
    stop = "end_turn"
    async for event in prov.stream(messages, config=config):
        etype = getattr(event, "type", "")
        if etype == "text_delta":
            chunks.append(event.text)  # type: ignore[attr-defined]
        elif etype == "message_done":
            stop = event.stop_reason  # type: ignore[attr-defined]
            in_tok = event.usage.input_tokens  # type: ignore[attr-defined]
            out_tok = event.usage.output_tokens  # type: ignore[attr-defined]
    answer_text = "".join(chunks).strip()
    stopped_reason = request.exposure.stop_rationale if request.exposure.recommend_stop else ""
    final = FinalAnswer(
        answer=answer_text,
        stopped_reason=stopped_reason,
        confidence="high" if request.resolutions or request.exposure.recommend_stop else "medium",
    )
    return final, SubAgentResult(answer_text, in_tok, out_tok, 0, stop)
