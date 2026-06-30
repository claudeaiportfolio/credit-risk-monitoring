"""EDGAR production spine — directory/body/exhibit fetch, pagination, retries,
rate limiting, unhappy paths. All offline via httpx.MockTransport."""

from __future__ import annotations

import time

import httpx
import pytest

from credit_risk_monitoring.sources import (
    EdgarClient,
    RateLimiter,
    html_to_text,
    request_with_retry,
)
from tests.conftest import (
    edgar_mock_transport,
    html_response,
    json_response,
    submissions_payload,
)


def _client(transport: httpx.MockTransport) -> EdgarClient:
    return EdgarClient(
        client=httpx.Client(transport=transport, follow_redirects=True),
        limiter=RateLimiter(0.0),  # no real sleeping in tests
    )


def test_submissions_index_parses_recent() -> None:
    transport = edgar_mock_transport(default_json=submissions_payload(name="Acme Corp"))
    with _client(transport) as edgar:
        idx = edgar.fetch_submissions_index("0000000001")
    assert idx.issuer_name == "Acme Corp"
    assert len(idx.filings) == 3
    assert idx.filings[0].form == "8-K"
    assert "Acme Corp" in idx.context_block()


def test_submissions_index_pagination_merges_files() -> None:
    base = submissions_payload(name="Acme", forms=["8-K"])
    base["filings"]["files"] = [{"name": "CIK0000000001-submissions-001.json"}]
    older = {
        "form": ["10-K", "8-K"],
        "filingDate": ["2018-01-01", "2018-02-01"],
        "accessionNumber": ["0001-18-1", "0001-18-2"],
        "primaryDocument": ["a.htm", "b.htm"],
        "primaryDocDescription": ["", ""],
    }
    transport = edgar_mock_transport(
        handlers={"submissions-001.json": json_response(older)},
        default_json=base,
    )
    with _client(transport) as edgar:
        merged = edgar.fetch_submissions_index("0000000001", paginate=True)
        recent_only = edgar.fetch_submissions_index("0000000001", paginate=False)
    assert len(recent_only.filings) == 1
    assert len(merged.filings) == 3  # 1 recent + 2 paginated


def test_filing_directory_and_body() -> None:
    index_json = {
        "directory": {
            "item": [
                {"name": "tm-8k.htm", "type": "8-K", "description": "8-K body"},
                {"name": "ex10-1.htm", "type": "EX-10.1", "description": "RSA"},
                {"name": "index.json", "type": "", "description": ""},
            ]
        }
    }
    transport = edgar_mock_transport(
        handlers={
            "/index.json": json_response(index_json),
            "tm-8k.htm": html_response("<html><body><p>Entry into RSA.</p></body></html>"),
        }
    )
    with _client(transport) as edgar:
        directory, body = edgar.fetch_filing("0000000001", "0001104659-20-096796")
    assert directory.primary_document_url.endswith("tm-8k.htm")
    assert len(directory.exhibits()) == 1
    assert "Entry into RSA." in body.text


def test_fetch_exhibit_strips_html() -> None:
    transport = edgar_mock_transport(
        handlers={"ex10-1.htm": html_response("<div>Ensco Global Resources Ltd</div>")}
    )
    with _client(transport) as edgar:
        doc = edgar.fetch_exhibit("https://www.sec.gov/Archives/x/ex10-1.htm")
    assert "Ensco Global Resources Ltd" in doc.text
    assert "<div>" not in doc.text


def test_unknown_cik_raises() -> None:
    transport = edgar_mock_transport()  # everything 404s
    with _client(transport) as edgar, pytest.raises(httpx.HTTPStatusError):
        edgar.fetch_submissions_index("0000000099")


def test_request_with_retry_recovers_from_429() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    resp = request_with_retry(
        client, "GET", "https://data.sec.gov/x", limiter=RateLimiter(0.0), backoff_base=0.0
    )
    assert resp.json() == {"ok": True}
    assert calls["n"] == 2  # one retry


def test_request_with_retry_gives_up_and_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(
            client,
            "GET",
            "https://data.sec.gov/x",
            limiter=RateLimiter(0.0),
            max_retries=2,
            backoff_base=0.0,
        )


def test_rate_limiter_spaces_calls() -> None:
    limiter = RateLimiter(0.05)
    start = time.monotonic()
    limiter.acquire()  # first is immediate
    limiter.acquire()  # second waits ~0.05s
    limiter.acquire()  # third waits ~0.05s
    elapsed = time.monotonic() - start
    assert elapsed >= 0.09


def test_html_to_text_collapses_and_unescapes() -> None:
    out = html_to_text("<p>Hertz&nbsp;UK</p><script>x()</script><p>Receivables</p>")
    assert "Hertz UK" in out
    assert "Receivables" in out
    assert "x()" not in out  # script body removed
