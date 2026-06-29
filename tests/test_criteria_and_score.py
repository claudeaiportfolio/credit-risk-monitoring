"""Layer-2 criteria builder + the end-to-end scoring harness.

The judge is exercised through ``agent_evals.judge.run_judge`` with a stub
caller (no API key, no network) — enough to prove the per-fixture criterion
scoping and that groundedness scores aggregate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_evals.judge import run_judge

from credit_risk_monitoring.evals.criteria import build_criteria
from credit_risk_monitoring.evals.score import score_traces
from credit_risk_monitoring.sources import SourceType
from tests.conftest import build_trace, make_fixture


def test_criteria_are_per_fixture_and_carry_expected_answer() -> None:
    fx_a = make_fixture(id="a", expected_answer="charge AAA created 2020")
    fx_b = make_fixture(id="b", expected_answer="charge BBB created 2021", caveats="purpose inferred")
    criteria = build_criteria([fx_a, fx_b])
    assert [c.name for c in criteria] == ["groundedness", "groundedness"]
    assert criteria[0].applies_to_query_id == ("a",)
    assert "charge AAA" in criteria[0].prompt
    # Caveats are surfaced so the judge doesn't penalise excluded facts.
    assert "purpose inferred" in criteria[1].prompt
    assert criteria[0].scale == "rubric_0_3"


async def test_judge_scoping_with_stub_caller(tmp_path: Path) -> None:
    """Each fixture's criterion fires only on its own trace."""
    fx_a = make_fixture(id="a", chain=[SourceType.EDGAR_FILING])
    fx_b = make_fixture(id="b", chain=[SourceType.EDGAR_FILING])
    traces = tmp_path / "t"
    traces.mkdir()
    for fx in (fx_a, fx_b):
        build_trace(
            query_id=fx.id,
            category=fx.classification,
            hops=[SourceType.EDGAR_FILING],
            expected_chain=[SourceType.EDGAR_FILING],
            out_dir=traces,
        )

    from agent_evals import load_directory

    records = load_directory(traces)

    async def stub(system: str, user: str, model: str) -> str:
        return "SCORE: 3\nREASONING: grounded."

    judged = await run_judge(
        records=records, criteria=build_criteria([fx_a, fx_b]), caller=stub, label="t"
    )
    # 2 fixtures x 1 scoped criterion each = 2 results (not 4).
    assert len(judged.results) == 2
    assert judged.mean_by_criterion()["groundedness"] == 1.0


def test_score_traces_produces_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    traces = tmp_path / "t"
    traces.mkdir()
    control = make_fixture(
        id="ctl", classification="single_hop_control",
        chain=[SourceType.EDGAR_SUBMISSIONS_INDEX], expected_stop_depth=1,
    )
    multi = make_fixture(
        id="mh", classification="multi_hop",
        chain=[SourceType.EDGAR_FILING, SourceType.COMPANIES_HOUSE],
    )
    # Baseline behaviour: one submissions-index hop for both.
    for fx in (control, multi):
        build_trace(
            query_id=fx.id, category=fx.classification,
            hops=[SourceType.EDGAR_SUBMISSIONS_INDEX],
            expected_chain=list(fx.expected_chain), out_dir=traces,
        )

    outcome = score_traces(
        trace_dir=traces, fixtures=[control, multi], out_dir=tmp_path / "out"
    )
    assert outcome.verdicts == {"ctl": True, "mh": False}
    assert (tmp_path / "out" / "report.md").exists()
    assert (tmp_path / "out" / "results.json").exists()
    assert "Branch-correctness verdict" in outcome.markdown


def test_score_traces_empty_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        score_traces(trace_dir=tmp_path, fixtures=[make_fixture()])
