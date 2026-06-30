"""Audit log: event shape, in-memory sink, and the env-driven sink selection
(Postgres in prod; in-memory fallback — never blocking the build)."""

from __future__ import annotations

import pytest

from credit_risk_monitoring.agent.audit import (
    AuditEvent,
    InMemoryAuditSink,
    PostgresAuditSink,
    make_audit_sink_from_env,
)


def test_audit_event_row_has_who_what_when_revoked() -> None:
    ev = AuditEvent(agent_id="exposure", action="execute", tool="edgar_filing", revoked=False)
    row = ev.as_row()
    assert row["agent_id"] == "exposure"  # who
    assert row["action"] == "execute" and row["tool"] == "edgar_filing"  # what
    assert isinstance(row["ts"], float) and row["ts"] > 0  # when
    assert row["revoked"] is False


def test_in_memory_sink_records_and_filters() -> None:
    sink = InMemoryAuditSink()
    sink.record(AuditEvent(agent_id="a", action="authorize", allowed=True))
    sink.record(AuditEvent(agent_id="a", action="authorize", allowed=False))
    sink.record(AuditEvent(agent_id="b", action="execute"))
    assert len(sink.events) == 3
    assert len(sink.for_agent("a")) == 2
    assert len(sink.denials()) == 1


def test_make_sink_falls_back_to_in_memory_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUDIT_DATABASE_URL", raising=False)
    sink = make_audit_sink_from_env()
    assert isinstance(sink, InMemoryAuditSink)


def test_postgres_sink_requires_dsn() -> None:
    with pytest.raises(ValueError, match="non-empty DSN"):
        PostgresAuditSink("")


def test_postgres_sink_construction_does_not_connect() -> None:
    # Construction must not open a connection (so it's safe to build without a DB).
    sink = PostgresAuditSink("postgresql://invalid:5432/nope")
    assert sink is not None  # no exception raised at construction time
