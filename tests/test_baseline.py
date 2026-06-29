"""Single-shot baseline runner — offline, no network, injected EDGAR stub."""

from __future__ import annotations

from pathlib import Path

import httpx
from agent_evals.trace import TraceRecord

from credit_risk_monitoring.baseline import offline_answerer, run_one
from credit_risk_monitoring.sources import FilingRef, SubmissionsIndex
from tests.conftest import make_fixture


class _StubEdgar:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def fetch_submissions_index(self, cik: str) -> SubmissionsIndex:
        if self.fail:
            raise httpx.HTTPStatusError("404", request=None, response=None)  # type: ignore[arg-type]
        return SubmissionsIndex(
            cik="0000000001",
            issuer_name="Test Issuer",
            filings=(
                FilingRef("8-K", "2023-01-05", "acc-1", "doc.htm", "going concern"),
                FilingRef("10-K", "2022-04-01", "acc-2", "tenk.htm", "annual report"),
            ),
        )


async def test_baseline_does_exactly_one_hop(tmp_path: Path) -> None:
    fx = make_fixture(id="mh", classification="multi_hop")
    result = await run_one(
        fx,
        edgar=_StubEdgar(),  # type: ignore[arg-type]
        answerer=offline_answerer,
        offline=True,
        trace_dir=tmp_path,
    )
    assert result.depth_reached == 1
    assert result.retrieval_ok is True

    rec = TraceRecord.load(result.trace_path)
    assert rec.tool_call_count == 1
    assert rec.tools_called == ("edgar_submissions_index",)
    assert rec.category == "multi_hop"
    # The fixture's expected chain is stamped into the trace.
    assert rec.expected_tools == fx.expected_chain_values
    # Offline answer is clearly labelled, never a fake real answer.
    assert "OFFLINE BASELINE" in rec.final_text


async def test_baseline_records_retrieval_failure(tmp_path: Path) -> None:
    fx = make_fixture(id="x")
    result = await run_one(
        fx,
        edgar=_StubEdgar(fail=True),  # type: ignore[arg-type]
        answerer=offline_answerer,
        offline=True,
        trace_dir=tmp_path,
    )
    assert result.retrieval_ok is False
    rec = TraceRecord.load(result.trace_path)
    assert rec.tool_calls[0].error is not None
