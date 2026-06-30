"""Audit log of every agent/tool action — who, what, when, revoked.

Every authorization decision, every tool execution, and every admin
revoke/deny is recorded as an :class:`AuditEvent`. The sink is pluggable:

* :class:`InMemoryAuditSink` — the default; used by tests and credential-less
  runs. Keeps events in a list for inspection.
* :class:`PostgresAuditSink` — production. Connects lazily (so importing this
  module never needs a database), creates the table if absent, and inserts one
  row per event. The connection string comes from ``os.environ`` only
  (``AUDIT_DATABASE_URL``); in production that is injected from Key Vault via
  workload identity at deploy time — never committed.

``make_audit_sink_from_env`` picks the Postgres sink when a DSN is configured
*and* the driver is installed, otherwise falls back to in-memory with a logged
note. The build is never blocked on a live database.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

AuditAction = Literal["authorize", "execute", "revoke_agent", "deny_tool", "mint"]


@dataclass(frozen=True)
class AuditEvent:
    """One audited action. ``who`` = ``agent_id``, ``what`` = ``action``/``tool``,
    ``when`` = ``ts`` (epoch seconds), ``revoked`` = blocked by a revoke/deny."""

    agent_id: str
    action: AuditAction
    tool: str = ""
    allowed: bool = True
    revoked: bool = False
    reason: str = ""
    detail: str = ""
    ts: float = field(default_factory=time.time)

    def as_row(self) -> dict[str, object]:
        return asdict(self)


@runtime_checkable
class AuditSink(Protocol):
    """Where audit events are persisted."""

    def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    """Default sink — keeps events in memory for inspection/tests.

    Optionally mirrors to stderr as JSON lines so a credential-less run still
    leaves a readable audit trail.
    """

    def __init__(self, *, mirror_to_stderr: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self._mirror = mirror_to_stderr

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)
        if self._mirror:
            print("AUDIT " + json.dumps(event.as_row(), default=str), file=sys.stderr)

    # Convenience accessors for assertions/reporting.
    def for_agent(self, agent_id: str) -> list[AuditEvent]:
        return [e for e in self.events if e.agent_id == agent_id]

    def denials(self) -> list[AuditEvent]:
        return [e for e in self.events if not e.allowed]


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    agent_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    tool        TEXT NOT NULL DEFAULT '',
    allowed     BOOLEAN NOT NULL DEFAULT TRUE,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    reason      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS agent_audit_log_agent_ts ON agent_audit_log (agent_id, ts);
"""

_INSERT = """
INSERT INTO agent_audit_log (ts, agent_id, action, tool, allowed, revoked, reason, detail)
VALUES (to_timestamp(%(ts)s), %(agent_id)s, %(action)s, %(tool)s,
        %(allowed)s, %(revoked)s, %(reason)s, %(detail)s)
"""


class PostgresAuditSink:
    """Append-only Postgres sink. Lazily connects; creates its table on first use.

    Uses ``psycopg`` (v3). Construction does **not** open a connection — the DSN
    is validated and the connection is established on the first ``record`` call
    — so importing/constructing this in a context without a DB never fails. A
    write failure is logged (not raised): an audit-log outage must not take down
    the investigation, but it must be visible.
    """

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgresAuditSink requires a non-empty DSN")
        self._dsn = dsn
        self._conn: object | None = None
        self._initialised = False

    def _connect(self) -> object:
        import psycopg  # imported lazily so the driver is only needed in prod

        conn = psycopg.connect(self._dsn, autocommit=True)
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(_CREATE_TABLE)
        return conn

    def record(self, event: AuditEvent) -> None:
        try:
            if self._conn is None:
                self._conn = self._connect()
                self._initialised = True
            with self._conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(_INSERT, event.as_row())
        except Exception as exc:  # noqa: BLE001 — never let auditing crash the run
            # Log ONLY the exception type, never the exception text: a psycopg
            # connection error can render the full DSN (incl. password) in its
            # message, which must not reach the log sink.
            logger.error(
                "audit-log write failed (event=%s): %s", event.action, type(exc).__name__
            )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()  # type: ignore[attr-defined]
            finally:
                self._conn = None


def make_audit_sink_from_env() -> AuditSink:
    """Pick the audit sink from the environment.

    ``AUDIT_DATABASE_URL`` set + ``psycopg`` importable -> Postgres; otherwise an
    in-memory sink (mirrored to stderr) with a logged note. Never raises.
    """
    dsn = os.environ.get("AUDIT_DATABASE_URL")
    if dsn:
        try:
            import psycopg  # noqa: F401

            return PostgresAuditSink(dsn)
        except ImportError:
            logger.warning(
                "AUDIT_DATABASE_URL is set but psycopg is not installed "
                "(install the 'audit' extra); falling back to in-memory audit."
            )
    return InMemoryAuditSink(mirror_to_stderr=True)
