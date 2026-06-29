"""Trace format — round-trips through agent-evals' TraceRecord loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_evals.trace import TraceRecord

from credit_risk_monitoring.sources import SourceType
from credit_risk_monitoring.trace import TraceWriter


def test_trace_round_trips_into_agent_evals(tmp_path: Path) -> None:
    tw = TraceWriter(
        run_id="run/x",
        query_id="fx-1",
        category="multi_hop",
        question="What charge?",
        model="test-model",
        expected_chain=("edgar_filing", "companies_house"),
        max_turns=4,
    )
    tw.start()
    tw.record_turn(stop_reason="tool_use")
    tw.record_hop(SourceType.EDGAR_FILING, locator="acc-123", preview="x" * 500, finding="f1")
    tw.record_hop(SourceType.COMPANIES_HOUSE, locator="CH 123", preview="y", finding="f2")
    tw.record_turn(stop_reason="end_turn", input_tokens=100, output_tokens=20)
    tw.finish(final_text="the full answer", stop_reason="end_turn")
    path = tw.write(tmp_path)

    rec = TraceRecord.load(path)
    assert rec.query_id == "fx-1"
    assert rec.category == "multi_hop"
    assert rec.expected_tools == ("edgar_filing", "companies_house")
    # One hop == one tool call; source chain == tool names in order.
    assert rec.tools_called == ("edgar_filing", "companies_house")
    assert rec.tool_call_count == 2
    assert rec.stop_reason == "end_turn"
    # final_text_full round-trips untruncated for the judge.
    assert rec.final_text == "the full answer"
    assert rec.final_text_truncated is False
    # preview is capped at 300 chars by the writer.
    assert rec.tool_calls[0].result_preview_len == 500
    assert len(rec.tool_calls[0].result_preview or "") == 300
    assert rec.total_input_tokens == 100
    assert rec.total_output_tokens == 20


def test_locator_recorded_in_tool_input(tmp_path: Path) -> None:
    tw = TraceWriter(run_id="r", query_id="q", category="c", question="?", model="m")
    tw.start()
    tw.record_turn(stop_reason="tool_use")
    tw.record_hop(SourceType.EDGAR_FILING, locator="EDGAR acc 0001-20", preview="p")
    tw.finish(final_text="a")
    rec = TraceRecord.load(tw.write(tmp_path))
    assert rec.tool_calls[0].input["locator"] == "EDGAR acc 0001-20"


def test_error_hop_recorded(tmp_path: Path) -> None:
    tw = TraceWriter(run_id="r", query_id="q", category="c", question="?", model="m")
    tw.start()
    tw.record_turn(stop_reason="tool_use")
    tw.record_hop_error(SourceType.EDGAR_SUBMISSIONS_INDEX, locator="CIK 9", error="HTTP 404")
    tw.finish(final_text="")
    rec = TraceRecord.load(tw.write(tmp_path))
    assert rec.tool_call_count == 1
    assert rec.tool_calls[0].error == "HTTP 404"


def test_output_before_finish_raises() -> None:
    tw = TraceWriter(run_id="r", query_id="q", category="c", question="?", model="m")
    tw.start()
    with pytest.raises(RuntimeError, match="finish"):
        tw.to_jsonl()
