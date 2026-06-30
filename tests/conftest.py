"""Shared test helpers: build fixtures and traces in-memory, plus offline fakes
(a scripted LLM provider and mocked HTTP transports) for the agent tests."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from llm_provider import (
    Completion,
    Message,
    MessageDone,
    ProviderConfig,
    StreamEvent,
    TextDelta,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

from credit_risk_monitoring.fixtures import Fixture, Hop
from credit_risk_monitoring.sources import SourceType
from credit_risk_monitoring.trace import TraceWriter


def make_fixture(
    *,
    id: str = "fx-test",
    classification: str = "multi_hop",
    chain: list[SourceType] | None = None,
    expected_stop_depth: int | None = None,
    expected_answer: str = "An MR01 charge 0123 created 2020-01-01.",
    caveats: str = "",
) -> Fixture:
    chain = chain or [
        SourceType.EDGAR_FILING,
        SourceType.EDGAR_EXHIBIT,
        SourceType.COMPANIES_HOUSE,
    ]
    hops = tuple(
        Hop(
            step=i + 1,
            source=t.value,
            source_type=t,
            locator=f"loc-{i + 1}",
            finding=f"finding-{i + 1}",
            triggers="",
        )
        for i, t in enumerate(chain)
    )
    depth = expected_stop_depth if expected_stop_depth is not None else len(chain)
    return Fixture(
        id=id,
        classification=classification,
        issuer_name="Test Issuer",
        cik="0000000001",
        jurisdiction="US->UK",
        depth=len(chain),
        question="What charge was created and what is the status?",
        ground_truth_path=hops,
        expected_stop_depth=depth,
        expected_answer=expected_answer,
        verification_caveats=caveats,
    )


def build_trace(
    *,
    query_id: str,
    category: str,
    hops: list[SourceType],
    expected_chain: list[SourceType],
    final_text: str = "answer",
    out_dir: Path,
) -> Path:
    tw = TraceWriter(
        run_id=f"run/{query_id}",
        query_id=query_id,
        category=category,
        question="q?",
        model="test-model",
        expected_chain=tuple(t.value for t in expected_chain),
        max_turns=1,
    )
    tw.start()
    tw.record_turn(stop_reason="tool_use")
    for h in hops:
        tw.record_hop(h, locator=f"loc-{h.value}", preview="preview text", finding="f")
    tw.record_turn(stop_reason="end_turn", input_tokens=10, output_tokens=5)
    tw.finish(final_text=final_text, stop_reason="end_turn")
    return tw.write(out_dir)


@pytest.fixture
def tmp_traces(tmp_path: Path) -> Path:
    d = tmp_path / "traces"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Offline LLM provider — scripted, no network. Implements the llm-provider seam
# (complete + stream) the agent-core loop and synthesis call through.
# ---------------------------------------------------------------------------
def tool_turn(name: str, tool_input: dict, *, call_id: str = "tu") -> Completion:
    """A scripted assistant turn that asks to call one tool."""
    return Completion(
        text="",
        tool_calls=[ToolUseBlock(id=call_id, name=name, input=tool_input)],
        stop_reason="tool_use",
        model="fake-model",
        usage=Usage(input_tokens=100, output_tokens=20),
    )


def final_turn(text: str) -> Completion:
    """A scripted assistant turn that ends the loop with final text."""
    return Completion(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        model="fake-model",
        usage=Usage(input_tokens=100, output_tokens=40),
    )


class FakeProvider:
    """A deterministic provider: ``complete`` pops the next scripted Completion;
    ``stream`` yields ``stream_text`` then a MessageDone. The agent loop drives it
    like a real provider, so the tools/trace/branch logic is exercised offline."""

    name = "fake"

    def __init__(
        self, completions: list[Completion] | None = None, *, stream_text: str = "final answer"
    ) -> None:
        self._completions: deque[Completion] = deque(completions or [])
        self._stream_text = stream_text
        self.complete_calls = 0
        self.stream_calls = 0

    def extend(self, completions: list[Completion]) -> None:
        self._completions.extend(completions)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        config: ProviderConfig | None = None,
    ) -> Completion:
        self.complete_calls += 1
        if not self._completions:
            raise AssertionError("FakeProvider ran out of scripted completions")
        c = self._completions.popleft()
        # Give each tool call a unique id so the loop's pairing is unambiguous.
        for i, tc in enumerate(c.tool_calls):
            tc.id = f"{tc.name}-{self.complete_calls}-{i}"
        return c

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        config: ProviderConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_calls += 1
        yield TextDelta(text=self._stream_text)
        yield MessageDone(
            stop_reason="end_turn", usage=Usage(input_tokens=80, output_tokens=30)
        )


# ---------------------------------------------------------------------------
# Mocked HTTP transports for the EDGAR + Companies House clients.
# ---------------------------------------------------------------------------
def edgar_mock_transport(
    *,
    handlers: dict[str, httpx.Response] | None = None,
    default_json: dict | None = None,
) -> httpx.MockTransport:
    """Route EDGAR requests by URL substring to canned responses."""
    handlers = handlers or {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, resp in handlers.items():
            if needle in url:
                return resp
        if default_json is not None:
            return httpx.Response(200, json=default_json)
        return httpx.Response(404, text=f"no mock for {url}")

    return httpx.MockTransport(handle)


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def html_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"Content-Type": "text/html"})


def submissions_payload(
    name: str = "Test Issuer", forms: list[str] | None = None
) -> dict:
    forms = forms or ["8-K", "10-K", "8-K"]
    n = len(forms)
    return {
        "name": name,
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": ["2020-08-19"] * n,
                "accessionNumber": ["0001104659-20-096796"] * n,
                "primaryDocument": ["doc.htm"] * n,
                "primaryDocDescription": ["8-K"] * n,
            },
            "files": [],
        },
    }


def large_submissions_payload(
    *,
    name: str = "Valaris plc",
    count: int = 500,
    target_index: int = 432,
    target_form: str = "8-K",
    target_date: str = "2020-08-19",
    target_accession: str = "0001104659-20-096796",
    target_doc: str = "tm2027797d1_8k.htm",
    target_desc: str = "Chapter 11 / Restructuring Support Agreement",
) -> dict:
    """A realistic large recent-filings index (the shape the unit tests missed).

    ``count`` rows newest-first (routine filings), with one distinctive target
    filing buried at ``target_index`` — well beyond any sane newest-N render cap.
    This reproduces the production bug: the target row is in the data structure
    but never rendered to the model unless the index is filtered by form+date.
    """
    from datetime import date, timedelta

    base = date(2024, 12, 31)
    cycle = ["10-Q", "8-K", "10-K", "4", "8-K"]
    forms: list[str] = []
    dates: list[str] = []
    accessions: list[str] = []
    docs: list[str] = []
    descs: list[str] = []
    for i in range(count):
        d = base - timedelta(days=i * 5)
        # Keep routine rows off the exact target date so the target is uniquely
        # resolvable by an exact-date window (the cadence can otherwise collide).
        if d.isoformat() == target_date:
            d = d - timedelta(days=1)
        forms.append(cycle[i % len(cycle)])
        dates.append(d.isoformat())
        accessions.append(f"9999999999-99-{i:06d}")
        docs.append(f"routine{i}.htm")
        descs.append("routine filing")
    forms[target_index] = target_form
    dates[target_index] = target_date
    accessions[target_index] = target_accession
    docs[target_index] = target_doc
    descs[target_index] = target_desc
    return {
        "name": name,
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": dates,
                "accessionNumber": accessions,
                "primaryDocument": docs,
                "primaryDocDescription": descs,
            },
            "files": [],
        },
    }
