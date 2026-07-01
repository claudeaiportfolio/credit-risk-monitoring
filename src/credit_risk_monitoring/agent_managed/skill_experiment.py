"""The measured Skills experiment: Arm B **with** vs **without** the Skill.

The scoping doc requires the "attach a Managed Agents Skill?" decision to be made
by *measurement on THIS eval*, not assumed. This module is that measurement. It:

1. creates ONE shared environment, then TWO rosters — a **no-skill** roster and a
   **skill** roster (the investigation-discipline Skill attached to all three
   agents). A Skill binds at agent-create time, so the two variants are distinct
   agent sets over the same environment, tools, prompts, and model — the Skill is
   the ONLY difference;
2. runs each variant over the same fixtures via the UNCHANGED Arm B data plane
   (:func:`run_arm_b_metrics`), emitting the SAME C2 traces + tokens/$/latency;
3. scores BOTH variants with the UNCHANGED shared scorer — it reuses
   :func:`~credit_risk_monitoring.agent_managed.compare.score_arm` verbatim
   (Layer-1 ``build_suite`` + the Layer-2 groundedness judge + Braintrust
   fan-out), the exact surface baseline / Arm A / Arm B are already scored by;
4. renders the with/without delta table (per-fixture chain/depth/groundedness +
   per-variant branch-correct, grounded, tokens, $ cost, latency), writes it and a
   machine-readable ``experiment.json`` sidecar, and (default) archives the MA
   resources it created so the experiment leaves no orphaned agents.

Nothing here modifies the scorer, the fixtures, the shared packages, or the
Arm B production path — it composes existing pieces and adds the Skill variant.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anthropic

from credit_risk_monitoring.agent.orchestrator import build_clients
from credit_risk_monitoring.agent.tools import Clients
from credit_risk_monitoring.agent_managed.compare import ArmScore, score_arm
from credit_risk_monitoring.agent_managed.metrics import (
    load_metrics,
    run_arm_b_metrics,
)
from credit_risk_monitoring.agent_managed.orchestrator import ManagedOrchestrator
from credit_risk_monitoring.agent_managed.roster import ARM_B_MODEL, Roster, ensure_roster
from credit_risk_monitoring.agent_managed.skill import ensure_skill
from credit_risk_monitoring.fixtures import Fixture

logger = logging.getLogger(__name__)

# Variant label -> whether the Skill is attached. Report order is no-skill first
# (the baseline for this experiment) then skill (the treatment).
VARIANTS: tuple[str, ...] = ("no-skill", "skill")
_GROUNDED_PASS = 2  # groundedness rubric is 0-3; >= 2 captures the verified fact


@dataclass
class VariantResult:
    """One variant's scored results + run metrics over the fixture bank."""

    label: str
    score: ArmScore
    roster: Roster


# Sentinel roster for a variant scored from disk (reuse_existing) — its live IDs
# aren't known from traces, so experiment.json records blanks for it.
_DISK_ROSTER = Roster(
    environment_id="", coordinator_id="", exposure_id="", entity_resolution_id=""
)


def _variant_on_disk(trace_dir: Path, out_dir: Path) -> bool:
    """True when a variant already has traces + a metrics sidecar to score from."""
    return (out_dir / "metrics.json").exists() and any(trace_dir.glob("*.jsonl"))


