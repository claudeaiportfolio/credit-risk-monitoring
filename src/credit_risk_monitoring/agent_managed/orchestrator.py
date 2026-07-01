"""Arm B data plane — drive one Managed Agents session and emit the scored trace.

Per fixture, the :class:`ManagedOrchestrator`:

1. creates a **session** referencing the persisted coordinator agent + the
   environment (the agent roster is the control plane — created once, see
   :mod:`~credit_risk_monitoring.agent_managed.roster`);
2. opens the SSE **event stream first**, then sends the kickoff ``user.message``
   (stream-first ordering — the stream only delivers events emitted after it
   opens);
3. consumes the stream. Each ``agent.custom_tool_use`` (from the coordinator's
   exposure / entity_resolution sub-agent threads, cross-posted to the primary
   thread) is executed **host-side** by Arm A's OWN tool handler
   (``TOOL_REGISTRY``) and answered with ``user.custom_tool_result`` — so the
   fetch and the secrets never leave this process, no public ingress is stood
   up, and the tool surface is byte-for-byte Arm A's. Every hop is recorded into
   the SAME C2 ``TraceWriter`` (one ``tool_use`` per hop, named by canonical
   ``SourceType``), so the UNCHANGED ``CheckSuite`` scores Arm B identically.
4. accumulates token usage (from ``span.model_request_end`` and, authoritatively,
   ``sessions.retrieve().usage``), measures wall-clock latency, and computes $
   cost — the build-vs-buy comparison data points.

The idle-break gate follows the Managed Agents client patterns: break on a
terminal ``session.status_idle`` (stop_reason ≠ ``requires_action``) or
``session.status_terminated``; keep streaming through transient idles.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx

from credit_risk_monitoring.agent.orchestrator import build_clients
from credit_risk_monitoring.agent.tools import TOOL_REGISTRY, Clients
from credit_risk_monitoring.agent_managed.roster import ARM_B_MODEL, Roster, ensure_roster
from credit_risk_monitoring.fixtures import Fixture
from credit_risk_monitoring.sources import SourceType
from credit_risk_monitoring.trace import TraceWriter

logger = logging.getLogger(__name__)

# opus-4-8 pricing ($ per token). Cache reads ~0.1x input, cache writes ~1.25x.
_PRICE_INPUT = 5.0 / 1_000_000
_PRICE_OUTPUT = 25.0 / 1_000_000
_PRICE_CACHE_READ = 0.5 / 1_000_000
_PRICE_CACHE_WRITE = 6.25 / 1_000_000

# Safety bounds so a wedged live session can't hang the eval forever.
_MAX_EVENTS = 5000
_MAX_WALL_CLOCK_S = 900.0


@dataclass
class ManagedRunResult:
    """Arm B per-fixture result — mirrors Arm A's ``AgentRunResult`` plus the
    build-vs-buy metrics (tokens, $ cost, wall-clock latency)."""

    fixture_id: str
    trace_path: Path
    depth_reached: int
    final_answer: str
    resolution_count: int
    session_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_s: float
    error: str | None = None


def token_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """opus-4-8 $ cost for a token bundle (input/output + cache read/write)."""
    return (
        input_tokens * _PRICE_INPUT
        + output_tokens * _PRICE_OUTPUT
        + cache_read_tokens * _PRICE_CACHE_READ
        + cache_write_tokens * _PRICE_CACHE_WRITE
    )


def _as_dict(obj: Any) -> dict[str, Any]:
    """Normalise an SDK event/usage object (Pydantic) or a plain dict to a dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except (TypeError, ValueError, AttributeError):
            # A serialisation quirk on an SDK object — fall through to an
            # attribute scan rather than failing the whole event.
            pass
    return {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_")}


def _text_from_content(content: Any) -> str:
    """Join the text blocks of an ``agent.message`` content array."""
    parts: list[str] = []
    for block in content or []:
        b = _as_dict(block) if not isinstance(block, dict) else block
        if b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]))
    return "".join(parts).strip()


def _usage_tokens(usage: Any) -> tuple[int, int, int, int]:
    """(uncached_input, output, cache_read, cache_write) from a usage dict/object.

    Handles both the flat ``span.model_request_end`` ``model_usage`` shape
    (``cache_creation_input_tokens``) and the aggregate ``session.usage`` shape,
    where cache writes live under a nested ``cache_creation`` dict
    (``{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}``). Managed Agents
    enables prompt caching automatically, so on a real run the bulk of the
    input volume is ``cache_read`` — the uncached ``input_tokens`` is small.
    """
    u = _as_dict(usage)

    def g(*keys: str) -> int:
        for k in keys:
            v = u.get(k)
            if v is not None:
                return int(v)
        return 0

    cache_write = g("cache_creation_input_tokens", "cache_creation_tokens", "cache_write_tokens")
    if not cache_write:
        cc = u.get("cache_creation")
        if isinstance(cc, dict):
            cache_write = sum(int(v or 0) for v in cc.values() if isinstance(v, (int, float)))

    return (
        g("input_tokens"),
        g("output_tokens"),
        g("cache_read_input_tokens", "cache_read_tokens"),
        cache_write,
    )


