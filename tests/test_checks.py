"""Layer-1 branch-correctness checks: chain + depth, incl. over-investigation."""

from __future__ import annotations

from pathlib import Path

from agent_evals import Outcome, load_directory, score_all

from credit_risk_monitoring.evals.checks import build_suite
from credit_risk_monitoring.sources import SourceType
from tests.conftest import build_trace, make_fixture

CHAIN = [SourceType.EDGAR_FILING, SourceType.EDGAR_EXHIBIT, SourceType.COMPANIES_HOUSE]


def _score_one(fixture, hops, tmp_traces: Path):
    build_trace(
        query_id=fixture.id,
        category=fixture.classification,
        hops=hops,
        expected_chain=list(fixture.expected_chain),
        out_dir=tmp_traces,
    )
    records = load_directory(tmp_traces)
    scored = score_all(records, build_suite([fixture]))
    match = next(s for s in scored if s.record.query_id == fixture.id)
    return {c.name: c for c in match.checks}


def test_full_correct_chain_passes(tmp_traces: Path) -> None:
    fx = make_fixture(chain=CHAIN)
    checks = _score_one(fx, CHAIN, tmp_traces)
    assert checks["chain_correctness"].outcome == Outcome.PASS
    assert checks["depth_correctness"].outcome == Outcome.PASS


def test_under_investigation_chain_prefix_passes_but_depth_fails(tmp_traces: Path) -> None:
    """Walked the right path but stopped short: chain PASS (prefix), depth FAIL."""
    fx = make_fixture(chain=CHAIN)  # expected depth 3
    checks = _score_one(fx, CHAIN[:1], tmp_traces)  # ran only hop 1
    assert checks["chain_correctness"].outcome == Outcome.PASS
    assert checks["depth_correctness"].outcome == Outcome.FAIL
    assert "under-investigation" in checks["depth_correctness"].detail


def test_wrong_source_type_fails_chain(tmp_traces: Path) -> None:
    fx = make_fixture(chain=CHAIN)
    wrong = [SourceType.COMPANIES_HOUSE, SourceType.EDGAR_EXHIBIT, SourceType.EDGAR_FILING]
    checks = _score_one(fx, wrong, tmp_traces)
    assert checks["chain_correctness"].outcome == Outcome.FAIL
    assert "hop 1" in checks["chain_correctness"].detail


def test_over_investigation_fails_depth_and_chain(tmp_traces: Path) -> None:
    """A control expects depth 1; ANY deeper traversal is over-investigation.

    This is the negative case proving the depth check fires on over-reach.
    """
    fx = make_fixture(
        id="control-x",
        classification="single_hop_control",
        chain=[SourceType.EDGAR_SUBMISSIONS_INDEX],
        expected_stop_depth=1,
    )
    # Agent over-investigates: index glance + two unwarranted deeper hops.
    over = [
        SourceType.EDGAR_SUBMISSIONS_INDEX,
        SourceType.EDGAR_FILING,
        SourceType.COMPANIES_HOUSE,
    ]
    checks = _score_one(fx, over, tmp_traces)
    assert checks["depth_correctness"].outcome == Outcome.FAIL
    assert "over-investigation" in checks["depth_correctness"].detail
    # Over-traversal is also not a prefix of the 1-hop chain -> chain FAIL.
    assert checks["chain_correctness"].outcome == Outcome.FAIL
    assert "over-traversed" in checks["chain_correctness"].detail


def test_no_hops_fails_chain(tmp_traces: Path) -> None:
    fx = make_fixture(chain=CHAIN)
    checks = _score_one(fx, [], tmp_traces)
    assert checks["chain_correctness"].outcome == Outcome.FAIL


def test_single_shot_baseline_pattern(tmp_traces: Path) -> None:
    """End-to-end: the single-shot baseline (one submissions-index hop) passes a
    control and fails a multi-hop chain — the headline thesis."""
    control = make_fixture(
        id="ctl",
        classification="single_hop_control",
        chain=[SourceType.EDGAR_SUBMISSIONS_INDEX],
        expected_stop_depth=1,
    )
    multi = make_fixture(id="mh", classification="multi_hop", chain=CHAIN)

    baseline_hop = [SourceType.EDGAR_SUBMISSIONS_INDEX]
    ctl_checks = _score_one(control, baseline_hop, tmp_traces)
    assert ctl_checks["chain_correctness"].outcome == Outcome.PASS
    assert ctl_checks["depth_correctness"].outcome == Outcome.PASS

    mh_checks = _score_one(multi, baseline_hop, tmp_traces)
    assert mh_checks["chain_correctness"].outcome == Outcome.FAIL
    assert mh_checks["depth_correctness"].outcome == Outcome.FAIL