def _make_env(client: Any, *, model: str) -> str:
    """One shared cloud environment for both variants (reused, not per-variant)."""
    env_id = os.environ.get("ARM_B_ENV_ID")
    if env_id:
        return env_id
    env = client.beta.environments.create(
        name=f"credit-risk-skill-exp-{os.getpid()}",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    return str(env.id)


def _archive_roster(client: Any, roster: Roster) -> None:
    """Best-effort archive of the agents this experiment created (no orphans)."""
    for agent_id in (roster.coordinator_id, roster.exposure_id, roster.entity_resolution_id):
        if not agent_id:
            continue
        try:
            client.beta.agents.archive(agent_id)
        except Exception as exc:  # noqa: BLE001 — cleanup must never fail the run
            logger.warning("skill-exp: archive agent %s failed: %s", agent_id, type(exc).__name__)


def run_skill_experiment(
    fixtures: list[Fixture],
    *,
    trace_root: Path,
    out_root: Path,
    variants: tuple[str, ...] = VARIANTS,
    model: str = ARM_B_MODEL,
    cleanup: bool = True,
    reuse_existing: bool = False,
    client: Any = None,
    clients: Clients | None = None,
) -> str:
    """Run + score both variants and return the rendered with/without markdown.

    ``reuse_existing``: a variant whose traces + metrics sidecar are already on
    disk is **scored from disk** rather than re-run — no roster is created for it
    and no live sessions are spent (mirrors ``compare --skip-run``). Used to
    resume after a partial run without paying for completed variants again.
    """
    client = client or anthropic.Anthropic()
    owns_clients = clients is None
    clients = clients or build_clients()
    env_id: str | None = None

    created_rosters: list[Roster] = []
    results: dict[str, VariantResult] = {}
    try:
        for label in variants:
            trace_dir = trace_root / label
            out_dir = out_root / label
            if reuse_existing and _variant_on_disk(trace_dir, out_dir):
                metrics = load_metrics(out_dir)
                score = score_arm(
                    f"arm-b-{label}", trace_dir=trace_dir, fixtures=fixtures, metrics=metrics
                )
                results[label] = VariantResult(label=label, score=score, roster=_DISK_ROSTER)
                continue
            use_skill = label == "skill"
            skill_id = ensure_skill(client) if use_skill else None
            if env_id is None:
                env_id = _make_env(client, model=model)
            roster = ensure_roster(
                client, model=model, environment_id=env_id, skill_id=skill_id
            )
            created_rosters.append(roster)
            orch = ManagedOrchestrator(
                client=client, clients=clients, roster=roster, use_skill=use_skill
            )
            metrics = run_arm_b_metrics(
                fixtures, trace_dir=trace_dir, out_dir=out_dir, orchestrator=orch
            )
            score = score_arm(
                f"arm-b-{label}", trace_dir=trace_dir, fixtures=fixtures, metrics=metrics
            )
            results[label] = VariantResult(label=label, score=score, roster=roster)
    finally:
        if cleanup:
            for roster in created_rosters:
                _archive_roster(client, roster)
        if owns_clients:
            clients.close()

    table = render_experiment_table(fixtures, results)
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "experiment.md").write_text(table, encoding="utf-8")
    (out_root / "experiment.json").write_text(
        json.dumps(_experiment_json(fixtures, results), indent=2), encoding="utf-8"
    )
    return table


# --- rendering --------------------------------------------------------------
def _cell(sc: ArmScore, qid: str) -> str:
    chain = "OK" if sc.chain_pass.get(qid) else "X"
    depth = "OK" if sc.depth_pass.get(qid) else "X"
    d = sc.depth_reached.get(qid, 0)
    g = sc.groundedness.get(qid)
    gnd = f"{g[0]}/{g[1]}" if g else "-"
    return f"{chain}/{depth}/{gnd} (d{d})"


def _totals(sc: ArmScore, fixtures: list[Fixture]) -> dict[str, Any]:
    n = len(fixtures)
    branch = sum(
        1 for fx in fixtures if sc.chain_pass.get(fx.id) and sc.depth_pass.get(fx.id)
    )
    grounded = sum(
        1 for fx in fixtures if (g := sc.groundedness.get(fx.id)) and g[0] >= _GROUNDED_PASS
    )
    in_tok = sum(
        m.input_tokens + m.cache_read_tokens + m.cache_write_tokens for m in sc.metrics.values()
    )
    out_tok = sum(m.output_tokens for m in sc.metrics.values())
    cost = sum(m.cost_usd for m in sc.metrics.values())
    wall = sum(m.latency_s for m in sc.metrics.values())
    return {
        "n": n,
        "branch": branch,
        "grounded": grounded,
        "grounded_scored": bool(sc.groundedness),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": cost,
        "wall_s": wall,
        "mean_latency_s": wall / n if n else 0.0,
    }


