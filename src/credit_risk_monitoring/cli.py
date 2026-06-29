"""CLI for the branch-correctness eval surface.

Two subcommands:

* ``run-baseline`` — run the single-shot baseline over all fixtures, writing one
  JSONL trace per fixture into ``--trace-dir``.
* ``score`` — score the traces in ``--trace-dir`` against the fixtures (Layer 1
  always; Layer 2 groundedness when ``ANTHROPIC_API_KEY`` is set), write the
  report/JSON to ``--out-dir``, and fan out to Braintrust when configured.

Keys are read from the environment by the shared packages. The Makefile wraps
these with ``uv run --env-file`` so secrets live in the invocation surface.
``score`` exits non-zero when any check failed (CI gate).
"""

from __future__ import annotations

import argparse
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
