"""Runtime authorization + kill-switch for sub-agent tool use.

Each sub-agent is issued a short-TTL :class:`ToolToken` scoped to exactly the
tools its role needs (least privilege). Every tool call is checked at the auth
boundary (``agent.tools.ToolRouter``) against the broker — so a revoke takes
effect **mid-workflow**, on the very next call, not just at startup. The broker
supports a one-call admin revoke (by agent) and a global tool deny-list (kill a
tool everywhere), and writes every decision/revoke to the audit log.

The check order is deliberate: agent-revoked and token-revoked are tested
before expiry and scope, so an admin kill always wins regardless of token
state. The deny-list is checked on *every* call (not cached) so adding a tool
to it refuses all in-flight agents immediately.

This is a self-contained authority broker, not Auth0 (agent-core's
``Auth0M2MClient`` authenticates the agent process to an MCP server; this
governs *which local tool* an already-authenticated sub-agent may invoke, at
sub-second TTLs the M2M layer is not meant for).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from credit_risk_monitoring.agent.audit import AuditEvent, AuditSink, InMemoryAuditSink

# Default per-sub-agent token lifetime. Short by design: a sub-agent's tools are
# only needed for the seconds its phase runs, so a leaked/replayed token expires
# almost immediately. Env-tunable by the caller; kept generous enough for a real
# multi-hop EDGAR/CH phase with retries.
DEFAULT_TOKEN_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class ToolToken:
    """A short-TTL capability: which agent may call which tools, until when."""

    agent_id: str
    scopes: frozenset[str]
    issued_at: float
    ttl: float
    nonce: str

    def expired(self, now: float) -> bool:
        return now > self.issued_at + self.ttl

    def in_scope(self, tool: str) -> bool:
        return tool in self.scopes


@dataclass(frozen=True)
class Decision:
    """The outcome of an authorization check."""

    allowed: bool
    reason: str
    revoked: bool = False  # True when refused by an admin revoke/deny (vs. expiry/scope)


class AuthorityBroker:
    """Mints tool tokens and authorizes/denies tool calls; the kill-switch.

    Thread-unaware by design (the orchestrator drives phases sequentially); the
    revoked/deny sets are plain in-memory state representing the live security
    posture. Wire a durable store behind ``AuditSink`` for forensic history;
    the *current* posture (who's revoked, what's denied) is process-local and
    intentionally fast.
    """

    def __init__(self, audit: AuditSink | None = None) -> None:
        self._audit: AuditSink = audit or InMemoryAuditSink()
        self._revoked_agents: set[str] = set()
        self._revoked_nonces: set[str] = set()
        self._deny_list: set[str] = set()

    @property
    def audit(self) -> AuditSink:
        return self._audit

    # -- minting -----------------------------------------------------------
    def mint(
        self, agent_id: str, scopes: set[str] | frozenset[str], *, ttl: float = DEFAULT_TOKEN_TTL_SECONDS
    ) -> ToolToken:
        token = ToolToken(
            agent_id=agent_id,
            scopes=frozenset(scopes),
            issued_at=time.monotonic(),
            ttl=ttl,
            nonce=secrets.token_hex(8),
        )
        self._audit.record(
            AuditEvent(
                agent_id=agent_id,
                action="mint",
                detail=f"scopes={sorted(token.scopes)} ttl={ttl}s",
            )
        )
        return token

    # -- the auth boundary -------------------------------------------------
    def authorize(self, token: ToolToken, tool: str, *, now: float | None = None) -> Decision:
        """Check one tool call. Records the decision to the audit log.

        Order: admin revoke (agent, then token) -> deny-list -> expiry ->
        scope. Admin kills win over everything so a revoked agent cannot ride an
        unexpired token.
        """
        now = time.monotonic() if now is None else now
        decision = self._evaluate(token, tool, now)
        self._audit.record(
            AuditEvent(
                agent_id=token.agent_id,
                action="authorize",
                tool=tool,
                allowed=decision.allowed,
                revoked=decision.revoked,
                reason=decision.reason,
            )
        )
        return decision

    def _evaluate(self, token: ToolToken, tool: str, now: float) -> Decision:
        if token.agent_id in self._revoked_agents:
            return Decision(False, f"agent {token.agent_id!r} has been revoked", revoked=True)
        if token.nonce in self._revoked_nonces:
            return Decision(False, "token has been revoked", revoked=True)
        if tool in self._deny_list:
            return Decision(False, f"tool {tool!r} is on the deny-list", revoked=True)
        if token.expired(now):
            return Decision(False, "token expired")
        if not token.in_scope(tool):
            return Decision(False, f"tool {tool!r} not in token scope {sorted(token.scopes)}")
        return Decision(True, "authorized")

    # -- admin kill-switch -------------------------------------------------
    def revoke_agent(self, agent_id: str) -> None:
        """One-call admin revoke: the agent's next tool call is refused."""
        self._revoked_agents.add(agent_id)
        self._audit.record(
            AuditEvent(agent_id=agent_id, action="revoke_agent", revoked=True, reason="admin revoke")
        )

    def revoke_token(self, token: ToolToken) -> None:
        self._revoked_nonces.add(token.nonce)
        self._audit.record(
            AuditEvent(
                agent_id=token.agent_id, action="revoke_agent", revoked=True, reason="token revoke"
            )
        )

    def deny_tool(self, tool: str) -> None:
        """Kill a tool globally: every agent's calls to it are refused next."""
        self._deny_list.add(tool)
        self._audit.record(
            AuditEvent(agent_id="*", action="deny_tool", tool=tool, revoked=True, reason="admin deny")
        )

    def allow_tool(self, tool: str) -> None:
        self._deny_list.discard(tool)

    # -- introspection -----------------------------------------------------
    def is_agent_revoked(self, agent_id: str) -> bool:
        return agent_id in self._revoked_agents

    @property
    def deny_list(self) -> frozenset[str]:
        return frozenset(self._deny_list)


@dataclass
class AuthContext:
    """Everything a sub-agent needs to act under authority: its id + token."""

    agent_id: str
    token: ToolToken
    broker: AuthorityBroker
    scopes: frozenset[str] = field(default_factory=frozenset)
