"""Orchestrator + handoff contracts + the auth boundary, and the end-to-end
trace round-trip through the existing C2 branch-correctness CheckSuite.

Everything is offline: a scripted FakeProvider drives the agent-core loops and
the synthesis stream, and the EDGAR/Companies House clients run on mocked HTTP.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from agent_evals import Outcome, load_directory, score_all

from credit_risk_monitoring.agent.audit import InMemoryAuditSink
from credit_risk_monitoring.agent.authz import AuthContext, AuthorityBroker
from credit_risk_monitoring.agent.companies_house import CompaniesHouseClient
from credit_risk_monitoring.agent.contracts import ExposureFindings, RegistryFinding
from credit_risk_monitoring.agent.orchestrator import EXPOSURE_TOOLS, Orchestrator
from credit_risk_monitoring.agent.tools import Clients, ToolRouter
from credit_risk_monitoring.evals.checks import build_suite
from credit_risk_monitoring.sources import EdgarClient, RateLimiter, SourceType
from credit_risk_monitoring.trace import TraceWriter
from tests.conftest import (
    FakeProvider,
    edgar_mock_transport,
    final_turn,
    html_response,
    json_response,
    large_submissions_payload,
    make_fixture,
    tool_turn,
)

_CH_BASE = "https://api.company-information.service.gov.uk"

_INDEX_JSON = {
    "directory": {
        "item": [
            {"name": "tm-8k.htm", "type": "8-K", "description": "8-K body"},
            {"name": "ex10-1.htm", "type": "EX-10.1", "description": "RSA"},
        ]
    }
}


def _edgar() -> EdgarClient:
    transport = edgar_mock_transport(
        handlers={
            "/index.json": json_response(_INDEX_JSON),
            "tm-8k.htm": html_response("<p>Entry into a Restructuring Support Agreement.</p>"),
            "ex10-1.htm": html_response("<p>UK debtor: Ensco Global Resources Ltd</p>"),
        },
        default_json={"name": "Acme", "filings": {"recent": {"form": ["8-K"], "filingDate": ["2020-08-19"], "accessionNumber": ["0001104659-20-096796"], "primaryDocument": ["tm-8k.htm"], "primaryDocDescription": [""]}, "files": []}},
    )
    return EdgarClient(client=httpx.Client(transport=transport, follow_redirects=True), limiter=RateLimiter(0.0))


def _companies_house() -> CompaniesHouseClient:
    profile = {
        "company_number": "07098531",
        "company_name": "Ensco Global Resources Limited",
        "company_status": "dissolved",
        "type": "ltd",
        "date_of_creation": "2009-12-01",
    }
    charges = {
        "items": [
            {
                "charge_code": "070985310001",
                "status": "outstanding",
                "created_on": "2020-09-25",
                "classification": {"description": "A registered charge"},
                "persons_entitled": [{"name": "Credit Agricole CIB"}],
            }
        ],
        "total_results": 1,
    }
    transport = edgar_mock_transport(
        handlers={"/charges": json_response(charges), "/company/": json_response(profile)}
    )
    inner = httpx.Client(transport=transport, base_url=_CH_BASE, auth=httpx.BasicAuth("k", ""))
    return CompaniesHouseClient(client=inner, limiter=RateLimiter(0.0))


def _exposure_script(lead_kind: str = "uk_company") -> list:
    findings = ExposureFindings(
        issuer_name="Valaris plc",
        distress_summary="Chapter 11; RSA exhibit names UK debtor.",
        leads=[
            {
                "name": "Ensco Global Resources Ltd",
                "kind": lead_kind,
                "jurisdiction": "UK",
                "rationale": "named in Exhibit 10.1 RSA",
                "source_locator": "ex10-1.htm",
            }
        ],
        recommend_stop=False,
        stop_rationale="",
    )
    return [
        tool_turn("edgar_filing", {"cik": "0000314808", "accession": "0001104659-20-096796"}),
        tool_turn("edgar_exhibit", {"url": "https://www.sec.gov/Archives/x/ex10-1.htm"}),
        final_turn(findings.model_dump_json()),
    ]


def _resolution_script() -> list:
    finding = RegistryFinding(
        entity_name="Ensco Global Resources Limited",
        authority="companies_house",
        identifier="07098531",
        status="dissolved",
        charges=["070985310001 (outstanding, 2020-09-25)"],
        secured_parties=["Credit Agricole CIB"],
        rating="",
        notes="MR01 charge created mid-Chapter 11",
        source_locator="CH 07098531 /charges",
    )
    return [
        tool_turn("companies_house", {"operation": "profile", "company_number": "07098531"}),
        tool_turn("companies_house", {"operation": "charges", "company_number": "07098531"}),
        final_turn(finding.model_dump_json()),
    ]


def _orchestrator(provider: FakeProvider, debug: Path) -> Orchestrator:
    return Orchestrator(
        provider=provider,
        clients=Clients(edgar=_edgar(), companies_house=_companies_house(), rating=None),
        broker=AuthorityBroker(audit=InMemoryAuditSink()),
        model="claude-opus-4-8",
        debug_trace_dir=debug,
    )


# --- the headline test: multi-hop run passes the existing CheckSuite ---------
async def test_multi_hop_run_passes_branch_correctness(tmp_path: Path) -> None:
    fixture = make_fixture(
        id="fx-multi",
        classification="multi_hop",
        chain=[
            SourceType.EDGAR_FILING,
            SourceType.EDGAR_EXHIBIT,
            SourceType.COMPANIES_HOUSE,
            SourceType.COMPANIES_HOUSE,
        ],
    )
    provider = FakeProvider(_exposure_script() + _resolution_script(), stream_text="An MR01 charge.")
    orch = _orchestrator(provider, tmp_path / "debug")
    trace_dir = tmp_path / "traces"
    result = await orch.investigate(fixture, trace_dir=trace_dir)

    assert result.error is None
    assert result.depth_reached == 4
    assert result.resolution_count == 1

    # Score the emitted trace with the UNCHANGED C2 suite.
    records = load_directory(trace_dir)
    scored = score_all(records, build_suite([fixture]))
    checks = {c.name: c.outcome for c in scored[0].checks}
    for name in (
        "chain_correctness",
        "depth_correctness",
        "tool_selection",
        "tool_count",
        "stopped_cleanly",
        "no_tool_errors",
    ):
        assert checks[name] == Outcome.PASS, f"{name} -> {checks[name]}"


async def test_control_stops_at_one_hop(tmp_path: Path) -> None:
    fixture = make_fixture(
        id="control-x",
        classification="single_hop_control",
        chain=[SourceType.EDGAR_SUBMISSIONS_INDEX],
        expected_stop_depth=1,
    )
    stop_findings = ExposureFindings(
        issuer_name="Healthy Co",
        distress_summary="Routine filings only.",
        leads=[],
        recommend_stop=True,
        stop_rationale="no distress signal",
    )
    provider = FakeProvider(
        [
            tool_turn("edgar_submissions_index", {"cik": "0000789019"}),
            final_turn(stop_findings.model_dump_json()),
        ],
        stream_text="No credit deterioration; no further investigation warranted.",
    )
    orch = _orchestrator(provider, tmp_path / "debug")
    trace_dir = tmp_path / "traces"
    result = await orch.investigate(fixture, trace_dir=trace_dir)

    assert result.depth_reached == 1
    assert result.stop_recommended is True
    assert result.resolution_count == 0  # never delegated to entity_resolution

    records = load_directory(trace_dir)
    scored = score_all(records, build_suite([fixture]))
    checks = {c.name: c.outcome for c in scored[0].checks}
    assert checks["chain_correctness"] == Outcome.PASS
    assert checks["depth_correctness"] == Outcome.PASS


async def test_contract_violation_is_handled(tmp_path: Path) -> None:
    fixture = make_fixture(id="fx-bad", chain=[SourceType.EDGAR_FILING])
    provider = FakeProvider(
        [
            tool_turn("edgar_filing", {"cik": "1", "accession": "0001104659-20-096796"}),
            final_turn("this is not JSON at all"),
        ]
    )
    orch = _orchestrator(provider, tmp_path / "debug")
    result = await orch.investigate(fixture, trace_dir=tmp_path / "traces")
    assert result.error is not None
    assert "contract violation" in result.error
    # Trace is still written and well-formed.
    assert result.trace_path.exists()


# --- router-level auth boundary tests ---------------------------------------
def _router(broker: AuthorityBroker, tw: TraceWriter, tools: tuple[str, ...]) -> ToolRouter:
    token = broker.mint("exposure", set(tools))
    ctx = AuthContext("exposure", token, broker, frozenset(tools))
    return ToolRouter(auth=ctx, allowed_tools=tools, clients=Clients(edgar=_edgar()), trace=tw, turn=1)


def _started_writer() -> TraceWriter:
    tw = TraceWriter(
        run_id="r", query_id="q", category="multi_hop", question="?", model="m",
        expected_chain=(SourceType.EDGAR_FILING.value,),
    )
    tw.start()
    return tw


async def test_router_records_hop_and_audits() -> None:
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    router = _router(broker, tw, EXPOSURE_TOOLS)
    out = await router.call_tool("edgar_filing", {"cik": "1", "accession": "0001104659-20-096796"})
    assert "Restructuring Support Agreement" in out
    assert tw.depth_reached == 1  # one hop recorded
    assert any(e.action == "execute" and e.tool == "edgar_filing" for e in sink.events)


async def test_router_denies_out_of_scope_without_recording_hop() -> None:
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    router = _router(broker, tw, EXPOSURE_TOOLS)  # edgar only
    out = await router.call_tool("companies_house", {"operation": "profile", "company_number": "1"})
    assert out.startswith("ERROR: authorization denied")
    assert tw.depth_reached == 0  # a policy refusal is NOT a retrieval hop
    assert any(e.action == "authorize" and not e.allowed for e in sink.events)


async def test_router_kill_switch_refuses_mid_run() -> None:
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    router = _router(broker, tw, EXPOSURE_TOOLS)

    first = await router.call_tool("edgar_filing", {"cik": "1", "accession": "0001104659-20-096796"})
    assert not first.startswith("ERROR")
    assert tw.depth_reached == 1

    broker.deny_tool("edgar_filing")  # admin kills the tool mid-workflow

    second = await router.call_tool("edgar_filing", {"cik": "1", "accession": "0001104659-20-096796"})
    assert second.startswith("ERROR: authorization denied")
    assert "deny-list" in second
    assert tw.depth_reached == 1  # no new hop after the kill
    revoked = [e for e in sink.events if e.action == "authorize" and e.revoked]
    assert revoked  # the refusal was audited as a revoke


async def test_router_records_retrieval_failure_as_hop_error() -> None:
    # A genuine retrieval failure (404) is recorded honestly as a tool_error.
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    edgar = EdgarClient(
        client=httpx.Client(transport=edgar_mock_transport()),  # everything 404s
        limiter=RateLimiter(0.0),
    )
    token = broker.mint("exposure", set(EXPOSURE_TOOLS))
    ctx = AuthContext("exposure", token, broker, frozenset(EXPOSURE_TOOLS))
    router = ToolRouter(
        auth=ctx, allowed_tools=EXPOSURE_TOOLS, clients=Clients(edgar=edgar), trace=tw, turn=1
    )
    out = await router.call_tool("edgar_filing", {"cik": "1", "accession": "0001104659-20-096796"})
    assert out.startswith("ERROR")
    assert tw.depth_reached == 1  # the failed hop is visible in the trace
    tw.record_turn(stop_reason="end_turn")
    tw.finish(final_text="x")
    events = json.loads("[" + ",".join(tw.to_jsonl().splitlines()) + "]")
    assert any(e["kind"] == "tool_error" for e in events)


def _edgar_large_index(*, with_filing_body: bool = False) -> EdgarClient:
    """EDGAR client whose recent index buries the target 8-K ~400 rows deep.

    With ``with_filing_body`` it also serves the resolved filing's directory and
    body so a form+date open can be exercised end-to-end."""
    handlers: dict = {}
    if with_filing_body:
        handlers = {
            "/index.json": json_response(
                {
                    "directory": {
                        "item": [
                            {"name": "tm2027797d1_8k.htm", "type": "8-K", "description": "8-K body"},
                            {"name": "tm2027797d1_ex10-1.htm", "type": "EX-10.1", "description": "RSA"},
                        ]
                    }
                }
            ),
            "tm2027797d1_8k.htm": html_response(
                "<p>Entry into a Restructuring Support Agreement; Chapter 11.</p>"
            ),
        }
    transport = edgar_mock_transport(
        handlers=handlers,
        default_json=large_submissions_payload(target_index=432, target_date="2020-08-19"),
    )
    return EdgarClient(
        client=httpx.Client(transport=transport, follow_redirects=True), limiter=RateLimiter(0.0)
    )


def _exposure_router(broker: AuthorityBroker, tw: TraceWriter, edgar: EdgarClient) -> ToolRouter:
    token = broker.mint("exposure", set(EXPOSURE_TOOLS))
    ctx = AuthContext("exposure", token, broker, frozenset(EXPOSURE_TOOLS))
    return ToolRouter(
        auth=ctx, allowed_tools=EXPOSURE_TOOLS, clients=Clients(edgar=edgar), trace=tw, turn=1
    )


async def test_edgar_filing_opens_buried_historical_filing_as_single_filing_hop() -> None:
    """Regression for the fix: edgar_filing resolves a buried historical accession
    from form+date and opens it in ONE EDGAR_FILING hop (the distress chain starts
    at the FILING, not at a separate submissions-index hop). This is the exact path
    that was impossible before the fix (accession invisible -> hallucinated -> 404)."""
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    router = _exposure_router(broker, tw, _edgar_large_index(with_filing_body=True))

    out = await router.call_tool(
        "edgar_filing",
        {"cik": "0000314808", "form_type": "8-K", "date_from": "2020-08-19",
         "date_to": "2020-08-19"},
    )

    assert not out.startswith("ERROR")
    assert "Restructuring Support Agreement" in out  # the real filing body was opened
    assert "tm2027797d1_ex10-1.htm" in out  # exhibit listed -> agent can open it next
    assert tw.depth_reached == 1  # exactly one EDGAR_FILING hop, no separate index hop
    # The recorded hop is the FILING source type (chain starts at the filing).
    assert any(e.tool == "edgar_filing" for e in sink.events if e.action == "execute")


def _companies_house_fuzzy() -> CompaniesHouseClient:
    """CH client whose name search returns many fuzzy '* GLOBAL RESOURCES *'
    matches; only one is the real Ensco subsidiary."""
    search_items = {
        "items": [
            {"company_name": "AKIDI GLOBAL RESOURCES LIMITED", "company_number": "09479817",
             "company_status": "active"},
            {"company_name": "ABACO GLOBAL RESOURCES LTD", "company_number": "10874367",
             "company_status": "active"},
            {"company_name": "ENSCO GLOBAL RESOURCES LIMITED", "company_number": "07098531",
             "company_status": "dissolved"},
            {"company_name": "AFFINITY GLOBAL RESOURCES LIMITED", "company_number": "10982943",
             "company_status": "active"},
        ]
    }
    profile = {
        "company_number": "07098531", "company_name": "Ensco Global Resources Limited",
        "company_status": "dissolved", "type": "ltd", "date_of_creation": "2009-12-01",
    }
    charges = {
        "items": [
            {"charge_code": "070985310001", "status": "outstanding", "created_on": "2020-09-25",
             "classification": {"description": "A registered charge"},
             "persons_entitled": [{"name": "Credit Agricole CIB"}]}
        ],
        "total_results": 1,
    }
    transport = edgar_mock_transport(
        handlers={
            "/search/companies": json_response(search_items),
            "/charges": json_response(charges),
            "/company/": json_response(profile),
        }
    )
    inner = httpx.Client(transport=transport, base_url=_CH_BASE, auth=httpx.BasicAuth("k", ""))
    return CompaniesHouseClient(client=inner, limiter=RateLimiter(0.0))


async def test_companies_house_resolves_name_to_exact_match_in_one_hop() -> None:
    """Regression: profile-by-name folds the name->number search into ONE registry
    hop and picks the EXACT subsidiary (Ensco), not a fuzzy '* GLOBAL RESOURCES *'
    sibling — so resolution is profile+charges (2 hops), not search+N profiles."""
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    tools = (SourceType.COMPANIES_HOUSE.value,)
    token = broker.mint("entity_resolution", set(tools))
    ctx = AuthContext("entity_resolution", token, broker, frozenset(tools))
    router = ToolRouter(
        auth=ctx, allowed_tools=tools, clients=Clients(edgar=_edgar(), companies_house=_companies_house_fuzzy()),
        trace=tw, turn=1,
    )

    record = await router.call_tool(
        "companies_house", {"operation": "profile", "query": "Ensco Global Resources Ltd"}
    )
    assert "07098531" in record  # resolved to the EXACT match, not AKIDI/ABACO/AFFINITY
    assert "dissolved" in record

    charges = await router.call_tool(
        "companies_house", {"operation": "charges", "company_number": "07098531"}
    )
    assert "070985310001" in charges
    assert tw.depth_reached == 2  # profile + charges only — no extra candidate profiling


async def test_submissions_browse_filter_surfaces_buried_filing() -> None:
    """The submissions-index BROWSE filter also surfaces the buried 2020 8-K's
    accession (form+date), recorded as an EDGAR_SUBMISSIONS_INDEX hop. Unfiltered,
    it stays hidden (the original bug)."""
    sink = InMemoryAuditSink()
    broker = AuthorityBroker(audit=sink)
    tw = _started_writer()
    router = _exposure_router(broker, tw, _edgar_large_index())

    unfiltered = await router.call_tool("edgar_submissions_index", {"cik": "0000314808"})
    assert "0001104659-20-096796" not in unfiltered

    filtered = await router.call_tool(
        "edgar_submissions_index",
        {"cik": "0000314808", "form_type": "8-K", "date_from": "2020-01-01",
         "date_to": "2020-12-31"},
    )
    assert "0001104659-20-096796" in filtered
    assert "2020-08-19" in filtered
    assert tw.depth_reached == 2
