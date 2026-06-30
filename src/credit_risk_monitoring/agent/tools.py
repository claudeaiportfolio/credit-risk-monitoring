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
import re
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
# Filtered (form/date) renders surface more rows than the newest-25 screen, since
# a date-bounded form query legitimately returns a handful-to-dozens of filings.
_FILTERED_RENDER_CAP = 50


def _h_submissions_index(clients: Clients, args: dict) -> HopOutcome:
    cik = str(args["cik"])
    form_type = _opt_str(args.get("form_type"))
    date_from = _opt_str(args.get("date_from"))
    date_to = _opt_str(args.get("date_to"))
    paginate = bool(args.get("paginate", False))
    filtered = bool(form_type or date_from or date_to)

    index = clients.edgar.fetch_submissions_index(cik, paginate=paginate)
    # If the model is reaching for a filing older than the loaded recent window,
    # paginate into the older files automatically so the date filter can reach it
    # (the recent block holds only the most recent ~1000 filings).
    if not paginate and date_from:
        oldest = index.oldest_filing_date
        if oldest and date_from < oldest:
            index = clients.edgar.fetch_submissions_index(cik, paginate=True)

    text = index.context_block(
        limit=_FILTERED_RENDER_CAP if filtered else 25,
        form_type=form_type,
        date_from=date_from,
        date_to=date_to,
    )
    if filtered:
        n = len(index.matching(form_type=form_type, date_from=date_from, date_to=date_to))
        finding = f"{n} filing(s) matching form/date filter for {index.issuer_name}"
    else:
        finding = f"recent-filings index for {index.issuer_name}"
    return HopOutcome(SourceType.EDGAR_SUBMISSIONS_INDEX, text, index.locator, finding=finding)


