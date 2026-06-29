"""The single-shot baseline.

For each fixture it does exactly **one** retrieval — the issuer's EDGAR
recent-filings index — then **one** ``llm-provider`` completion answering the
question from that sole evidence, and emits a trace.

The single-retrieval limit is the whole point. A credit screen that pulls the
filings list and stops is the right move for a *healthy* issuer (the controls:
"glanced at recent filings, nothing distressed, stop") but cannot reach a
depth-3/4 *downstream* fact — an exhibit's subsidiary, a foreign-registry charge
id. So the baseline is engineered to **pass the single-hop controls and fail the
multi-hop fixtures**, and that gap is the honest measure of the agent's win.

What this is NOT: the Arm A agent loop (C3). This is the deliberately weak
reference point the agent is measured against. The retrieval helper
(``sources.EdgarClient``) is structured so the C3 spine extends it with further
hops rather than replacing it.

Provider selection goes through ``llm-provider``'s ``get_provider()`` (reads
``LLM_PROVIDER``; default Anthropic). Keys are read from ``os.environ`` by the
SDKs — never a .env loader. When no provider key is configured the baseline
falls back to a clearly-labelled offline answerer so the Layer-1 branch-
correctness surface (which needs no LLM) is still runnable in credential-less
CI; that fallback is stamped into the trace ``note`` and never masquerades as a
real answer.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from credit_risk_monitoring.fixtures import Fixture
from credit_risk_monitoring.sources import EdgarClient, SourceType, SubmissionsIndex
from credit_risk_monitoring.trace import TraceWriter

# (question, evidence_context) -> (answer_text, input_tokens, output_tokens)
Answerer = Callable[[str, str], Awaitable[tuple[str, int, int]]]

DEFAULT_BASELINE_MODEL = os.environ.get("BASELINE_MODEL", "claude-sonnet-4-5")

_SYSTEM_PROMPT = (
    "You are a credit-risk analyst screening an issuer for signs of credit "
    "deterioration (covenant waivers, defaults, forbearance, going-concern "
    "doubt, bankruptcy). You are given the issuer's recent SEC filing index as "
    "your ONLY evidence — you cannot open filing bodies, exhibits, or any "
    "registry. Answer the question strictly from this evidence. If the evidence "
    "is insufficient to reach a downstream fact, say so explicitly rather than "
    "guessing or drawing on prior knowledge. If recent filings are routine with "
    "no distress signal, say so and recommend no further investigation."
)


def _build_prompt(question: str, evidence: str) -> str:
    return (
        f"# Question\n{question}\n\n"
        f"# Evidence (single retrieval — EDGAR recent-filings index)\n{evidence}\n\n"
        "# Task\nAnswer the question using only the evidence above."
    )


def llm_answerer(model: str = DEFAULT_BASELINE_MODEL) -> Answerer:
    """Default answerer: one ``llm-provider`` completion.

    Imports + constructs the provider lazily so a missing key surfaces only when
    a live answer is actually requested (the offline path never touches it).
    """

    async def _answer(question: str, evidence: str) -> tuple[str, int, int]:
        from llm_provider import Message, ProviderConfig, get_provider

        provider = get_provider()
        completion = await provider.complete(
            [Message(role="user", content=_build_prompt(question, evidence))],
            config=ProviderConfig(
                model=model, system=_SYSTEM_PROMPT, max_tokens=1024, temperature=0.0
            ),
        )
        return (
            completion.text,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )

    return _answer


async def offline_answerer(question: str, evidence: str) -> tuple[str, int, int]:
    """Credential-less fallback. Returns a clearly-marked non-answer.

    Layer-1 (chain + depth) scores the retrieval, not this text, so the
    branch-correctness demonstration runs fully offline. Layer-2 groundedness
    is deferred until a provider key is present.
    """
    note = (
        "[OFFLINE BASELINE — no LLM provider key configured; answer deferred. "
        "Single retrieval (EDGAR submissions index) was performed; its summary "
        "follows for Layer-1 scoring.]\n\n" + evidence
    )
    return note, 0, 0


def provider_key_present() -> bool:
    """True iff the selected provider's API key is in the environment."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def select_answerer(force_offline: bool = False) -> tuple[Answerer, bool]:
    """Pick the answerer; returns ``(answerer, is_offline)``."""
    if force_offline or not provider_key_present():
        return offline_answerer, True
    return llm_answerer(), False


