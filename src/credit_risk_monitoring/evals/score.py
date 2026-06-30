"""Scoring harness — load traces, score branch-correctness, surface results.

Ties the pieces together:

1. Load JSONL traces (``agent_evals.load_directory``).
2. Layer 1 — score every trace against the fixture-bound :func:`build_suite`
   (chain + depth + the package's universal checks). Always runs; no API key.
3. Layer 2 — run the groundedness judge (:func:`build_criteria`) when
   ``ANTHROPIC_API_KEY`` is set; skipped with a note otherwise.
4. Render the ``agent-evals`` Markdown report (+ a per-fixture branch-correctness
   verdict table) and write JSON.
5. Fan out to Braintrust via the shared adapter — active iff ``BRAINTRUST_API_KEY``
   is set and the extra is installed; otherwise a silent no-op.

All keys are read from ``os.environ`` by the shared packages; this module loads
no .env. The invocation surface (Makefile / ``uv run --env-file``) owns secrets.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from agent_evals import Outcome, ScoredRecord, load_directory, render_single_run, score_all

from credit_risk_monitoring.evals.checks import build_suite
from credit_risk_monitoring.evals.criteria import build_criteria
from credit_risk_monitoring.fixtures import Fixture

# The Layer-1 checks whose conjunction defines the per-fixture branch-correctness
# verdict (chain right + depth right). Groundedness (Layer 2) is folded in when
# the judge ran.
_VERDICT_CHECKS = ("chain_correctness", "depth_correctness")

# Layer-2 judge model for the piece. Replaces C2's legacy claude-haiku-4-5
# default with the current Sonnet generation; env-overridable so a cheaper or
# newer judge can be swapped without code changes.
DEFAULT_JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")


@dataclass
class ScoreOutcome:
    scored: list[ScoredRecord]
    markdown: str
    judge_markdown: str | None
    total_fail: int
    verdicts: dict[str, bool]  # query_id -> branch-correctness pass (Layer 1)


def _check_passed(s: ScoredRecord, name: str) -> bool:
    for c in s.checks:
        if c.name == name:
            return c.outcome == Outcome.PASS
    return False


def _branch_verdict(s: ScoredRecord) -> bool:
    return all(_check_passed(s, name) for name in _VERDICT_CHECKS)


def _render_verdict_table(scored: list[ScoredRecord], by_id: dict[str, Fixture]) -> str:
    lines = [
        "## Branch-correctness verdict (Layer 1)",
        "",
        "| fixture | classification | chain | depth | verdict |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in sorted(scored, key=lambda x: x.record.query_id):
        fx = by_id.get(s.record.query_id)
        cls = fx.classification if fx else s.record.category
        chain = "PASS" if _check_passed(s, "chain_correctness") else "FAIL"
        depth = "PASS" if _check_passed(s, "depth_correctness") else "FAIL"
        verdict = "PASS" if _branch_verdict(s) else "FAIL"
        lines.append(f"| {s.record.query_id} | {cls} | {chain} | {depth} | {verdict} |")
    return "\n".join(lines) + "\n"


async def _run_judge_async(
    scored: list[ScoredRecord], fixtures: list[Fixture], label: str
) -> str:
    from agent_evals.judge import render_judge_run, run_judge

    criteria = build_criteria(fixtures)
    judged = await run_judge(
        records=scored, criteria=criteria, model=DEFAULT_JUDGE_MODEL, label=label
    )
    return render_judge_run(judged)


def score_traces(
    *,
    trace_dir: Path,
    fixtures: list[Fixture],
    label: str = "baseline",
    out_dir: Path | None = None,
    run_judge_layer2: bool | None = None,
) -> ScoreOutcome:
    """Score every trace in ``trace_dir`` against the fixtures.

    ``run_judge_layer2`` defaults to "run iff ANTHROPIC_API_KEY is set".
    """
    records = load_directory(trace_dir)
    if not records:
        raise FileNotFoundError(f"no traces found under {trace_dir}")

    by_id = {f.id: f for f in fixtures}
    suite = build_suite(fixtures)
    scored = score_all(records, suite)

    # Layer-1 report + branch-correctness verdict table.
    markdown = render_single_run(scored, label)
    verdict_table = _render_verdict_table(scored, by_id)
    markdown = f"{markdown}\n{verdict_table}"

    # Layer 2 — groundedness judge.
    if run_judge_layer2 is None:
        run_judge_layer2 = bool(os.environ.get("ANTHROPIC_API_KEY"))
    judge_markdown: str | None = None
    if run_judge_layer2:
        judge_markdown = asyncio.run(_run_judge_async(scored, fixtures, label))
    else:
        markdown += (
            "\n> _Layer-2 groundedness judge skipped — set `ANTHROPIC_API_KEY` "
            "to run it._\n"
        )

    verdicts = {s.record.query_id: _branch_verdict(s) for s in scored}
    total_fail = sum(s.fail_count for s in scored)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(markdown, encoding="utf-8")
        if judge_markdown is not None:
            (out_dir / "judge.md").write_text(judge_markdown, encoding="utf-8")
        (out_dir / "results.json").write_text(
            json.dumps(_results_payload(scored, label, verdicts), indent=2, default=str),
            encoding="utf-8",
        )

    _fan_out_braintrust(scored, label)

    return ScoreOutcome(
        scored=scored,
        markdown=markdown,
        judge_markdown=judge_markdown,
        total_fail=total_fail,
        verdicts=verdicts,
    )


def _results_payload(
    scored: list[ScoredRecord], label: str, verdicts: dict[str, bool]
) -> dict:
    return {
        "label": label,
        "query_count": len(scored),
        "records": [
            {
                "query_id": s.record.query_id,
                "category": s.record.category,
                "branch_verdict": verdicts.get(s.record.query_id, False),
                "depth_reached": s.record.tool_call_count,
                "source_chain": list(s.record.tools_called),
                "pass_count": s.pass_count,
                "fail_count": s.fail_count,
                "checks": [
                    {"name": c.name, "outcome": c.outcome.value, "detail": c.detail}
                    for c in s.checks
                ],
            }
            for s in scored
        ],
    }


def _fan_out_braintrust(scored: list[ScoredRecord], label: str) -> None:
    """Post to Braintrust via the shared adapter; no-op without the key/SDK."""
    try:
        from agent_evals.adapters import braintrust
    except ImportError:
        return
    if not braintrust.is_enabled():
        return
    try:
        braintrust.post_run(scored, run_label=label)
    except ImportError as exc:
        print(f"note: braintrust fan-out skipped — {exc}")
    except Exception as exc:  # noqa: BLE001 — never let surfacing crash scoring
        print(f"warning: braintrust fan-out failed: {exc}")
