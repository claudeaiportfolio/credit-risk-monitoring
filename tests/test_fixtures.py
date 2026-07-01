"""Fixtures loader — parsing, chain derivation, consistency guards."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from credit_risk_monitoring.fixtures import (
    default_fixtures_path,
    fixtures_by_id,
    load_fixtures,
)
from credit_risk_monitoring.sources import SourceType


def test_loads_the_real_fixture_bank() -> None:
    fixtures = load_fixtures(default_fixtures_path())
    assert len(fixtures) == 10
    classifications = {f.classification for f in fixtures}
    assert classifications == {"multi_hop", "single_hop_control", "trap_control"}


def test_derives_expected_chain() -> None:
    by_id = fixtures_by_id(load_fixtures(default_fixtures_path()))
    valaris = by_id["valaris-uk-subsidiary-charge"]
    assert valaris.expected_chain == (
        SourceType.EDGAR_FILING,
        SourceType.EDGAR_EXHIBIT,
        SourceType.COMPANIES_HOUSE,
        SourceType.COMPANIES_HOUSE,
    )
    assert valaris.expected_stop_depth == 4

    control = by_id["control-microsoft"]
    assert control.expected_chain == (SourceType.EDGAR_SUBMISSIONS_INDEX,)
    assert control.expected_stop_depth == 1


def test_hertz_chain_is_the_ch_search_discovery_model() -> None:
    """The hertz question does NOT name the UK entity, and neither does the Chapter 11
    8-K (it carves out the 'International Subsidiaries' as a class). So the specific
    receivables entity must be DISCOVERED via a Companies House company-search hop:
    Chapter 11 8-K -> CH search -> CH profile -> CH charges, depth 4. Locks in the
    genuine-discovery framing (the entity is found by property, never pre-named)."""
    by_id = fixtures_by_id(load_fixtures(default_fixtures_path()))
    hertz = by_id["hertz-uk-receivables-charge-holder"]
    assert hertz.expected_chain == (
        SourceType.EDGAR_FILING,
        SourceType.COMPANIES_HOUSE,
        SourceType.COMPANIES_HOUSE,
        SourceType.COMPANIES_HOUSE,
    )
    assert hertz.expected_stop_depth == 4
    # The question must not pre-name the target entity (genuine discovery).
    assert "Hertz UK Receivables" not in hertz.question
    assert "08789381" not in hertz.question


def test_reworded_questions_do_not_pre_name_their_target() -> None:
    """Integrity guard: the discovery-reworded questions must not contain the exact
    registered name (or CH number) of the entity the analyst is supposed to discover."""
    by_id = fixtures_by_id(load_fixtures(default_fixtures_path()))
    revlon = by_id["revlon-elizabeth-arden-uk-charge"]
    assert "Elizabeth Arden (UK)" not in revlon.question
    assert "04126357" not in revlon.question
    hertz = by_id["hertz-uk-receivables-charge-holder"]
    assert "Hertz UK Receivables" not in hertz.question


def test_question_and_answer_whitespace_normalised() -> None:
    fx = load_fixtures(default_fixtures_path())[0]
    assert "\n" not in fx.question
    assert "  " not in fx.question


def _write_yaml(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "fx.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def test_depth_mismatch_raises(tmp_path: Path) -> None:
    doc = {
        "multi_hop": [
            {
                "id": "bad",
                "classification": "multi_hop",
                "issuer": {"name": "X", "cik": "1"},
                "question": "q",
                "ground_truth_path": [
                    {"step": 1, "source": "8-K", "locator": "l", "finding": "f"}
                ],
                "expected_stop_depth": 3,  # mismatch: only 1 hop
                "expected_answer": "a",
            }
        ]
    }
    with pytest.raises(ValueError, match="expected_stop_depth"):
        load_fixtures(_write_yaml(tmp_path, doc))


def test_duplicate_ids_raise(tmp_path: Path) -> None:
    hop = [{"step": 1, "source": "8-K", "locator": "l", "finding": "f"}]
    entry = {
        "id": "dup",
        "classification": "multi_hop",
        "issuer": {"name": "X", "cik": "1"},
        "question": "q",
        "ground_truth_path": hop,
        "expected_stop_depth": 1,
        "expected_answer": "a",
    }
    doc = {"multi_hop": [entry, dict(entry)]}
    with pytest.raises(ValueError, match="duplicate"):
        load_fixtures(_write_yaml(tmp_path, doc))


def test_empty_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no fixtures"):
        load_fixtures(_write_yaml(tmp_path, {"multi_hop": []}))
