"""The tool surface the sub-agents act through — and the auth boundary.

A :class:`ToolRouter` is what each sub-agent's ``agent-core`` loop sees as its
``mcp`` (it satisfies the duck-typed ``tool_specs()`` + ``call_tool()``
interface the loop uses), so no real MCP server / Auth0 round-trip is needed —
the tools are local, in-process retrieval against the EDGAR / Companies House /
rating spines.

Every ``call_tool`` does four things, in order:

1. **Authorize** at the boundary — the broker checks the sub-agent's token
   (scope + TTL), the deny-list, and any admin revoke. A refusal returns an
   ``ERROR:`` string (which the loop surfaces to the model as a tool error) and
   records **no** retrieval hop — a policy refusal is not a retrieval.
2. **Execute** the retrieval (off the event loop via ``to_thread`` so the sync,
   rate-limited HTTP clients don't block).
3. **Record the hop** into the shared C2 ``TraceWriter`` — one ``tool_use``
   named by the canonical :class:`SourceType` — so the existing
   branch-correctness suite scores Arm A unchanged. Failed retrievals are
   recorded as ``tool_error`` (honest: a broken hop fails scoring).
4. **Audit** the execution (who/what/when/result) to the audit log.

The tool names are the canonical source types, and each router is constructed
with only the subset of tools its sub-agent role is scoped to (least privilege),
so the token scope, the deny-list, and the chain the scorer reads all line up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from llm_provider import ToolSpec

from credit_risk_monitoring.agent.audit import AuditEvent
from credit_risk_monitoring.agent.authz import AuthContext
from credit_risk_monitoring.agent.companies_house import CompaniesHouseClient
from credit_risk_monitoring.agent.rating import RatingClient
from credit_risk_monitoring.sources import EdgarClient, SourceType
from credit_risk_monitoring.trace import TraceWriter


@dataclass
class HopOutcome:
    """What a tool handler produced for one retrieval hop."""

    source_type: SourceType
    result_text: str
    locator: str
    finding: str = ""


@dataclass
class Clients:
    """The retrieval spines the handlers dispatch to (injectable for tests)."""

    edgar: EdgarClient
    companies_house: CompaniesHouseClient | None = None
    rating: RatingClient | None = None

    def close(self) -> None:
        for c in (self.edgar, self.companies_house, self.rating):
            if c is not None:
                with contextlib.suppress(Exception):
                    c.close()


# A handler maps validated tool args -> a HopOutcome (or raises on a retrieval
# failure, which the router records as a tool_error).
Handler = Callable[["Clients", dict], HopOutcome]


# --- handlers ---------------------------------------------------------------
def _h_submissions_index(clients: Clients, args: dict) -> HopOutcome:
    cik = str(args["cik"])
    index = clients.edgar.fetch_submissions_index(cik, paginate=bool(args.get("paginate", False)))
    return HopOutcome(
        SourceType.EDGAR_SUBMISSIONS_INDEX,
        index.context_block(),
        index.locator,
        finding=f"recent-filings index for {index.issuer_name}",
    )


def _h_filing(clients: Clients, args: dict) -> HopOutcome:
    url = args.get("url")
    if url:
        doc = clients.edgar.fetch_document(str(url))
        return HopOutcome(SourceType.EDGAR_FILING, doc.context_block(), doc.locator, "filing body")
    cik = str(args["cik"])
    accession = str(args["accession"])
    directory, body = clients.edgar.fetch_filing(
        cik, accession, primary_document=args.get("primary_document")
    )
    text = directory.index_block() + "\n\n" + body.context_block()
    return HopOutcome(
        SourceType.EDGAR_FILING,
        text,
        directory.locator,
        finding=f"8-K body + {len(directory.exhibits())} exhibit(s)",
    )


def _h_exhibit(clients: Clients, args: dict) -> HopOutcome:
    url = str(args["url"])
    doc = clients.edgar.fetch_exhibit(url)
    return HopOutcome(SourceType.EDGAR_EXHIBIT, doc.context_block(), doc.locator, "exhibit body")


def _h_companies_house(clients: Clients, args: dict) -> HopOutcome:
    if clients.companies_house is None:
        raise RuntimeError("Companies House client not configured (no API key)")
    ch = clients.companies_house
    op = str(args.get("operation", "profile"))
    number = args.get("company_number")
    if op == "search":
        query = str(args["query"])
        items = ch.search_companies(query)
        lines = [
            f"- {it.get('company_name')} (no. {it.get('company_number')}, {it.get('company_status')})"
            for it in items[:20]
        ]
        text = f"Companies House search for {query!r}:\n" + ("\n".join(lines) or "- (no matches)")
        return HopOutcome(SourceType.COMPANIES_HOUSE, text, f"CH search {query!r}", "registry search")
    if number is None:
        raise ValueError(f"companies_house op {op!r} requires company_number")
    number = str(number)
    if op == "profile":
        profile = ch.get_company(number)
        return HopOutcome(
            SourceType.COMPANIES_HOUSE, profile.context_block(), profile.locator, "company record"
        )
    if op == "charges":
        charges = ch.get_charges(number)
        lines = [f"- {c.summary_line()}" for c in charges]
        text = f"Companies House charges for CH {number}:\n" + ("\n".join(lines) or "- (no charges)")
        return HopOutcome(SourceType.COMPANIES_HOUSE, text, f"CH {number} /charges", "charges")
    if op == "officers":
        officers = ch.get_officers(number)
        lines = [f"- {o.get('name')} ({o.get('officer_role')})" for o in officers[:40]]
        text = f"Companies House officers for CH {number}:\n" + ("\n".join(lines) or "- (none)")
        return HopOutcome(SourceType.COMPANIES_HOUSE, text, f"CH {number} /officers", "officers")
    if op == "filing_history":
        history = ch.get_filing_history(number)
        lines = [f"- {h.get('date')} {h.get('type')}: {h.get('description')}" for h in history[:40]]
        text = f"Companies House filing history for CH {number}:\n" + ("\n".join(lines) or "- (none)")
        return HopOutcome(
            SourceType.COMPANIES_HOUSE, text, f"CH {number} /filing-history", "filing history"
        )
    raise ValueError(f"unknown companies_house operation {op!r}")


def _h_rating(clients: Clients, args: dict) -> HopOutcome:
    if clients.rating is None:
        raise RuntimeError("rating client not configured")
    subject = str(args["subject"])
    result = clients.rating.get_rating(subject)
    return HopOutcome(
        SourceType.EXTERNAL_RATING, result.context_block(), result.source_locator, "rating action"
    )


@dataclass(frozen=True)
class ToolDef:
    spec: ToolSpec
    handler: Handler


# The full tool registry. Tool name == canonical source type so the agent-core
# trace and the scored C2 trace name hops identically. Sub-agents are scoped to
# subsets of these (see agent.subagents).
def _registry() -> dict[str, ToolDef]:
    return {
        SourceType.EDGAR_SUBMISSIONS_INDEX.value: ToolDef(
            ToolSpec(
                name=SourceType.EDGAR_SUBMISSIONS_INDEX.value,
                description=(
                    "Fetch an SEC issuer's recent-filings index (data.sec.gov) by CIK. "
                    "The right SINGLE move to screen a healthy issuer for distress — it "
                    "shows that filings exist, not their contents."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "cik": {"type": "string", "description": "Issuer CIK (any format)."},
                        "paginate": {
                            "type": "boolean",
                            "description": "Follow older paginated filing pages (default false).",
                        },
                    },
                    "required": ["cik"],
                },
            ),
            _h_submissions_index,
        ),
        SourceType.EDGAR_FILING.value: ToolDef(
            ToolSpec(
                name=SourceType.EDGAR_FILING.value,
                description=(
                    "Open a SPECIFIC SEC filing: its document directory plus the primary "
                    "document body (e.g. an 8-K). Provide cik+accession (preferred) or a "
                    "direct document url. Returns the exhibit list so you can open an exhibit next."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "cik": {"type": "string"},
                        "accession": {
                            "type": "string",
                            "description": "Accession number, e.g. 0001104659-20-096796.",
                        },
                        "primary_document": {
                            "type": "string",
                            "description": "Optional primary-document filename to pin the body.",
                        },
                        "url": {
                            "type": "string",
                            "description": "Alternative: a direct www.sec.gov/Archives document URL.",
                        },
                    },
                },
            ),
            _h_filing,
        ),
        SourceType.EDGAR_EXHIBIT.value: ToolDef(
            ToolSpec(
                name=SourceType.EDGAR_EXHIBIT.value,
                description=(
                    "Fetch the body of a named exhibit document (its full archive URL, from a "
                    "filing's directory) — e.g. an RSA or forbearance agreement that names the "
                    "subsidiaries/agents the filing body omits."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
            _h_exhibit,
        ),
        SourceType.COMPANIES_HOUSE.value: ToolDef(
            ToolSpec(
                name=SourceType.COMPANIES_HOUSE.value,
                description=(
                    "Query UK Companies House for a company named in an SEC exhibit. "
                    "operation=search (resolve a name->number), profile (status/type), "
                    "charges (registered security + who holds it), officers, filing_history. "
                    "Make only the calls the question needs."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["search", "profile", "charges", "officers", "filing_history"],
                        },
                        "company_number": {"type": "string"},
                        "query": {"type": "string", "description": "Name to search (operation=search)."},
                    },
                    "required": ["operation"],
                },
            ),
            _h_companies_house,
        ),
        SourceType.EXTERNAL_RATING.value: ToolDef(
            ToolSpec(
                name=SourceType.EXTERNAL_RATING.value,
                description=(
                    "Look up an issuer's external credit rating / rating action (e.g. an S&P "
                    "downgrade) after a confirmed default. Returns the rating as UNVERIFIED if no "
                    "rating provider is configured — report it as such, do not invent a rating."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"subject": {"type": "string"}},
                    "required": ["subject"],
                },
            ),
            _h_rating,
        ),
    }


TOOL_REGISTRY = _registry()


def specs_for(names: list[str] | tuple[str, ...]) -> list[ToolSpec]:
    return [TOOL_REGISTRY[n].spec for n in names]


class ToolRouter:
    """Per-sub-agent tool surface + the enforced auth boundary.

    Construct with the subset of tool names the role is scoped to; ``tool_specs``
    exposes exactly those to the loop, and ``call_tool`` enforces the broker on
    every call. ``turn`` is set by the orchestrator before each phase so recorded
    hops are attributed to the right turn in the scored trace.
    """

    def __init__(
        self,
        *,
        auth: AuthContext,
        allowed_tools: list[str] | tuple[str, ...],
        clients: Clients,
        trace: TraceWriter,
        turn: int = 1,
    ) -> None:
        unknown = set(allowed_tools) - set(TOOL_REGISTRY)
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        self._auth = auth
        self._allowed = tuple(allowed_tools)
        self._clients = clients
        self._trace = trace
        self.turn = turn

    def tool_specs(self) -> list[ToolSpec]:
        return specs_for(self._allowed)

    async def call_tool(self, name: str, arguments: dict) -> str:
        # 1. Authorize at the boundary (records the decision to the audit log).
        decision = self._auth.broker.authorize(self._auth.token, name)
        if not decision.allowed:
            return f"ERROR: authorization denied for {name!r}: {decision.reason}"

        if name not in self._allowed:
            # Defence-in-depth: scope said yes but this router doesn't serve it.
            return f"ERROR: tool {name!r} is not available to {self._auth.agent_id!r}"

        handler = TOOL_REGISTRY[name].handler
        # 2. Execute off the event loop (sync, rate-limited HTTP).
        try:
            outcome = await asyncio.to_thread(handler, self._clients, arguments)
        except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._trace.record_hop_error(
                _guess_source_type(name), locator=_locator_hint(name, arguments), error=error,
                turn=self.turn,
            )
            self._auth.broker.audit.record(
                AuditEvent(
                    agent_id=self._auth.agent_id, action="execute", tool=name,
                    allowed=True, reason="retrieval failed", detail=error[:300],
                )
            )
            return f"ERROR: {error}"

        # 3. Record the successful retrieval hop into the scored C2 trace.
        self._trace.record_hop(
            outcome.source_type,
            locator=outcome.locator,
            preview=outcome.result_text,
            finding=outcome.finding,
            turn=self.turn,
        )
        # 4. Audit the execution.
        self._auth.broker.audit.record(
            AuditEvent(
                agent_id=self._auth.agent_id, action="execute", tool=name, allowed=True,
                detail=f"{outcome.source_type.value} @ {outcome.locator}"[:300],
            )
        )
        return outcome.result_text


def _guess_source_type(tool_name: str) -> SourceType:
    return SourceType(tool_name)


def _locator_hint(tool_name: str, args: dict) -> str:
    for k in ("accession", "url", "company_number", "subject", "query", "cik"):
        if args.get(k):
            return f"{tool_name}:{args[k]}"
    return tool_name + ":" + json.dumps(args, default=str)[:80]
