"""Arm B (Managed Agents) — offline tests.

A scripted fake Anthropic client drives the SSE event loop (no network, no MA
service), and the EDGAR / Companies House clients run on mocked HTTP. The
headline test is the same bar Arm A is held to: the emitted trace scores through
the UNCHANGED C2 ``CheckSuite`` (chain + depth + the package universals). Also
covers the tool-exposure boundary (custom-tool dispatch + hop recording +
result round-trip), the roster/control-plane construction, and the cost model.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from agent_evals import Outcome, load_directory, score_all

from credit_risk_monitoring.agent.tools import Clients
from credit_risk_monitoring.agent_managed.orchestrator import ManagedOrchestrator, token_cost
from credit_risk_monitoring.agent_managed.roster import (
    EXPOSURE_SYSTEM,
    Roster,
    custom_tools_for,
    ensure_roster,
)
from credit_risk_monitoring.evals.checks import build_suite
from credit_risk_monitoring.sources import EdgarClient, RateLimiter, SourceType
from tests.conftest import (
    edgar_mock_transport,
    html_response,
    json_response,
    make_fixture,
    submissions_payload,
)

_CH_BASE = "https://api.company-information.service.gov.uk"


# --- mocked retrieval spines (mirrors test_agent_orchestrator) ---------------
_DIR_JSON = {
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
            "/index.json": json_response(_DIR_JSON),
            "tm-8k.htm": html_response("<p>Entry into a Restructuring Support Agreement.</p>"),
            "ex10-1.htm": html_response("<p>UK debtor: Ensco Global Resources Ltd</p>"),
        },
        default_json=submissions_payload(name="Valaris plc"),
    )
    return EdgarClient(
        client=httpx.Client(transport=transport, follow_redirects=True), limiter=RateLimiter(0.0)
    )


def _companies_house() -> Any:
    from credit_risk_monitoring.agent.companies_house import CompaniesHouseClient

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


# --- the scripted fake Managed Agents client --------------------------------
class _FakeStream:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __iter__(self):
        return iter(self._events)


class _FakeEvents:
    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self.sent: list[dict] = []

    def stream(self, *, session_id: str) -> _FakeStream:  # noqa: ARG002
        return _FakeStream(self._script)

    def send(self, *, session_id: str, events: list[dict]) -> None:  # noqa: ARG002
        self.sent.extend(events)


class _FakeSessions:
    def __init__(self, script: list[dict], usage: dict) -> None:
        self.events = _FakeEvents(script)
        self._usage = usage
        self.created: list[dict] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(id="sesn_fake", status="running")

    def retrieve(self, *, session_id: str) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(usage=self._usage, status="idle")


class _FakeManagedClient:
    """Just enough of ``anthropic.Anthropic().beta`` for the Arm B data plane."""

    def __init__(self, script: list[dict], usage: dict) -> None:
        self.beta = SimpleNamespace(sessions=_FakeSessions(script, usage))


def _orchestrator(script: list[dict], usage: dict, *, ch: bool = True) -> ManagedOrchestrator:
    return ManagedOrchestrator(
        client=_FakeManagedClient(script, usage),
        clients=Clients(edgar=_edgar(), companies_house=_companies_house() if ch else None),
        roster=Roster(
            environment_id="env_x",
            coordinator_id="agent_coord",
            exposure_id="agent_exp",
            entity_resolution_id="agent_res",
        ),
    )


def _ctu(name: str, tool_input: dict, sevt: str, thread: str) -> dict:
    return {
        "type": "agent.custom_tool_use",
        "name": name,
        "input": tool_input,
        "id": sevt,
        "session_thread_id": thread,
    }


# --- headline test: a multi-hop run scores through the UNCHANGED suite --------
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
    script = [
        {"type": "session.status_running"},
        _ctu("edgar_filing", {"cik": "0000314808", "accession": "0001104659-20-096796"},
             "sevt1", "t-exp"),
        {"type": "span.model_request_end",
         "model_usage": {"input_tokens": 1000, "output_tokens": 100}},
        _ctu("edgar_exhibit", {"url": "https://www.sec.gov/Archives/x/ex10-1.htm"},
             "sevt2", "t-exp"),
        _ctu("companies_house", {"operation": "profile", "company_number": "07098531"},
             "sevt3", "t-res"),
        _ctu("companies_house", {"operation": "charges", "company_number": "07098531"},
             "sevt4", "t-res"),
        {"type": "agent.message", "content": [{"type": "text", "text":
            "An MR01 charge 070985310001 created 2020-09-25 over Ensco Global Resources "
            "Limited (CH 07098531); the company is Dissolved."}]},
        {"type": "span.model_request_end",
         "model_usage": {"input_tokens": 500, "output_tokens": 200}},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    usage = {"input_tokens": 1500, "output_tokens": 300,
             "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50}
    orch = _orchestrator(script, usage)
    trace_dir = tmp_path / "traces"
    result = orch.investigate(fixture, trace_dir=trace_dir)

    assert result.error is None
    assert result.depth_reached == 4
    assert result.resolution_count == 2  # two companies_house hops
    assert "070985310001" in result.final_answer
    # Authoritative session usage flows into the metrics + cost.
    assert result.input_tokens == 1500 and result.output_tokens == 300
    assert result.cost_usd > 0
    # A custom_tool_result was sent for every custom_tool_use (4 hops).
    sent = orch.client.beta.sessions.events.sent
    results_sent = [e for e in sent if e["type"] == "user.custom_tool_result"]
    assert len(results_sent) == 4
    assert all(e.get("session_thread_id") for e in results_sent)  # thread echoed

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
    script = [
        _ctu("edgar_submissions_index", {"cik": "0000789019"}, "sevt1", "t-exp"),
        {"type": "agent.message", "content": [{"type": "text", "text":
            "No credit deterioration; no further investigation warranted."}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    orch = _orchestrator(script, {"input_tokens": 400, "output_tokens": 60}, ch=False)
    trace_dir = tmp_path / "traces"
    result = orch.investigate(fixture, trace_dir=trace_dir)

    assert result.depth_reached == 1
    assert result.resolution_count == 0  # never resolved an entity

    records = load_directory(trace_dir)
    scored = score_all(records, build_suite([fixture]))
    checks = {c.name: c.outcome for c in scored[0].checks}
    assert checks["chain_correctness"] == Outcome.PASS
    assert checks["depth_correctness"] == Outcome.PASS


async def test_custom_tool_failure_recorded_as_hop_error(tmp_path: Path) -> None:
    """A retrieval failure is an honest tool_error hop + an is_error result event."""
    fixture = make_fixture(id="fx-err", chain=[SourceType.EDGAR_FILING])
    # everything 404s -> the handler raises -> recorded as a hop_error
    edgar = EdgarClient(client=httpx.Client(transport=edgar_mock_transport()),
                        limiter=RateLimiter(0.0))
    script = [
        _ctu("edgar_filing", {"cik": "1", "accession": "0001104659-20-096796"}, "sevt1", "t-exp"),
        {"type": "agent.message", "content": [{"type": "text", "text": "could not open filing"}]},
        {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
    ]
    orch = ManagedOrchestrator(
        client=_FakeManagedClient(script, {"input_tokens": 10, "output_tokens": 5}),
        clients=Clients(edgar=edgar),
        roster=Roster("env", "c", "e", "r"),
    )
    result = orch.investigate(fixture, trace_dir=tmp_path / "traces")
    assert result.depth_reached == 1  # the failed hop is visible in the trace
    sent = orch.client.beta.sessions.events.sent
    err_results = [e for e in sent if e["type"] == "user.custom_tool_result" and e.get("is_error")]
    assert len(err_results) == 1


# --- control plane: roster construction -------------------------------------
class _RecordingRosterClient:
    def __init__(self) -> None:
        self.agents_created: list[dict] = []
        self.envs_created: list[dict] = []
        outer = self

        class _Agents:
            def create(self, **kw: Any) -> SimpleNamespace:
                outer.agents_created.append(kw)
                return SimpleNamespace(id=f"agent_{kw['name']}", version=1)

        class _Envs:
            def create(self, **kw: Any) -> SimpleNamespace:
                outer.envs_created.append(kw)
                return SimpleNamespace(id="env_new")

        self.beta = SimpleNamespace(agents=_Agents(), environments=_Envs())


def test_ensure_roster_creates_two_subagents_and_a_coordinator() -> None:
    client = _RecordingRosterClient()
    roster = ensure_roster(client, model="claude-opus-4-8")

    # Minimum roster: environment + exposure + entity_resolution + coordinator.
    assert roster.environment_id == "env_new"
    assert len(client.agents_created) == 3
    names = [a["name"] for a in client.agents_created]
    assert names == ["credit-risk-exposure", "credit-risk-entity-resolution",
                     "credit-risk-coordinator"]

    coord = client.agents_created[-1]
    # The coordinator is tool-less (delegates + synthesizes) and carries the roster.
    assert coord["tools"] == []
    assert coord["multiagent"]["type"] == "coordinator"
    assert set(coord["multiagent"]["agents"]) == {"agent_credit-risk-exposure",
                                                  "agent_credit-risk-entity-resolution"}
    assert set(roster.created) == {"environment", "exposure", "entity_resolution", "coordinator"}


def test_ensure_roster_reuses_supplied_ids() -> None:
    client = _RecordingRosterClient()
    roster = ensure_roster(
        client,
        environment_id="env_pinned",
        coordinator_id="coord_pinned",
    )
    # Nothing created when the coordinator + env are already known.
    assert not client.agents_created
    assert not client.envs_created
    assert roster.coordinator_id == "coord_pinned"
    assert roster.environment_id == "env_pinned"


def test_custom_tools_mirror_arm_a_tool_surface() -> None:
    tools = custom_tools_for((SourceType.EDGAR_FILING.value, SourceType.COMPANIES_HOUSE.value))
    assert [t["name"] for t in tools] == ["edgar_filing", "companies_house"]
    assert all(t["type"] == "custom" for t in tools)
    assert all("input_schema" in t and t["description"] for t in tools)
    # The exposure system prompt is Arm A's own (reused, not copied).
    assert "EXPOSURE sub-agent" in EXPOSURE_SYSTEM


def test_token_cost_opus_pricing() -> None:
    # 1M input @ $5, 1M output @ $25.
    assert token_cost(1_000_000, 0) == 5.0
    assert token_cost(0, 1_000_000) == 25.0
    assert token_cost(1_000_000, 1_000_000, 1_000_000, 1_000_000) == 5.0 + 25.0 + 0.5 + 6.25


def test_usage_tokens_handles_nested_session_cache_creation() -> None:
    """session.usage reports cache writes under a nested ``cache_creation`` dict
    (prompt caching is auto-enabled by Managed Agents) — sum it, don't drop it."""
    from credit_risk_monitoring.agent_managed.orchestrator import _usage_tokens

    session_usage = {
        "input_tokens": 12,
        "output_tokens": 2069,
        "cache_read_input_tokens": 30200,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 3666},
    }
    assert _usage_tokens(session_usage) == (12, 2069, 30200, 3666)
    # Flat span shape still works.
    span_usage = {
        "input_tokens": 2,
        "output_tokens": 483,
        "cache_read_input_tokens": 4241,
        "cache_creation_input_tokens": 218,
    }
    assert _usage_tokens(span_usage) == (2, 483, 4241, 218)
