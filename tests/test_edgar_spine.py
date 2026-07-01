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
    large_submissions_payload,
    submissions_payload,
)

# The real Valaris 8-K the multi-hop fixtures target: a 2020 historical filing
# buried ~400 rows deep in the recent index.
_TARGET_ACCESSION = "0001104659-20-096796"


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


def test_historical_filing_hidden_from_unfiltered_render_but_found_by_filter() -> None:
    """Regression: a 2020 8-K buried ~400 rows deep is invisible to the newest-N
    screen (the bug that blocked every multi-hop fixture) but IS surfaced when the
    index is filtered by form type + date window."""
    transport = edgar_mock_transport(
        default_json=large_submissions_payload(target_index=432, target_date="2020-08-19")
    )
    with _client(transport) as edgar:
        idx = edgar.fetch_submissions_index("0000314808")

    assert len(idx.filings) == 500
    # The row is in the data structure...
    assert any(f.accession == _TARGET_ACCESSION for f in idx.filings)
    # ...but the unfiltered newest-25 render never shows it (this is the bug).
    assert _TARGET_ACCESSION not in idx.context_block()
    assert _TARGET_ACCESSION not in idx.context_block(limit=25)

    # The form+date filter surfaces it (the fix).
    matches = idx.matching(form_type="8-K", date_from="2020-01-01", date_to="2020-12-31")
    accs = {f.accession for f in matches}
    assert _TARGET_ACCESSION in accs
    block = idx.context_block(
        limit=50, form_type="8-K", date_from="2020-01-01", date_to="2020-12-31"
    )
    assert _TARGET_ACCESSION in block
    assert "2020-08-19" in block


def test_find_filings_locates_buried_historical_filing() -> None:
    """The addressing lookup edgar_filing uses: resolve a historical accession
    from form type + date window even when it is buried far below the newest set."""
    transport = edgar_mock_transport(
        default_json=large_submissions_payload(target_index=432, target_date="2020-08-19")
    )
    with _client(transport) as edgar:
        matches = edgar.find_filings(
            "0000314808", form_type="8-K", date_from="2020-08-19", date_to="2020-08-19"
        )
    assert len(matches) == 1
    assert matches[0].accession == _TARGET_ACCESSION
    assert matches[0].primary_document == "tm2027797d1_8k.htm"


def test_matching_form_filter_includes_amendments_and_respects_date_bounds() -> None:
    transport = edgar_mock_transport(
        default_json={
            "name": "Acme",
            "filings": {
                "recent": {
                    "form": ["8-K", "8-K/A", "10-K", "8-K"],
                    "filingDate": ["2021-03-01", "2020-06-15", "2020-02-01", "2019-12-31"],
                    "accessionNumber": ["a-21", "a-20a", "a-20k", "a-19"],
                    "primaryDocument": ["w.htm"] * 4,
                    "primaryDocDescription": [""] * 4,
                },
                "files": [],
            },
        }
    )
    with _client(transport) as edgar:
        idx = edgar.fetch_submissions_index("0000000001")

    # form "8-K" matches the plain form AND its "/A" amendment.
    eightks = {f.accession for f in idx.matching(form_type="8-K")}
    assert eightks == {"a-21", "a-20a", "a-19"}
    # date window is inclusive and chronological via ISO string comparison.
    in_2020 = {f.accession for f in idx.matching(date_from="2020-01-01", date_to="2020-12-31")}
    assert in_2020 == {"a-20a", "a-20k"}
    # combined form + date.
    combo = {f.accession for f in idx.matching(form_type="8-K", date_from="2020-01-01")}
    assert combo == {"a-21", "a-20a"}


def test_filtered_render_reports_overflow_when_matches_exceed_cap() -> None:
    transport = edgar_mock_transport(
        default_json=large_submissions_payload(count=500, target_index=432)
    )
    with _client(transport) as edgar:
        idx = edgar.fetch_submissions_index("0000314808")
    # Every 8-K across the whole span far exceeds the cap -> overflow hint shown.
    block = idx.context_block(limit=5, form_type="8-K")
    assert "more match(es) not shown" in block


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