@dataclass
class ManagedOrchestrator:
    """Drives Arm B investigations against Managed Agents and emits scored traces.

    ``client`` is an ``anthropic.Anthropic`` (built lazily from the environment
    when omitted; injectable — a fake — for offline tests). ``clients`` are the
    Arm A retrieval spines the custom-tool handlers dispatch to (built from the
    environment when omitted). ``roster`` is the persisted control-plane resource
    IDs (created once via :func:`ensure_roster` when omitted).
    """

    client: Any = None
    clients: Clients | None = None
    roster: Roster | None = None
    model: str = ARM_B_MODEL
    title_prefix: str = "credit-risk arm-b"

    _owns_clients: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = anthropic.Anthropic()
        if self.clients is None:
            self.clients = build_clients()
            self._owns_clients = True
        if self.roster is None:
            self.roster = ensure_roster(self.client, model=self.model)

    # -- one investigation --------------------------------------------------
    def investigate(self, fixture: Fixture, *, trace_dir: Path) -> ManagedRunResult:
        assert self.roster is not None and self.clients is not None
        tw = TraceWriter(
            run_id=f"arm-b/{fixture.id}",
            query_id=fixture.id,
            category=fixture.classification,
            question=fixture.question,
            model=self.model,
            expected_chain=fixture.expected_chain_values,
            skills_enabled=False,
            prompt_version="c3b-arm-b-managed-v1",
            note="arm-b-managed-agents-coordinator-subagents",
        )
        tw.start()
        tw.record_turn(stop_reason="tool_use")  # the coordinator's opening turn

        started = time.monotonic()
        session_id = ""
        error: str | None = None
        final_answer = ""
        resolution_count = 0
        stream_in = stream_out = stream_cr = stream_cw = 0

        try:
            session = self.client.beta.sessions.create(
                agent=self.roster.coordinator_id,
                environment_id=self.roster.environment_id,
                title=f"{self.title_prefix}: {fixture.id}",
            )
            session_id = getattr(session, "id", "") or _as_dict(session).get("id", "")

            kickoff = (
                f"Issuer: {fixture.issuer_name} (CIK {fixture.cik}).\n"
                f"Question: {fixture.question}\n\n"
                "Run the credit-deterioration investigation: delegate to exposure, then resolve "
                "any downstream leads via entity_resolution (only if exposure did not recommend "
                "stopping), then give your final grounded answer as your last message."
            )

            final_answer, resolution_count, (stream_in, stream_out, stream_cr, stream_cw) = (
                self._drive_session(session_id, kickoff, tw)
            )
        except Exception as exc:  # noqa: BLE001 — always finish the trace (mirror Arm A)
            # Trace/result carry only the exception TYPE: an httpx/SDK error can
            # embed a credential-bearing URL in its message.
            error = type(exc).__name__
            # Log only the exception TYPE — an httpx/SDK error message can embed a
            # credential-bearing URL, so do NOT log the full traceback/message.
            logger.error("arm-b investigation %s crashed: %s", fixture.id, error)
            if not final_answer:
                final_answer = f"[investigation crashed: {error}]"

        latency_s = time.monotonic() - started

        # Authoritative token usage from the session object (falls back to the
        # sum accumulated off the stream if the retrieve fails).
        in_tok, out_tok, cr_tok, cw_tok = stream_in, stream_out, stream_cr, stream_cw
        if session_id:
            try:
                session = self.client.beta.sessions.retrieve(session_id=session_id)
                s_in, s_out, s_cr, s_cw = _usage_tokens(getattr(session, "usage", None))
                if s_in or s_out:
                    in_tok, out_tok, cr_tok, cw_tok = s_in, s_out, s_cr, s_cw
            except (anthropic.APIError, httpx.HTTPError) as exc:
                # Usage is best-effort (the investigation already finished), so we
                # don't abort — but surface it at WARNING with the exception TYPE
                # (an auth failure here must not stay silent; the message may carry
                # a credential-bearing URL, so log the type only).
                logger.warning(
                    "arm-b: session usage retrieve failed for %s (%s); "
                    "using stream-accumulated tokens",
                    fixture.id,
                    type(exc).__name__,
                )

        # Record the accounting turn so trace-derived tokens match session usage
        # (uniform with Arm A / baseline, whose tokens also live in claude_response).
        tw.record_turn(stop_reason="end_turn", input_tokens=in_tok, output_tokens=out_tok)
        tw.finish(final_text=final_answer, stop_reason="end_turn")
        path = tw.write(trace_dir)

        return ManagedRunResult(
            fixture_id=fixture.id,
            trace_path=path,
            depth_reached=tw.depth_reached,
            final_answer=final_answer,
            resolution_count=resolution_count,
            session_id=session_id,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cr_tok,
            cache_write_tokens=cw_tok,
            cost_usd=token_cost(in_tok, out_tok, cr_tok, cw_tok),
            latency_s=latency_s,
            error=error,
        )

    # -- the SSE event loop -------------------------------------------------
    def _drive_session(
        self, session_id: str, kickoff: str, tw: TraceWriter
    ) -> tuple[str, int, list[int]]:
        assert self.client is not None
        events_api = self.client.beta.sessions.events
        final_answer = ""
        resolution_count = 0
        usage = [0, 0, 0, 0]  # input, output, cache_read, cache_write
        started = time.monotonic()
        n_events = 0

        # Stream-first, then send the kickoff (Managed Agents Pattern 7).
        with events_api.stream(session_id=session_id) as stream:
            events_api.send(
                session_id=session_id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": kickoff}]}],
            )
            for event in stream:
                n_events += 1
                if n_events > _MAX_EVENTS or time.monotonic() - started > _MAX_WALL_CLOCK_S:
                    logger.warning("arm-b: session %s exceeded safety bound; stopping", session_id)
                    break

                ev = _as_dict(event)
                etype = ev.get("type", "")

                if etype == "agent.custom_tool_use":
                    src = self._handle_custom_tool(session_id, ev, tw)
                    if src in ("companies_house", "external_rating"):
                        resolution_count += 1
                    continue

                if etype == "agent.message":
                    text = _text_from_content(ev.get("content"))
                    if text:
                        final_answer = text  # keep the coordinator's latest message
                    continue

                if etype == "span.model_request_end":
                    mi, mo, mcr, mcw = _usage_tokens(ev.get("model_usage"))
                    usage[0] += mi
                    usage[1] += mo
                    usage[2] += mcr
                    usage[3] += mcw
                    continue

                if etype == "session.status_terminated":
                    break

                if etype == "session.status_idle":
                    stop = _as_dict(ev.get("stop_reason"))
                    if stop.get("type") == "requires_action":
                        continue  # waiting on a result we already sent; keep streaming
                    break  # end_turn / retries_exhausted — terminal

        return final_answer, resolution_count, usage

    def _handle_custom_tool(self, session_id: str, ev: dict[str, Any], tw: TraceWriter) -> str:
        """Execute one custom-tool call host-side (Arm A's handler), record the hop,
        and send the result back over the stream. Returns the tool/source name."""
        assert self.client is not None and self.clients is not None
        name = ev.get("name", "")
        args = ev.get("input") or {}
        tool_use_id = ev.get("id", "")
        thread_id = ev.get("session_thread_id")

        result_content = self._run_tool(name, args, tw)
        is_error = result_content.startswith("ERROR:")

        result_event: dict[str, Any] = {
            "type": "user.custom_tool_result",
            "custom_tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": result_content}],
        }
        if is_error:
            result_event["is_error"] = True
        if thread_id is not None:  # echo the originating sub-agent thread (multiagent)
            result_event["session_thread_id"] = thread_id

        self.client.beta.sessions.events.send(session_id=session_id, events=[result_event])
        return name

    def _run_tool(self, name: str, args: dict[str, Any], tw: TraceWriter) -> str:
        """Dispatch to Arm A's OWN tool handler and record the hop into the trace.

        Success -> ``record_hop`` (a scored retrieval); failure -> ``record_hop_error``
        (honest: a broken hop fails scoring), exactly as Arm A's ToolRouter does.
        """
        assert self.clients is not None
        tool_def = TOOL_REGISTRY.get(name)
        if tool_def is None:
            src = _source_type_or_default(name)
            tw.record_hop_error(src, locator=name, error=f"unknown tool {name!r}")
            return f"ERROR: unknown tool {name!r}"
        try:
            outcome = tool_def.handler(self.clients, args)
        except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
            # A retrieval failure is a scored hop_error (mirrors Arm A's ToolRouter);
            # anything outside this set is a bug and propagates.
            error = f"{type(exc).__name__}: {exc}"
            tw.record_hop_error(
                _source_type_or_default(name), locator=_locator_hint(name, args), error=error
            )
            return f"ERROR: {error}"
        tw.record_hop(
            outcome.source_type,
            locator=outcome.locator,
            preview=outcome.result_text,
            finding=outcome.finding,
        )
        return outcome.result_text

    def close(self) -> None:
        if self._owns_clients and self.clients is not None:
            self.clients.close()


def _source_type_or_default(name: str) -> SourceType:
    try:
        return SourceType(name)
    except ValueError:
        return SourceType.EDGAR_FILING


def _locator_hint(name: str, args: dict[str, Any]) -> str:
    for k in ("accession", "url", "company_number", "subject", "query", "cik"):
        if args.get(k):
            return f"{name}:{args[k]}"
    return name


__all__ = ["ManagedOrchestrator", "ManagedRunResult", "token_cost"]
