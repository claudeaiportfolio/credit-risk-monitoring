"""Arm B investigation-discipline **Skill** — author, register, attach (off by default).

This is a *measured experiment*, not a shipped default. The question the scoping
doc requires answering by measurement on THIS eval: does attaching a Managed
Agents Skill (progressive-disclosure investigation methodology) to the Arm B
roster improve investigation quality (branch-correctness + groundedness)?

The Skill lives in ``skills/investigation_discipline/SKILL.md`` — a progressive-
disclosure skill: its ``name`` + ``description`` frontmatter sits in the agent's
context by default, and the model reads the full body on demand when a task calls
for it. It encodes GENERAL credit-investigation discipline (follow the hop the
last source's content generates, open the on-path exhibit rather than guessing,
resolve a named/derived entity at the right registry incl. company-search, STOP
when the picture resolves, don't over- or under-investigate) — deliberately NOT
fixture answers (that would be gaming, and it would not generalise).

Wiring is behind the off-by-default ``ARM_B_USE_SKILL`` flag
(:class:`~credit_risk_monitoring.agent_managed.orchestrator.ManagedOrchestrator`
reads it): **Arm B defaults to NO Skill.** The Skill is kept in-repo as the
measured experiment; the keep/drop verdict is recorded in
``docs/skills-experiment.md``.

Creating the Skill is a **control-plane** step (create once, reference by ID) —
mirror the roster: :func:`ensure_skill` reuses ``ARM_B_SKILL_ID`` when present and
only uploads when it's missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# The progressive-disclosure skill directory (must contain SKILL.md at its root).
SKILL_DIR = Path(__file__).parent / "skills" / "investigation_discipline"
# Human-readable label (NOT sent to the model — the SKILL.md frontmatter is).
SKILL_DISPLAY_TITLE = "Credit investigation discipline"


def _skill_name() -> str:
    """The ``name`` from the SKILL.md YAML frontmatter — the Skills API requires the
    upload's top-level folder to match it exactly."""
    for line in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            return stripped.split(":", 1)[1].strip().strip("\"'")
    raise ValueError(f"{SKILL_DIR / 'SKILL.md'} has no 'name:' in its frontmatter")


def _skill_files() -> list[tuple[str, bytes, str]]:
    """Every file under the skill dir as ``(path, bytes, content_type)`` upload tuples.

    The Skills API requires all files inside ONE top-level folder, with ``SKILL.md``
    at that folder's root AND the folder name equal to the skill's ``name``
    frontmatter. So each upload path is ``<skill-name>/<rel>`` (e.g.
    ``credit-investigation-discipline/SKILL.md``) — deriving the folder from the
    frontmatter keeps the on-disk dir name independent of the API constraint. A bare
    ``SKILL.md`` (no folder) or a folder that doesn't match ``name`` is a 400.
    """
    folder = _skill_name()
    files: list[tuple[str, bytes, str]] = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(SKILL_DIR).as_posix()
        content_type = "text/markdown" if path.suffix == ".md" else "text/plain"
        files.append((f"{folder}/{rel}", path.read_bytes(), content_type))
    if not any(name.endswith("/SKILL.md") for name, _, _ in files):
        raise FileNotFoundError(f"skill dir {SKILL_DIR} has no SKILL.md at its root")
    return files


def ensure_skill(client: Any, *, skill_id: str | None = None) -> str:
    """Create (or reuse) the investigation-discipline Skill; return its ``skill_id``.

    Reuses ``skill_id`` (arg or ``ARM_B_SKILL_ID`` env) when supplied so the
    upload happens once and is referenced by ID thereafter. The SDK sets the
    ``skills-2025-10-02`` beta header automatically for ``client.beta.skills.*``.
    """
    skill_id = skill_id or os.environ.get("ARM_B_SKILL_ID")
    if skill_id:
        return skill_id
    # Find-or-create: skills.create rejects a duplicate display_title with a 400,
    # so reuse an already-uploaded Skill with the same title if one exists (makes
    # a re-run idempotent instead of crashing).
    for existing in client.beta.skills.list():
        if getattr(existing, "display_title", None) == SKILL_DISPLAY_TITLE:
            return str(existing.id)
    resp = client.beta.skills.create(
        display_title=SKILL_DISPLAY_TITLE,
        files=_skill_files(),
    )
    return str(resp.id)


def skill_reference(skill_id: str, *, version: str = "latest") -> dict[str, Any]:
    """The agent-side reference for a custom Skill (goes in ``agents.create(skills=[...])``)."""
    return {"type": "custom", "skill_id": skill_id, "version": version}


def env_use_skill() -> bool:
    """Whether ``ARM_B_USE_SKILL`` opts Arm B into the Skill (default: OFF)."""
    return os.environ.get("ARM_B_USE_SKILL", "").strip().lower() in ("1", "true", "yes", "on")


__all__ = [
    "SKILL_DIR",
    "SKILL_DISPLAY_TITLE",
    "ensure_skill",
    "env_use_skill",
    "skill_reference",
]
