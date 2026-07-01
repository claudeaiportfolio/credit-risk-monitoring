"""Per-arm run metrics — tokens, $ cost, wall-clock latency — for the 3-way table.

The build-vs-buy comparison needs the SAME metrics captured the SAME way across
all three arms (single-shot baseline, Arm A self-hosted loop, Arm B Managed
Agents). This module provides one timed runner per arm that writes traces AND a
per-fixture metrics sidecar (``out/<label>/metrics.json``), plus the shared
cost model. It reuses each arm's existing public entry point unchanged — it does
not modify the baseline, Arm A, the scorer, or the fixtures.

Tokens: extracted uniformly from each arm's C2 trace ``claude_response`` events
(Arm B records its authoritative ``sessions.retrieve().usage`` there too), so a
single extractor works for all three. $ cost uses the shared ``opus-4-8`` model
(``token_cost``); cache tokens are reported separately for Arm B (Managed Agents
reports them via session usage) and folded into its cost.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from credit_risk_monitoring.agent_managed.orchestrator import (
    ManagedOrchestrator,
    ManagedRunResult,
    token_cost,
)
from credit_risk_monitoring.fixtures import Fixture


@dataclass
class ArmMetric:
    """One fixture's cost/latency for one arm."""

    fixture_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_s: float
    error: str | None = None


def tokens_from_trace(path: Path) -> tuple[int, int]:
    """Sum (input, output) tokens from a C2 trace's ``claude_response`` events."""
    in_tok = out_tok = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("kind") == "claude_response":
            in_tok += int(ev.get("input_tokens", 0) or 0)
            out_tok += int(ev.get("output_tokens", 0) or 0)
    return in_tok, out_tok


def _write_metrics(metrics: dict[str, ArmMetric], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "metrics.json"
    path.write_text(
        json.dumps({k: asdict(v) for k, v in metrics.items()}, indent=2), encoding="utf-8"
    )
    return path


def load_metrics(out_dir: Path) -> dict[str, ArmMetric]:
    """Read a previously-written metrics sidecar (for ``compare --skip-run``)."""
    path = out_dir / "metrics.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: ArmMetric(**v) for k, v in raw.items()}


# --- timed per-arm runners --------------------------------------------------
def run_baseline_metrics(
    fixtures: list[Fixture], *, trace_dir: Path, out_dir: Path
) -> dict[str, ArmMetric]:
    """Run the single-shot baseline per fixture (timed) and capture metrics.

    Uses one event loop + one EDGAR client for the whole arm (per-fixture timing
    via ``run_one``) — avoids the noisy async-teardown a per-fixture
    ``asyncio.run`` produces.
    """
    from credit_risk_monitoring.baseline import run_one, select_answerer
    from credit_risk_monitoring.sources import EdgarClient

    async def _run() -> dict[str, ArmMetric]:
        answerer, offline = select_answerer()
        edgar = EdgarClient()
        out: dict[str, ArmMetric] = {}
        try:
            for fx in fixtures:
                started = time.monotonic()
                r = await run_one(
                    fx, edgar=edgar, answerer=answerer, offline=offline, trace_dir=trace_dir
                )
                latency = time.monotonic() - started
                in_tok, out_tok = tokens_from_trace(r.trace_path)
                out[fx.id] = ArmMetric(
                    fixture_id=fx.id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=token_cost(in_tok, out_tok),
                    latency_s=latency,
                )
        finally:
            edgar.close()
        return out

    metrics = asyncio.run(_run())
    _write_metrics(metrics, out_dir)
    return metrics


def run_arm_a_metrics(
    fixtures: list[Fixture], *, trace_dir: Path, out_dir: Path, debug_trace_dir: Path
) -> dict[str, ArmMetric]:
    """Run Arm A (self-hosted loop) per fixture (timed) and capture metrics."""
    from credit_risk_monitoring.agent.orchestrator import Orchestrator

    async def _run() -> dict[str, ArmMetric]:
        orch = Orchestrator(debug_trace_dir=debug_trace_dir)
        out: dict[str, ArmMetric] = {}
        try:
            for fx in fixtures:
                started = time.monotonic()
                res = await orch.investigate(fx, trace_dir=trace_dir)
                latency = time.monotonic() - started
                in_tok, out_tok = tokens_from_trace(res.trace_path)
                out[fx.id] = ArmMetric(
                    fixture_id=fx.id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=token_cost(in_tok, out_tok),
                    latency_s=latency,
                    error=res.error,
                )
        finally:
            orch.close()
        return out

    metrics = asyncio.run(_run())
    _write_metrics(metrics, out_dir)
    return metrics


def run_arm_b_metrics(
    fixtures: list[Fixture],
    *,
    trace_dir: Path,
    out_dir: Path,
    orchestrator: ManagedOrchestrator | None = None,
) -> dict[str, ArmMetric]:
    """Run Arm B (Managed Agents) per fixture and capture metrics.

    ``ManagedRunResult`` already carries authoritative tokens/cost/latency
    (from the session usage + wall-clock), so no trace re-parse is needed.
    """
    orch = orchestrator or ManagedOrchestrator()
    owns = orchestrator is None
    metrics: dict[str, ArmMetric] = {}
    try:
        for fx in fixtures:
            res: ManagedRunResult = orch.investigate(fx, trace_dir=trace_dir)
            metrics[fx.id] = ArmMetric(
                fixture_id=fx.id,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                cache_read_tokens=res.cache_read_tokens,
                cache_write_tokens=res.cache_write_tokens,
                cost_usd=res.cost_usd,
                latency_s=res.latency_s,
                error=res.error,
            )
    finally:
        if owns:
            orch.close()
    _write_metrics(metrics, out_dir)
    return metrics


__all__ = [
    "ArmMetric",
    "load_metrics",
    "run_arm_a_metrics",
    "run_arm_b_metrics",
    "run_baseline_metrics",
    "tokens_from_trace",
]
