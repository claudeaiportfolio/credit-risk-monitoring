"""Typed loader for ``fixtures/fixtures.yaml``.

The YAML is the single source of truth (its schema + the downstream-question
design are documented in ``fixtures/README.md``). This module parses it into
typed objects and derives, for each fixture, the canonical *expected chain* —
the ordered :class:`~credit_risk_monitoring.sources.SourceType` sequence the
branch-correctness scorer compares a run against.

Both sides of the eval consume this: the run harness (to stamp ``expected_tools``
into a trace) and the scorer (to build the per-fixture checks/criteria). Keeping
the derivation here means the fixtures file is the only place the ground truth
lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from credit_risk_monitoring.sources import SourceType, classify_source

# The three top-level groupings in fixtures.yaml and the classification each
# carries. The scorer treats every classification uniformly (chain + depth +
# groundedness); the grouping only affects how the report buckets results.
_GROUPS = ("multi_hop", "single_hop_controls", "trap_controls")


@dataclass(frozen=True)
class Hop:
    """One verified step in a fixture's ground-truth investigation path."""

    step: int
    source: str
    source_type: SourceType
    locator: str
    finding: str
    triggers: str


@dataclass(frozen=True)
class Fixture:
    """One eval case parsed from fixtures.yaml.

    ``expected_chain`` is the ordered canonical source-type sequence derived
    from ``ground_truth_path`` — the dependency chain a correct run walks.
    """

    id: str
    classification: str
    issuer_name: str
    cik: str
    jurisdiction: str
    depth: int
    question: str
    ground_truth_path: tuple[Hop, ...]
    expected_stop_depth: int
    expected_answer: str
    verification_caveats: str

    @property
    def expected_chain(self) -> tuple[SourceType, ...]:
        return tuple(h.source_type for h in self.ground_truth_path)

    @property
    def expected_chain_values(self) -> tuple[str, ...]:
        """The chain as plain strings — the form recorded in a trace."""
        return tuple(t.value for t in self.expected_chain)


def _parse_hop(raw: dict) -> Hop:
    source = str(raw["source"])
    return Hop(
        step=int(raw["step"]),
        source=source,
        source_type=classify_source(source),
        locator=str(raw.get("locator", "")),
        finding=str(raw.get("finding", "")),
        triggers=str(raw.get("triggers", "")),
    )


def _parse_fixture(raw: dict) -> Fixture:
    issuer = raw.get("issuer", {}) or {}
    hops = tuple(_parse_hop(h) for h in raw.get("ground_truth_path", []))
    if not hops:
        raise ValueError(f"fixture {raw.get('id')!r} has no ground_truth_path")

    fixture = Fixture(
        id=str(raw["id"]),
        classification=str(raw["classification"]),
        issuer_name=str(issuer.get("name", "")),
        cik=str(issuer.get("cik", "")),
        jurisdiction=str(raw.get("jurisdiction", "")),
        depth=int(raw.get("depth", len(hops))),
        question=" ".join(str(raw["question"]).split()),
        ground_truth_path=hops,
        expected_stop_depth=int(raw["expected_stop_depth"]),
        expected_answer=" ".join(str(raw["expected_answer"]).split()),
        verification_caveats=" ".join(str(raw.get("verification_caveats", "")).split()),
    )
    # Internal consistency: the verified path length should match the declared
    # stop depth. A mismatch is a fixtures bug, surfaced at load time.
    if len(fixture.ground_truth_path) != fixture.expected_stop_depth:
        raise ValueError(
            f"fixture {fixture.id!r}: ground_truth_path has "
            f"{len(fixture.ground_truth_path)} hops but expected_stop_depth="
            f"{fixture.expected_stop_depth}"
        )
    return fixture


def load_fixtures(path: str | Path) -> list[Fixture]:
    """Parse every fixture from a fixtures.yaml file, preserving file order."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    fixtures: list[Fixture] = []
    for group in _GROUPS:
        for raw in data.get(group, []) or []:
            fixtures.append(_parse_fixture(raw))
    if not fixtures:
        raise ValueError(f"{path}: no fixtures found under {_GROUPS}")

    ids = [f.id for f in fixtures]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"{path}: duplicate fixture ids: {sorted(dupes)}")
    return fixtures


def fixtures_by_id(fixtures: list[Fixture]) -> dict[str, Fixture]:
    return {f.id: f for f in fixtures}


def default_fixtures_path() -> Path:
    """Locate ``fixtures/fixtures.yaml`` relative to the repo root."""
    return Path(__file__).resolve().parents[2] / "fixtures" / "fixtures.yaml"
