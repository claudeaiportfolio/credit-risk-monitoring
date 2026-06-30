"""Runtime authz / kill-switch: scope, TTL, deny-list, admin revoke — all
checked at the boundary on every call, and every decision audited."""

from __future__ import annotations

import time

from credit_risk_monitoring.agent.audit import InMemoryAuditSink
from credit_risk_monitoring.agent.authz import AuthorityBroker

EDGAR = "edgar_filing"
CH = "companies_house"


def _broker() -> tuple[AuthorityBroker, InMemoryAuditSink]:
    sink = InMemoryAuditSink()
    return AuthorityBroker(audit=sink), sink


def test_in_scope_allowed_out_of_scope_denied() -> None:
    broker, _ = _broker()
    token = broker.mint("exposure", {EDGAR})
    assert broker.authorize(token, EDGAR).allowed is True
    denied = broker.authorize(token, CH)
    assert denied.allowed is False
    assert "not in token scope" in denied.reason


def test_expired_token_denied() -> None:
    broker, _ = _broker()
    token = broker.mint("exposure", {EDGAR}, ttl=10.0)
    future = time.monotonic() + 11.0
    d = broker.authorize(token, EDGAR, now=future)
    assert d.allowed is False
    assert "expired" in d.reason
    assert d.revoked is False  # expiry is not an admin kill


def test_deny_list_refuses_mid_workflow() -> None:
    broker, _ = _broker()
    token = broker.mint("exposure", {EDGAR})
    assert broker.authorize(token, EDGAR).allowed is True  # works before
    broker.deny_tool(EDGAR)  # admin kills the tool mid-run
    after = broker.authorize(token, EDGAR)  # same unexpired token
    assert after.allowed is False
    assert after.revoked is True
    assert "deny-list" in after.reason


def test_admin_revoke_agent_refuses_next_call() -> None:
    broker, _ = _broker()
    token = broker.mint("entity_resolution", {CH})
    assert broker.authorize(token, CH).allowed is True
    broker.revoke_agent("entity_resolution")  # one-call admin revoke
    after = broker.authorize(token, CH)
    assert after.allowed is False
    assert after.revoked is True
    assert broker.is_agent_revoked("entity_resolution")


def test_revoke_token_refuses() -> None:
    broker, _ = _broker()
    token = broker.mint("exposure", {EDGAR})
    broker.revoke_token(token)
    assert broker.authorize(token, EDGAR).allowed is False


def test_revoke_wins_over_valid_unexpired_token() -> None:
    # Order guarantee: admin kill beats a token that is otherwise in-scope+fresh.
    broker, _ = _broker()
    token = broker.mint("exposure", {EDGAR}, ttl=999.0)
    broker.revoke_agent("exposure")
    assert broker.authorize(token, EDGAR).allowed is False


def test_every_decision_and_revoke_audited() -> None:
    broker, sink = _broker()
    token = broker.mint("exposure", {EDGAR})
    broker.authorize(token, EDGAR)  # allow
    broker.authorize(token, CH)  # deny (scope)
    broker.deny_tool(EDGAR)  # admin deny
    actions = [e.action for e in sink.events]
    assert "mint" in actions
    assert actions.count("authorize") == 2
    assert "deny_tool" in actions
    # who/what/when present on an authorize event.
    auth_events = [e for e in sink.events if e.action == "authorize"]
    assert all(e.agent_id == "exposure" and e.ts > 0 for e in auth_events)
    assert any(not e.allowed for e in auth_events)  # the scope denial recorded
