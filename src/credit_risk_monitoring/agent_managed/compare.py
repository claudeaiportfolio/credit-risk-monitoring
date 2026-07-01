"""The live 3-way build-vs-buy comparison: baseline vs Arm A vs Arm B.

Runs (or re-scores) the three arms over the fixtures and renders one table with,
per fixture, each arm's chain / depth / groundedness, plus per-arm totals for
tokens, $ cost, and wall-clock latency. Scoring uses the UNCHANGED shared
``agent-evals`` surface (``build_suite`` Layer 1 + the groundedness judge Layer 2)
— identical to how the baseline and Arm A are already scored — and fans every
arm out to Braintrust (project ``agent-evals``) via the shared adapter.

This module orchestrates existing pieces; it does not modify the scorer, the
fixtures, or the shared packages.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from agent_evals import Outcome, ScoredRecord, load_directory, score_all

from credit_risk_monitoring.agent_managed.metrics import (
    ArmMetric,
    load_metrics,
    run_arm_a_metrics,
    run_arm_b_metrics,
    run_baseline_metrics,
)
from credit_risk_monitoring.evals.checks import build_suite
from credit_risk_monitoring.evals.criteria import build_criteria
from credit_risk_monitoring.evals.score import DEFAULT_JUDGE_MODEL
from credit_risk_monitoring.fixtures import Fixture

# The three arms, in report order. label -> trace subdir under the trace root.
ARM_LABELS = ("baseline", "arm-a", "arm-b")
_GROUNDED_PASS = 2  # groundedness rubric is 0-3; >= 2 captures the verified fact


@dataclass
class ArmScore:
    """One arm's scored + judged results over the fixture bank."""

    label: str
    chain_pass: dict[str, bool]
    depth_pass: dict[str, bool]
    depth_reached: dict[str, int]
    chain: dict[str, list[str]]
    groundedness: dict[str, tuple[int, int]]  # qid -> (score, max)
    metrics: dict[str, ArmMetric]


def _check(s: ScoredRecord, name: str) -> bool:
    return any(c.name == name and c.outcome == Outcome.PASS for c in s.checks)


def _judge_scores(
    scored: list[ScoredRecord], fixtures: list[Fixture], label: str
) -> dict[str, tuple[int, int]]:
    """Run the Layer-2 groundedness judge; return qid -> (score, max_score).

    Skipped (empty) without ANTHROPIC_API_KEY, matching the rest of the eval.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    from agent_evals.judge import run_judge

    async def _run() -> dict[str, tuple[int, int]]:
        judged = await run_judge(
            records=scored,
            criteria=build_criteria(fixtures),
            model=DEFAULT_JUDGE_MODEL,
            label=label,
        )
        return {r.record_query_id: (int(r.score), int(r.max_score)) for r in judged.results}

    return asyncio.run(_run())


def _fan_out_braintrust(scored: list[ScoredRecord], label: str) -> None:
    """Surface an arm to Braintrust (project agent-evals) — no-op without the key."""
    try:
        from agent_evals.adapters import braintrust
    except ImportError:
        return
    if not braintrust.is_enabled():
        return
    try:
        braintrust.post_run(scored, run_label=label)
    except Exception as exc:  # noqa: BLE001 — never let surfacing crash the comparison
        print(f"warning: braintrust fan-out for {label!r} failed: {exc}")


def score_arm(
    label: str, *, trace_dir: Path, fixtures: list[Fixture], metrics: dict[str, ArmMetric]
) -> ArmScore:
    """Score one arm's traces (Layer 1 + Layer 2) and fan out to Braintrust."""
    records = load_directory(trace_dir)
    if not records:
        raise FileNotFoundError(f"no traces found under {trace_dir} for arm {label!r}")
    scored = score_all(records, build_suite(fixtures))
    grounded = _judge_scores(scored, fixtures, label)
    _fan_out_braintrust(scored, label)
    return ArmScore(
        label=label,
        chain_pass={s.record.query_id: _check(s, "chain_correctness") for s in scored},
        depth_pass={s.record.query_id: _check(s, "depth_correctness") for s in scored},
        depth_reached={s.record.query_id: s.record.tool_call_count for s in scored},
        chain={s.record.query_id: list(s.record.tools_called) for s in scored},
        groundedness=grounded,
        metrics=metrics,
    )


# --- run phase --------------------------------------------------------------
def run_arms(
    fixtures: list[Fixture],
    *,
    trace_root: Path,
    out_root: Path,
    arms: tuple[str, ...] = ARM_LABELS,
    skip_run: bool = False,
) -> dict[str, dict[str, ArmMetric]]:
    """Run (or, with ``skip_run``, reuse) the selected arms; return per-arm metrics."""
    metrics: dict[str, dict[str, ArmMetric]] = {}
    for label in arms:
        trace_dir = trace_root / label
        out_dir = out_root / label
        if skip_run:
            metrics[label] = load_metrics(out_dir)
            continue
        if label == "baseline":
            metrics[label] = run_baseline_metrics(fixtures, trace_dir=trace_dir, out_dir=out_dir)
        elif label == "arm-a":
            metrics[label] = run_arm_a_metrics(
                fixtures,
                trace_dir=trace_dir,
                out_dir=out_dir,
                debug_trace_dir=trace_root / "arm-a-debug",
            )
        elif label == "arm-b":
            metrics[label] = run_arm_b_metrics(fixtures, trace_dir=trace_dir, out_dir=out_dir)
        else:
            raise ValueError(f"unknown arm {label!r}")
    return metrics