def render_experiment_table(fixtures: list[Fixture], results: dict[str, VariantResult]) -> str:
    """Per-fixture with/without table + per-variant totals + the measured delta."""
    labels = [lb for lb in VARIANTS if lb in results]
    lines: list[str] = [
        "# Skills experiment — Arm B with vs without the investigation-discipline Skill",
        "",
        "Same environment, tools, sub-agent prompts, and model in both variants — the "
        "attached Skill is the only difference. Each fixture cell is "
        "`chain/depth/groundedness (depth reached)`: `OK`/`X` for the UNCHANGED Layer-1 "
        "chain & depth checks, and the Layer-2 groundedness score `n/3` (`-` if the judge "
        "was skipped).",
        "",
    ]
    header = "| fixture | class | exp. depth | " + " | ".join(labels) + " |"
    sep = "| --- | --- | --- | " + " | ".join("---" for _ in labels) + " |"
    lines += [header, sep]
    for fx in fixtures:
        cells = " | ".join(_cell(results[lb].score, fx.id) for lb in labels)
        lines.append(f"| {fx.id} | {fx.classification} | {fx.expected_stop_depth} | {cells} |")

    lines += [
        "",
        "## Per-variant totals",
        "",
        "| variant | branch-correct (chain+depth) | grounded (>=2/3) | input tok | "
        "output tok | cost $ | wall-clock s | mean latency s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    tot = {lb: _totals(results[lb].score, fixtures) for lb in labels}
    for lb in labels:
        t = tot[lb]
        gnd = f"{t['grounded']}/{t['n']}" if t["grounded_scored"] else "n/a"
        lines.append(
            f"| {lb} | {t['branch']}/{t['n']} | {gnd} | {t['input_tokens']:,} | "
            f"{t['output_tokens']:,} | ${t['cost_usd']:.4f} | {t['wall_s']:.1f} | "
            f"{t['mean_latency_s']:.1f} |"
        )

    if "no-skill" in tot and "skill" in tot:
        a, b = tot["no-skill"], tot["skill"]
        d_branch = b["branch"] - a["branch"]
        d_grounded = b["grounded"] - a["grounded"]
        d_cost = b["cost_usd"] - a["cost_usd"]
        d_lat = b["mean_latency_s"] - a["mean_latency_s"]
        cost_pct = (100.0 * d_cost / a["cost_usd"]) if a["cost_usd"] else 0.0
        lines += [
            "",
            "## Measured delta (skill − no-skill)",
            "",
            f"- branch-correct: **{d_branch:+d}** ({a['branch']} -> {b['branch']} of {a['n']})",
            f"- grounded (>=2/3): **{d_grounded:+d}** ({a['grounded']} -> {b['grounded']})"
            if a["grounded_scored"] and b["grounded_scored"]
            else "- grounded: judge not run (no ANTHROPIC_API_KEY)",
            f"- cost: **${d_cost:+.4f}** ({cost_pct:+.1f}% — the Skill adds context = cost)",
            f"- mean latency: **{d_lat:+.1f}s**",
        ]
    return "\n".join(lines) + "\n"


def _experiment_json(
    fixtures: list[Fixture], results: dict[str, VariantResult]
) -> dict[str, Any]:
    return {
        "variants": {
            lb: {
                "roster": {
                    "environment_id": vr.roster.environment_id,
                    "coordinator_id": vr.roster.coordinator_id,
                    "exposure_id": vr.roster.exposure_id,
                    "entity_resolution_id": vr.roster.entity_resolution_id,
                    "skill_id": vr.roster.skill_id,
                    "model": vr.roster.model,
                },
                "per_fixture": {
                    fx.id: {
                        "chain_pass": vr.score.chain_pass.get(fx.id),
                        "depth_pass": vr.score.depth_pass.get(fx.id),
                        "depth_reached": vr.score.depth_reached.get(fx.id),
                        "chain": vr.score.chain.get(fx.id),
                        "groundedness": vr.score.groundedness.get(fx.id),
                        "metrics": asdict(vr.score.metrics[fx.id])
                        if fx.id in vr.score.metrics
                        else None,
                    }
                    for fx in fixtures
                },
                "totals": _totals(vr.score, fixtures),
            }
            for lb, vr in results.items()
        }
    }


__all__ = ["VARIANTS", "VariantResult", "render_experiment_table", "run_skill_experiment"]