def test_primary_pick_skips_sec_wrapper_files_when_type_is_uninformative() -> None:
    """Real EDGAR directories interleave SGML/index wrappers and XBRL-viewer files
    with the filing docs, and the ``type`` field is often just an icon name
    (``text.gif``) rather than the document type. The primary pick must skip the
    ``*-index-headers.html`` wrapper (which has no filing body) and the ``ex*`` /
    ``R\\d+`` artefacts, landing on the real 8-K body — otherwise an accession-only
    open reads the header dump and an investigation stalls."""
    acc = "0001104659-20-065674"
    index_json = {
        "directory": {
            "item": [
                {"name": f"{acc}-index-headers.html", "type": "text.gif", "description": ""},
                {"name": f"{acc}-index.html", "type": "text.gif", "description": ""},
                {"name": "R1.htm", "type": "text.gif", "description": ""},
                {"name": "tm2020858d1_8k.htm", "type": "text.gif", "description": "FORM 8-K"},
                {"name": "tm2020858d1_ex10-1.htm", "type": "text.gif", "description": "EXHIBIT 10.1"},
            ]
        }
    }
    transport = edgar_mock_transport(
        handlers={
            "/index.json": json_response(index_json),
            "tm2020858d1_8k.htm": html_response("<html><body><p>ITEM 1.03 BANKRUPTCY</p></body></html>"),
        }
    )
    with _client(transport) as edgar:
        directory, body = edgar.fetch_filing("0000047129", acc)
    assert directory.primary_document_url.endswith("tm2020858d1_8k.htm")
    assert "index-headers" not in directory.primary_document_url
    assert "BANKRUPTCY" in body.text


def test_fetch_filing_honors_primary_document_pin() -> None:
    """A primary_document that exists in the directory pins the body."""
    index_json = {
        "directory": {
            "item": [
                {"name": "big-10k.htm", "type": "10-K", "description": "10-K"},
                {"name": "tm-8k.htm", "type": "8-K", "description": "8-K body"},
            ]
        }
    }
    transport = edgar_mock_transport(
        handlers={
            "/index.json": json_response(index_json),
            "tm-8k.htm": html_response("<html><body><p>the 8-K body</p></body></html>"),
            "big-10k.htm": html_response("<html><body><p>the 10-K</p></body></html>"),
        }
    )
    with _client(transport) as edgar:
        _dir, body = edgar.fetch_filing(
            "0000000001", "0001104659-20-096796", primary_document="tm-8k.htm"
        )
    assert "the 8-K body" in body.text


def test_fetch_filing_falls_back_when_primary_document_not_in_directory() -> None:
    """A guessed/hallucinated primary_document that is NOT in the directory falls
    back to the directory's own primary pick instead of building a URL that 404s —
    so an investigation is not aborted by a wrong filename guess."""
    index_json = {
        "directory": {
            "item": [
                {"name": "tm-8k.htm", "type": "8-K", "description": "8-K body"},
                {"name": "ex10-1.htm", "type": "EX-10.1", "description": "RSA"},
            ]
        }
    }
    transport = edgar_mock_transport(
        handlers={
            "/index.json": json_response(index_json),
            "tm-8k.htm": html_response("<html><body><p>real 8-K body</p></body></html>"),
        }
    )
    with _client(transport) as edgar:
        # The model guessed a filename that does not exist in this accession.
        directory, body = edgar.fetch_filing(
            "0000000001", "0001104659-20-096796", primary_document="ea180000-8k_wrong.htm"
        )
    assert directory.primary_document_url.endswith("tm-8k.htm")
    assert "real 8-K body" in body.text  # fell back to the directory pick, no 404


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
