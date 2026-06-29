"""Run-trace writer — emits the JSONL schema ``agent-evals`` scores.

A run trace is the durable, agent-agnostic record of one investigation: the
ordered retrieval hops (each tagged with its source *type* and locator), the
per-turn token usage, the depth reached, and the final answer. The single-shot
baseline emits it now; the Arm A agent loop (C3) will emit the *same* format
later, so the branch-correctness scorer compares both arms apples-to-apples.

The schema is exactly what ``agent_evals.trace.TraceRecord.load`` consumes
(``query_meta`` / ``loop_start`` / ``claude_response`` / ``tool_use`` /
``tool_result`` | ``tool_error`` / ``loop_end``). We emit it directly rather
than depending on ``agent-core``'s Tracer: a thin writer is simpler than taking
a whole agent-runtime dependency just to serialise six event kinds, and it keeps
the trace contract owned where the eval can see it.

Key modelling choice — **one retrieval hop == one ``tool_use`` event, whose
``tool`` name is the canonical source type**. ``agent-evals``'s ``TraceRecord``
has no native "depth" or "source chain" field, so we encode both inside the
tool-call stream it already understands: ``depth_reached`` is the tool-call
count and the source chain is ``tools_called``. The locator/finding ride in the
tool ``input``. This needs zero changes to the shared package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from credit_risk_monitoring.sources import SourceType


@dataclass
class TraceWriter:
    """Accumulates ordered events for one run, then writes a JSONL trace.

    Usage mirrors an agent loop's lifecycle::

        tw = TraceWriter(run_id=..., query_id=..., category=..., question=...,
                         model=..., expected_chain=[...])
        tw.start()
        tw.record_turn(stop_reason="tool_use", input_tokens=..., output_tokens=...)
        tw.record_hop(SourceType.EDGAR_SUBMISSIONS_INDEX, locator=..., preview=...)
        ...
        tw.finish(final_text=..., stop_reason="end_turn")
        tw.write(Path("traces/baseline"))
    """

    run_id: str
    query_id: str
    category: str
    question: str
    model: str
    expected_chain: tuple[str, ...] = ()
    skills_enabled: bool = False
    prompt_version: str = ""
    max_turns: int = 1
    note: str = ""

    _events: list[dict[str, Any]] = field(default_factory=list, init=False)
    _turn: int = field(default=0, init=False)
    _tool_calls: int = field(default=0, init=False)
    _finished: bool = field(default=False, init=False)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Emit query_meta + loop_start. ``expected_tools`` carries the
        fixture's expected source chain so the trace is self-describing and the
        package's own default tool-selection/-count checks are meaningful too."""
        self._events.append(
            {
                "kind": "query_meta",
                "run_id": self.run_id,
                "query_id": self.query_id,
                "category": self.category,
                "expected_tools": list(self.expected_chain),
                "skills_enabled": self.skills_enabled,
                "note": self.note,
            }
        )
        self._events.append(
            {
                "kind": "loop_start",
                "question": self.question,
                "model": self.model,
                "prompt_version": self.prompt_version,
                "max_turns": self.max_turns,
                "tools": [t.value for t in SourceType],
            }
        )

    def record_turn(
        self, *, stop_reason: str, input_tokens: int = 0, output_tokens: int = 0
    ) -> int:
        """Record one model response; returns the (1-based) turn number."""
        self._turn += 1
        self._events.append(
            {
                "kind": "claude_response",
                "turn": self._turn,
                "stop_reason": stop_reason,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
            }
        )
        return self._turn

    def record_hop(
        self,
        source_type: SourceType,
        *,
        locator: str,
        preview: str = "",
        finding: str = "",
        turn: int | None = None,
    ) -> None:
        """Record one successful retrieval hop (tool_use + tool_result).

        The hop's ``tool`` name is the canonical source type — that is what the
        chain/depth checks read. ``locator``/``finding`` go in the tool input so
        a reviewer can audit *what* was fetched.
        """
        t = self._turn if turn is None else turn
        tool = source_type.value
        tool_use_id = f"{self.query_id}-hop{self._tool_calls + 1}"
        self._events.append(
            {
                "kind": "tool_use",
                "turn": t,
                "tool": tool,
                "tool_use_id": tool_use_id,
                "input": {"locator": locator, "finding": finding},
            }
        )
        self._events.append(
            {
                "kind": "tool_result",
                "turn": t,
                "tool": tool,
                "tool_use_id": tool_use_id,
                "preview": preview[:300],
                "preview_len": len(preview),
            }
        )
        self._tool_calls += 1

    def record_hop_error(
        self,
        source_type: SourceType,
        *,
        locator: str,
        error: str,
        turn: int | None = None,
    ) -> None:
        """Record a retrieval hop that failed (tool_use + tool_error)."""
        t = self._turn if turn is None else turn
        tool = source_type.value
        tool_use_id = f"{self.query_id}-hop{self._tool_calls + 1}"
        self._events.append(
            {
                "kind": "tool_use",
                "turn": t,
                "tool": tool,
                "tool_use_id": tool_use_id,
                "input": {"locator": locator},
            }
        )
        self._events.append(
            {
                "kind": "tool_error",
                "turn": t,
                "tool": tool,
                "tool_use_id": tool_use_id,
                "error": error,
            }
        )
        self._tool_calls += 1

    def finish(self, *, final_text: str, stop_reason: str = "end_turn") -> None:
        """Emit loop_end. ``final_text_full`` carries the whole answer so the
        Layer-2 judge scores the real text, not a preview."""
        self._events.append(
            {
                "kind": "loop_end",
                "turns": self._turn,
                "tool_calls": self._tool_calls,
                "stop_reason": stop_reason,
                "final_text_full": final_text,
                "final_text_preview": final_text[:300],
            }
        )
        self._finished = True

    # -- output -------------------------------------------------------------
    @property
    def depth_reached(self) -> int:
        return self._tool_calls

    def to_jsonl(self) -> str:
        if not self._finished:
            raise RuntimeError("TraceWriter.finish() must be called before output")
        return "".join(json.dumps(ev) + "\n" for ev in self._events)

    def write(self, trace_dir: str | Path) -> Path:
        """Write ``<query_id>.jsonl`` into ``trace_dir`` and return its path."""
        out_dir = Path(trace_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.query_id}.jsonl"
        path.write_text(self.to_jsonl(), encoding="utf-8")
        return path