def _h_filing(clients: Clients, args: dict) -> HopOutcome:
    url = _opt_str(args.get("url"))
    if url:
        doc = clients.edgar.fetch_document(url)
        return HopOutcome(SourceType.EDGAR_FILING, doc.context_block(), doc.locator, "filing body")

    cik = str(args["cik"])
    accession = _opt_str(args.get("accession"))
    primary_document = _opt_str(args.get("primary_document"))
    resolved_note = ""

    # If the accession is unknown, resolve it from the issuer's index by
    # form type + filing-date window. This addressing lookup is folded INTO this
    # one EDGAR_FILING hop (it is not a separate scored source) so a historical
    # filing is openable from "the issuer's 8-K around <event date>".
    if accession is None:
        form_type = _opt_str(args.get("form_type"))
        date_from = _opt_str(args.get("date_from"))
        date_to = _opt_str(args.get("date_to"))
        if not (form_type or date_from or date_to):
            raise ValueError(
                "edgar_filing needs an accession, a url, or a form_type/date_from/date_to "
                "window to locate the filing"
            )
        matches = clients.edgar.find_filings(
            cik, form_type=form_type, date_from=date_from, date_to=date_to
        )
        if not matches:
            raise ValueError(
                f"no filing matches form={form_type} {date_from}..{date_to} for CIK {cik}; "
                "widen the date window or check the form type"
            )
        chosen = matches[0]  # newest within the window
        accession = chosen.accession
        primary_document = primary_document or chosen.primary_document or None
        if len(matches) > 1:
            others = ", ".join(f"{m.form} {m.filing_date} acc {m.accession}" for m in matches[1:6])
            resolved_note = (
                f"\n\n[resolved to the newest of {len(matches)} matching filings: "
                f"opened {chosen.form} {chosen.filing_date} acc {chosen.accession}. "
                f"Other matches: {others}. If this is the wrong filing, re-open with a "
                "tighter date window or the exact accession.]"
            )

    directory, body = clients.edgar.fetch_filing(cik, accession, primary_document=primary_document)
    text = directory.index_block() + "\n\n" + body.context_block() + resolved_note
    return HopOutcome(
        SourceType.EDGAR_FILING,
        text,
        directory.locator,
        finding=f"filing body + {len(directory.exhibits())} exhibit(s)",
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
    number = _opt_str(args.get("company_number"))
    query = _opt_str(args.get("query"))
    if op == "search":
        if query is None:
            raise ValueError("companies_house operation=search requires a query (company name)")
        items = ch.search_companies(query)
        lines = [
            f"- {it.get('company_name')} (no. {it.get('company_number')}, {it.get('company_status')})"
            for it in items[:20]
        ]
        text = f"Companies House search for {query!r}:\n" + ("\n".join(lines) or "- (no matches)")
        return HopOutcome(SourceType.COMPANIES_HOUSE, text, f"CH search {query!r}", "registry search")

    # record/charges/officers/filing_history need a company NUMBER. If the caller
    # only has a NAME, resolve it to a number HERE (a name->number search folded
    # INTO this one registry hop, not a separate scored source) and pick the
    # exact-name match — so the agent resolves the right subsidiary in one hop
    # instead of profiling every fuzzy search result.
    resolved_note = ""
    if number is None:
        if query is None:
            raise ValueError(
                f"companies_house operation={op!r} requires company_number or query (a company name)"
            )
        number, resolved_note = _resolve_company_number(ch, query)
    number = str(number)
    if op == "profile":
        profile = ch.get_company(number)
        return HopOutcome(
            SourceType.COMPANIES_HOUSE,
            profile.context_block() + resolved_note,
            profile.locator,
            "company record",
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
                    "Fetch an SEC issuer's filings index (data.sec.gov) by CIK. "
                    "With NO filter it shows only the newest ~25 filings — the right "
                    "SINGLE move to SCREEN a healthy issuer for distress (shows that "
                    "filings exist, not their contents). To OPEN a specific historical "
                    "filing whose accession you don't know, prefer edgar_filing with "
                    "form_type + a date window (it resolves and opens in one step). "
                    "This tool's optional form_type/date_from/date_to filter is for "
                    "BROWSING an issuer's filing history (matching rows are returned "
                    "with accession numbers even when they sit hundreds deep; older "
                    "filings are paginated in automatically when date_from predates "
                    "the recent set)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "cik": {"type": "string", "description": "Issuer CIK (any format)."},
                        "form_type": {
                            "type": "string",
                            "description": (
                                "Filter to one form, e.g. \"8-K\" (also matches its \"/A\" "
                                "amendments), \"10-K\", \"10-Q\"."
                            ),
                        },
                        "date_from": {
                            "type": "string",
                            "description": "Inclusive lower filing-date bound, ISO YYYY-MM-DD.",
                        },
                        "date_to": {
                            "type": "string",
                            "description": "Inclusive upper filing-date bound, ISO YYYY-MM-DD.",
                        },
                        "paginate": {
                            "type": "boolean",
                            "description": (
                                "Force-follow older paginated filing pages (default false; "
                                "set automatically when a date_from predates the recent set)."
                            ),
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
                    "document body (e.g. an 8-K). Returns the exhibit list so you can open "
                    "an exhibit next. Three ways to locate it:\n"
                    "  1. cik + accession (when you already know the accession), or\n"
                    "  2. cik + form_type + a date window (date_from/date_to) — USE THIS to "
                    "open a historical filing whose accession you don't know (e.g. an issuer's "
                    "8-K around a known 2020 event): the accession is resolved for you and the "
                    "filing opened in this one step. Use the TIGHTEST window you can — set "
                    "date_from = date_to to the exact filing date if you know it — so the right "
                    "filing is opened (the newest match in the window is opened), or\n"
                    "  3. a direct www.sec.gov/Archives document url."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "cik": {"type": "string"},
                        "accession": {
                            "type": "string",
                            "description": "Accession number, e.g. 0001104659-20-096796.",
                        },
                        "form_type": {
                            "type": "string",
                            "description": (
                                "With a date window instead of an accession: the form to open, "
                                "e.g. \"8-K\"."
                            ),
                        },
                        "date_from": {
                            "type": "string",
                            "description": "Inclusive lower filing-date bound (ISO YYYY-MM-DD).",
                        },
                        "date_to": {
                            "type": "string",
                            "description": "Inclusive upper filing-date bound (ISO YYYY-MM-DD).",
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
                    "operation=profile (the company record: status/type), charges (registered "
                    "security + who holds it), officers, filing_history, or search (raw name "
                    "lookup). For profile/charges/officers/filing_history you may pass EITHER a "
                    "company_number OR query=<the company name>: a name is resolved to its "
                    "registered number for you (exact-name match) within that one call, so you "
                    "do NOT need a separate search step and should NOT profile multiple "
                    "candidates. Make only the calls the question needs."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["search", "profile", "charges", "officers", "filing_history"],
                        },
                        "company_number": {"type": "string"},
                        "query": {
                            "type": "string",
                            "description": (
                                "A company name — used as the search term (operation=search) or "
                                "resolved to a number for profile/charges/officers/filing_history."
                            ),
                        },
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


def _opt_str(value: object) -> str | None:
    """Normalise an optional string arg: blanks/None -> None, else stripped str."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


_COMPANY_SUFFIX_RE = re.compile(
    r"\b(limited|ltd|plc|llp|llc|inc|corporation|corp|company|co|group|holdings?)\b"
)


def _normalize_company_name(name: str) -> str:
    """Fold a company name to a comparable core: lowercase, drop punctuation and
    common legal suffixes (Ltd/Limited/PLC/…) so ``"Ensco Global Resources Ltd"``
    and ``"ENSCO GLOBAL RESOURCES LIMITED"`` compare equal."""
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    s = _COMPANY_SUFFIX_RE.sub(" ", s)
    return " ".join(s.split())


def _resolve_company_number(ch: CompaniesHouseClient, query: str) -> tuple[str, str]:
    """Resolve a company name to its registered number via search, picking the
    EXACT normalized-name match (falling back to a containment match, then the
    top hit). Returns ``(number, note)``; raises if the register has no matches.

    This is the name->number addressing lookup, folded into the calling registry
    hop — it is deliberately NOT recorded as a separate scored source."""
    items = ch.search_companies(query)
    if not items:
        raise ValueError(f"no Companies House company matches name {query!r}")
    nq = _normalize_company_name(query)
    exact = [it for it in items if _normalize_company_name(str(it.get("company_name", ""))) == nq]
    contains = [
        it
        for it in items
        if nq and nq in _normalize_company_name(str(it.get("company_name", "")))
    ]
    chosen = (exact or contains or items)[0]
    num = _opt_str(chosen.get("company_number"))
    if num is None:
        raise ValueError(f"Companies House match for {query!r} has no company number")
    note = (
        f"\n[resolved name {query!r} -> {chosen.get('company_name')} "
        f"(no. {num}, status {chosen.get('company_status')})]"
    )
    return num, note


def _guess_source_type(tool_name: str) -> SourceType:
    return SourceType(tool_name)


def _locator_hint(tool_name: str, args: dict) -> str:
    for k in ("accession", "url", "company_number", "subject", "query", "cik"):
        if args.get(k):
            return f"{tool_name}:{args[k]}"
    return tool_name + ":" + json.dumps(args, default=str)[:80]
