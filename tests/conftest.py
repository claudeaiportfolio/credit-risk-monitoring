"""Shared test helpers: build fixtures and traces in-memory."""

from __future__ import annotations

from pathlib import Path

import pytest

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
