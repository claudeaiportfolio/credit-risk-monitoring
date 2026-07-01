"""CLI for the branch-correctness eval surface.

Subcommands:

* ``run-baseline`` — run the single-shot baseline over all fixtures, writing one
  JSONL trace per fixture into ``--trace-dir``.
* ``run-agent`` — run the Arm A multi-hop investigation agent (orchestrator +
  sub-agents) over all fixtures, writing one JSONL trace per fixture into
  ``--trace-dir``. Needs ``ANTHROPIC_API_KEY`` (the agent loop) and, for the
  UK-registry hops, ``COMPANIES_HOUSE_API_KEY``.
* ``run-agent-b`` — run the Arm B multi-hop investigation on **Anthropic Managed
  Agents** (coordinator + sub-agent roster) over all fixtures, writing the SAME
  JSONL trace format into ``--trace-dir`` (default ``traces/agent-b``) and a
  metrics sidecar (tokens/cost/latency) into ``--out-dir``. Needs
  ``ANTHROPIC_API_KEY`` (the Managed Agents session) and, for the UK-registry
  hops, ``COMPANIES_HOUSE_API_KEY``.
* ``score`` — score the traces in ``--trace-dir`` against the fixtures (Layer 1
  always; Layer 2 groundedness when ``ANTHROPIC_API_KEY`` is set), write the
  report/JSON to ``--out-dir``, and fan out to Braintrust when configured.
* ``compare`` — run/score all three arms and render the 3-way build-vs-buy table
  (chain / depth / groundedness per fixture + per-arm tokens/$ cost/latency).

Keys are read from the environment by the shared packages. The Makefile wraps
these with ``uv run --env-file`` so secrets live in the invocation surface.
``score`` exits non-zero when any check failed (CI gate).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from credit_risk_monitoring.baseline import run_baseline_sync
from credit_risk_monitoring.evals.score import score_traces
from credit_risk_monitoring.fixtures import default_fixtures_path, load_fixtures


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="Path to fixtures.yaml (default: <repo>/fixtures/fixtures.yaml)",
    )
    p.add_argument(
        "--trace-dir",
        type=Path,
        default=Path("traces/baseline"),
        help="Directory for JSONL run traces (default: traces/baseline)",
    )


def _cmd_run_baseline(args: argparse.Namespace) -> int:
    fixtures = load_fixtures(args.fixtures or default_fixtures_path())
    results = run_baseline_sync(
        fixtures,
        trace_dir=args.trace_dir,
        offline=args.offline or None,
    )
    offline = any(r.offline for r in results)
    if offline:
        print(
            "NOTE: offline baseline (no LLM provider key) — answers are deferred "
            "stubs; Layer-1 branch-correctness is still fully scorable.",
            file=sys.stderr,
        )
    for r in results:
        status = "ok" if r.retrieval_ok else "RETRIEVAL-FAILED"
        print(f"  {r.fixture_id:<48} depth={r.depth_reached} [{status}] -> {r.trace_path}")
    print(f"wrote {len(results)} traces to {args.trace_dir}")
    return 0


def _cmd_run_agent(args: argparse.Namespace) -> int:
    # Imported lazily so `run-baseline`/`score` don't pull the agent runtime.
    from credit_risk_monitoring.agent.orchestrator import Orchestrator

    fixtures = load_fixtures(args.fixtures or default_fixtures_path())

    async def _run() -> list:
        orch = Orchestrator(debug_trace_dir=args.debug_trace_dir)
        try:
            results = []
            for fx in fixtures:
                results.append(await orch.investigate(fx, trace_dir=args.trace_dir))
            return results
        finally:
            orch.close()

    results = asyncio.run(_run())
    for r in results:
        status = f"ERROR: {r.error}" if r.error else "ok"
        print(
            f"  {r.fixture_id:<48} depth={r.depth_reached} "
            f"resolved={r.resolution_count} [{status}] -> {r.trace_path}"
        )
    print(f"wrote {len(results)} agent traces to {args.trace_dir}")
    return 0


def _cmd_run_agent_b(args: argparse.Namespace) -> int:
    # Imported lazily so the other subcommands don't pull the Managed Agents SDK path.
    from credit_risk_monitoring.agent_managed.metrics import run_arm_b_metrics

    fixtures = load_fixtures(args.fixtures or default_fixtures_path())
    metrics = run_arm_b_metrics(fixtures, trace_dir=args.trace_dir, out_dir=args.out_dir)
    for fx in fixtures:
        m = metrics[fx.id]
        status = f"ERROR: {m.error}" if m.error else "ok"
        print(
            f"  {fx.id:<48} in={m.input_tokens} out={m.output_tokens} "
            f"${m.cost_usd:.4f} {m.latency_s:.1f}s [{status}]"
        )
    print(f"wrote {len(fixtures)} Arm B traces to {args.trace_dir}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    from credit_risk_monitoring.agent_managed.compare import ARM_LABELS, compare

    fixtures = load_fixtures(args.fixtures or default_fixtures_path())
    if args.only:
        fixtures = [f for f in fixtures if f.id in set(args.only)]
    elif args.limit:
        fixtures = fixtures[: args.limit]
    arms = tuple(args.arms) if args.arms else ARM_LABELS
    table = compare(
        fixtures,
        trace_root=args.trace_root,
        out_root=args.out_dir,
        arms=arms,
        skip_run=args.skip_run,
    )
    print(table)
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    fixtures = load_fixtures(args.fixtures or default_fixtures_path())
    outcome = score_traces(
        trace_dir=args.trace_dir,
        fixtures=fixtures,
        label=args.label,
        out_dir=args.out_dir,
    )
    print(outcome.markdown)
    if outcome.judge_markdown is not None:
        print(outcome.judge_markdown)
    return 1 if outcome.total_fail else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="credit-risk-eval", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run-baseline", help="run the single-shot baseline over all fixtures")
    _add_common(p_run)
    p_run.add_argument(
        "--offline",
        action="store_true",
        help="force the offline answerer even if a provider key is present",
    )
    p_run.set_defaults(func=_cmd_run_baseline)

    p_agent = sub.add_parser(
        "run-agent", help="run the Arm A multi-hop investigation agent over all fixtures"
    )
    _add_common(p_agent)
    p_agent.add_argument(
        "--debug-trace-dir",
        type=Path,
        default=Path("traces/agent-debug"),
        help="Directory for the agent-core per-sub-agent debug traces.",
    )
    # Default the agent traces to their own dir so they don't clobber baseline.
    p_agent.set_defaults(func=_cmd_run_agent, trace_dir=Path("traces/agent"))

    p_agent_b = sub.add_parser(
        "run-agent-b", help="run the Arm B Managed Agents investigation over all fixtures"
    )
    _add_common(p_agent_b)
    p_agent_b.add_argument(
        "--out-dir", type=Path, default=Path("out/agent-b"),
        help="directory for the Arm B metrics sidecar (tokens/cost/latency)",
    )
    p_agent_b.set_defaults(func=_cmd_run_agent_b, trace_dir=Path("traces/agent-b"))

    p_compare = sub.add_parser(
        "compare", help="run/score all three arms and render the 3-way build-vs-buy table"
    )
    p_compare.add_argument("--fixtures", type=Path, default=None)
    p_compare.add_argument(
        "--trace-root", type=Path, default=Path("traces"),
        help="root for per-arm trace subdirs (baseline/arm-a/arm-b)",
    )
    p_compare.add_argument(
        "--out-dir", type=Path, default=Path("out/compare"),
        help="root for per-arm metrics + the rendered comparison table",
    )
    p_compare.add_argument(
        "--arms", nargs="+", choices=["baseline", "arm-a", "arm-b"], default=None,
        help="subset of arms to run/score (default: all three)",
    )
    p_compare.add_argument(
        "--skip-run", action="store_true",
        help="do not run the arms; score existing traces + metrics sidecars",
    )
    p_compare.add_argument(
        "--limit", type=int, default=None, help="only the first N fixtures (spend-aware smoke)"
    )
    p_compare.add_argument(
        "--only", nargs="+", default=None, help="only these fixture ids (spend-aware smoke)"
    )
    p_compare.set_defaults(func=_cmd_compare)

    p_score = sub.add_parser("score", help="score traces against the fixtures")
    _add_common(p_score)
    p_score.add_argument("--label", default="baseline", help="run label for the report")
    p_score.add_argument(
        "--out-dir", type=Path, default=Path("out"), help="directory for report/JSON output"
    )
    p_score.set_defaults(func=_cmd_score)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
