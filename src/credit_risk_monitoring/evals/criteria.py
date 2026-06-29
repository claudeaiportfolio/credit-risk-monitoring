"""Layer-2 groundedness criteria (built on ``agent-evals``' judge).

Where Layer 1 scores *the path* (right sources, right order, right depth),
Layer 2 scores *the answer*: does the run's final text match the fixture's
verified ``expected_answer`` **without synthesising past the retrieved
sources**? That is a judgement call, so it runs through ``agent_evals.judge``
(Anthropic, Haiku by default) — one call per fixture.

The judge sees the rendered trace (question, the retrieval results, the final
answer) but not the gold answer, so we embed each fixture's ``expected_answer``
and ``verification_caveats`` into a **per-fixture** ``JudgeCriterion`` scoped via
``applies_to_query_id``. Every criterion is named ``groundedness`` so the
report/dashboard aggregates one groundedness score across the bank while each
only fires on its own case.

The rubric rewards two things the fixtures are built around: (1) hitting the
specific downstream fact (the charge id, the registry status, the named agent),
and (2) *not* fabricating — for the controls/trap the grounded answer is
"nothing further to find, stop", so confidently inventing distress fails.
"""

from __future__ import annotations

from agent_evals.judge import JudgeCriterion

from credit_risk_monitoring.fixtures import Fixture

_RUBRIC_TEMPLATE = """Judge whether the agent's FINAL ANSWER is grounded in the \
retrieved sources AND matches the verified expected answer for this case, \
WITHOUT synthesising facts the retrieved sources do not support.

Verified expected answer (ground truth):
{expected_answer}
{caveats}
Scoring (0-3):
0 = answer contradicts the expected answer, or fabricates specifics \
(charge ids, dates, parties, ratings) not present in the retrieved sources.
1 = answer is vaguely related but misses the key verified fact(s), or asserts \
unsupported specifics.
2 = answer captures the main verified fact but with a notable gap, hedge, or a \
minor unsupported detail.
3 = answer states the verified fact(s) and is fully grounded in the retrieved \
sources — nothing asserted beyond what the sources support. For a control/trap \
case whose expected answer is "no distress / benign / stop", a 3 requires the \
answer to reach that conclusion without inventing a credit event."""


def _caveats_block(fx: Fixture) -> str:
    if not fx.verification_caveats:
        return ""
    return (
        "\nVerification caveats (these facts are NOT ground truth — do not "
        f"penalise their absence, and treat asserting them as ungrounded):\n{fx.verification_caveats}\n"
    )


def build_criteria(fixtures: list[Fixture]) -> list[JudgeCriterion]:
    """One ``groundedness`` criterion per fixture, each carrying its gold answer
    and scoped to its own ``query_id``."""
    criteria: list[JudgeCriterion] = []
    for fx in fixtures:
        prompt = _RUBRIC_TEMPLATE.format(
            expected_answer=fx.expected_answer,
            caveats=_caveats_block(fx),
        )
        criteria.append(
            JudgeCriterion(
                name="groundedness",
                prompt=prompt,
                scale="rubric_0_3",
                applies_to_query_id=(fx.id,),
            )
        )
    return criteria
