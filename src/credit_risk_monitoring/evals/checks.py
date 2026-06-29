"""Layer-1 branch-correctness checks (built on ``agent-evals``).

Two domain checks score the mechanical, judgement-free part of an investigation
against a fixture's ground truth — pure functions over a ``TraceRecord``, no API
calls, the gating signal for CI:

* **chain_correctness** — did the run traverse the right *source types* in the
  right dependency order? Defined as: the run's source chain
  (``record.tools_called``) is a non-empty order-preserving **prefix** of the
  fixture's ``expected_chain``. This isolates "every hop taken was the correct
  next source" from "did it go far enough" (that is depth's job). A run that
  walked the right path but stopped short still passes *chain* (depth catches the
  shortfall); a run that visited a wrong source type, or went *past* the chain,
  fails.

* **depth_correctness** — did it stop at ``expected_stop_depth``? The depth
  reached is the trace's tool-call count (one hop == one tool call). Equal →
  pass; **fewer** → fail (under-investigation, stopped short / missed); **more**
  → fail (over-investigation — for controls/trap, ``expected_stop_depth == 1``,
  so *any* deeper traversal fails here, which is exactly the over-investigation
  the controls exist to catch).

Both are registered as *universal* checks (run on every record) that look the
fixture up by ``query_id`` from a registry captured at suite-build time — the
clean way to give a per-record check its ground truth through ``agent-evals``'
``(TraceRecord) -> CheckResult`` contract. ``build_suite`` composes them on top
of the package defaults, so the universal Layer-1 checks
(``tool_selection``/``tool_count``/``stopped_cleanly``/…) run alongside.
"""

from __future__ import annotations

from agent_evals import CheckResult, CheckSuite, Outcome
from agent_evals.trace import TraceRecord

from credit_risk_monitoring.fixtures import Fixture


def make_chain_correctness_check(by_id: dict[str, Fixture]):
    """Build the chain-correctness check bound to a fixture registry."""

    def chain_correctness(r: TraceRecord) -> CheckResult:
        fx = by_id.get(r.query_id)
        if fx is None:
            return CheckResult(
                "chain_correctness", Outcome.NA, f"no fixture for query_id={r.query_id!r}"
            )
        expected = list(fx.expected_chain_values)
        actual = list(r.tools_called)
        if not actual:
            return CheckResult(
                "chain_correctness",
                Outcome.FAIL,
                f"no retrieval hops; expected chain {expected}",
            )
        # Over-traversal: ran more hops than the chain has -> not a prefix.
        if len(actual) > len(expected):
            return CheckResult(
                "chain_correctness",
                Outcome.FAIL,
                f"over-traversed: ran {actual}, chain is only {expected}",
            )
        prefix = expected[: len(actual)]
        if actual == prefix:
            walked = "full chain" if len(actual) == len(expected) else f"prefix {len(actual)}/{len(expected)}"
            return CheckResult(
                "chain_correctness",
                Outcome.PASS,
                f"correct source order ({walked}): {actual}",
            )
        # Find the first divergence for an actionable detail.
        diverged_at = next(
            (i for i in range(len(actual)) if actual[i] != prefix[i]), len(actual)
        )
        return CheckResult(
            "chain_correctness",
            Outcome.FAIL,
            f"wrong source at hop {diverged_at + 1}: got {actual}, expected prefix {prefix}",
        )

    return chain_correctness


def make_depth_correctness_check(by_id: dict[str, Fixture]):
    """Build the depth-correctness check bound to a fixture registry."""

    def depth_correctness(r: TraceRecord) -> CheckResult:
        fx = by_id.get(r.query_id)
        if fx is None:
            return CheckResult(
                "depth_correctness", Outcome.NA, f"no fixture for query_id={r.query_id!r}"
            )
        expected = fx.expected_stop_depth
        reached = r.tool_call_count
        if reached == expected:
            return CheckResult(
                "depth_correctness",
                Outcome.PASS,
                f"stopped at depth {reached} as expected",
            )
        if reached < expected:
            return CheckResult(
                "depth_correctness",
                Outcome.FAIL,
                f"under-investigation: stopped at depth {reached}, expected {expected}",
            )
        return CheckResult(
            "depth_correctness",
            Outcome.FAIL,
            f"over-investigation: reached depth {reached}, expected {expected}",
        )

    return depth_correctness


def build_suite(fixtures: list[Fixture]) -> CheckSuite:
    """Compose the branch-correctness suite: package defaults + the two
    domain checks bound to this fixture set."""
    by_id = {f.id: f for f in fixtures}
    return CheckSuite.with_defaults(
        extra_universal=[
            make_chain_correctness_check(by_id),
            make_depth_correctness_check(by_id),
        ],
    )