# --- rendering --------------------------------------------------------------
def _cell(sc: ArmScore, qid: str) -> str:
    chain = "OK" if sc.chain_pass.get(qid) else "X"
    depth = "OK" if sc.depth_pass.get(qid) else "X"
    d = sc.depth_reached.get(qid, 0)
    g = sc.groundedness.get(qid)
    gnd = f"{g[0]}/{g[1]}" if g else "-"
    return f"{chain}/{depth}/{gnd} (d{d})"


def render_comparison_table(fixtures: list[Fixture], scores: dict[str, ArmScore]) -> str:
    """Per-fixture 3-way table (chain/depth/groundedness) + per-arm totals."""
    labels = [lb for lb in ARM_LABELS if lb in scores]
    lines: list[str] = [
        "# 3-way comparison — baseline vs Arm A (self-hosted loop) vs Arm B (Managed Agents)",
        "",
        "Per fixture, each arm cell is `chain/depth/groundedness (depth reached)` — "
        "`OK`/`X` for the Layer-1 chain & depth checks, and the Layer-2 groundedness "
        "score `n/3` (`-` if the judge was skipped).",
        "",
    ]

    header = "| fixture | class | exp. depth | " + " | ".join(labels) + " |"
    sep = "| --- | --- | --- | " + " | ".join("---" for _ in labels) + " |"
    lines += [header, sep]
    for fx in fixtures:
        cells = " | ".join(_cell(scores[lb], fx.id) for lb in labels)
        lines.append(f"| {fx.id} | {fx.classification} | {fx.expected_stop_depth} | {cells} |")

    # Per-arm totals.
    lines += [
        "",
        "## Per-arm totals",
        "",
        "| arm | branch-correct (chain+depth) | grounded (>=2/3) | input tok* | output tok | "
        "cost $ | wall-clock s | mean latency s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    n = len(fixtures)
    for lb in labels:
        sc = scores[lb]
        branch = sum(
            1 for fx in fixtures if sc.chain_pass.get(fx.id) and sc.depth_pass.get(fx.id)
        )
        grounded = sum(
            1 for fx in fixtures if (g := sc.groundedness.get(fx.id)) and g[0] >= _GROUNDED_PASS
        )
        gnd_total = "n/a" if not sc.groundedness else f"{grounded}/{n}"
        # Total input-side tokens processed = uncached input + cache read + cache
        # write. For Arm B these come from session.usage (caching auto-enabled);
        # baseline / Arm A carry no cache split in their trace, so this is their
        # recorded input_tokens.
        in_tok = sum(
            m.input_tokens + m.cache_read_tokens + m.cache_write_tokens
            for m in sc.metrics.values()
        )
        out_tok = sum(m.output_tokens for m in sc.metrics.values())
        cost = sum(m.cost_usd for m in sc.metrics.values())
        wall = sum(m.latency_s for m in sc.metrics.values())
        mean_lat = wall / n if n else 0.0
        lines.append(
            f"| {lb} | {branch}/{n} | {gnd_total} | {in_tok:,} | {out_tok:,} | "
            f"${cost:.4f} | {wall:.1f} | {mean_lat:.1f} |"
        )
    lines += [
        "",
        "\\* *input tok* = total input-side tokens processed (uncached input + prompt-cache "
        "read + write). Managed Agents (Arm B) auto-enables prompt caching, so most of its "
        "input is cache-read (priced at ~0.1x); the baseline caches nothing and Arm A's trace "
        "carries no cache split. **`cost $` is the most directly comparable spend metric** — it "
        "prices each arm's tokens (incl. Arm B's cache read/write) at `opus-4-8` rates; output "
        "tokens and latency are cache-independent and comparable as-is.",
    ]
    return "\n".join(lines) + "\n"


def compare(
    fixtures: list[Fixture],
    *,
    trace_root: Path,
    out_root: Path,
    arms: tuple[str, ...] = ARM_LABELS,
    skip_run: bool = False,
) -> str:
    """Run/score the arms and return the rendered 3-way comparison markdown."""
    metrics = run_arms(
        fixtures, trace_root=trace_root, out_root=out_root, arms=arms, skip_run=skip_run
    )
    scores: dict[str, ArmScore] = {}
    for label in arms:
        scores[label] = score_arm(
            label,
            trace_dir=trace_root / label,
            fixtures=fixtures,
            metrics=metrics[label],
        )
    table = render_comparison_table(fixtures, scores)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "compare.md").write_text(table, encoding="utf-8")
    return table


__all__ = ["ArmScore", "compare", "render_comparison_table", "run_arms", "score_arm"]