@dataclass
class BaselineResult:
    fixture_id: str
    trace_path: Path
    depth_reached: int
    retrieval_ok: bool
    offline: bool


async def run_one(
    fixture: Fixture,
    *,
    edgar: EdgarClient,
    answerer: Answerer,
    offline: bool,
    trace_dir: Path,
    model: str = DEFAULT_BASELINE_MODEL,
    run_id: str | None = None,
) -> BaselineResult:
    """Run the single-shot baseline for one fixture and write its trace."""
    tw = TraceWriter(
        run_id=run_id or f"baseline/{fixture.id}",
        query_id=fixture.id,
        category=fixture.classification,
        question=fixture.question,
        model=model,
        expected_chain=fixture.expected_chain_values,
        skills_enabled=False,
        prompt_version="baseline-single-shot-v1",
        max_turns=1,
        note="offline-baseline-no-llm" if offline else "single-shot-baseline",
    )
    tw.start()

    # Turn 1: the model "decides" to do its one retrieval.
    tw.record_turn(stop_reason="tool_use")

    # The single retrieval — real EDGAR call.
    index: SubmissionsIndex | None = None
    retrieval_ok = True
    try:
        index = edgar.fetch_submissions_index(fixture.cik)
        tw.record_hop(
            SourceType.EDGAR_SUBMISSIONS_INDEX,
            locator=index.locator,
            preview=index.context_block(),
            finding=f"recent-filings index for {index.issuer_name}",
        )
    except (httpx.HTTPError, ValueError) as exc:
        retrieval_ok = False
        tw.record_hop_error(
            SourceType.EDGAR_SUBMISSIONS_INDEX,
            locator=f"CIK {fixture.cik}",
            error=f"{type(exc).__name__}: {exc}",
        )

    # The single completion — answer from that sole evidence.
    evidence = index.context_block() if index is not None else "(retrieval failed)"
    answer, in_tok, out_tok = await answerer(fixture.question, evidence)

    # Turn 2: the model emits the final answer and stops.
    tw.record_turn(stop_reason="end_turn", input_tokens=in_tok, output_tokens=out_tok)
    tw.finish(final_text=answer, stop_reason="end_turn")

    path = tw.write(trace_dir)
    return BaselineResult(
        fixture_id=fixture.id,
        trace_path=path,
        depth_reached=tw.depth_reached,
        retrieval_ok=retrieval_ok,
        offline=offline,
    )


async def run_baseline(
    fixtures: list[Fixture],
    *,
    trace_dir: Path,
    answerer: Answerer | None = None,
    offline: bool | None = None,
    model: str = DEFAULT_BASELINE_MODEL,
    edgar: EdgarClient | None = None,
) -> list[BaselineResult]:
    """Run the single-shot baseline over every fixture, writing one trace each.

    Retrievals run sequentially to stay polite to EDGAR's rate limit; the LLM
    completion per fixture is awaited in turn (a small bank — no need to fan out).
    """
    if answerer is None:
        answerer, resolved_offline = select_answerer(force_offline=bool(offline))
    else:
        resolved_offline = bool(offline)

    owns_client = edgar is None
    client = edgar or EdgarClient()
    try:
        results: list[BaselineResult] = []
        for fx in fixtures:
            results.append(
                await run_one(
                    fx,
                    edgar=client,
                    answerer=answerer,
                    offline=resolved_offline,
                    trace_dir=trace_dir,
                    model=model,
                )
            )
        return results
    finally:
        if owns_client:
            client.close()


def run_baseline_sync(fixtures: list[Fixture], **kwargs: object) -> list[BaselineResult]:
    return asyncio.run(run_baseline(fixtures, **kwargs))  # type: ignore[arg-type]
