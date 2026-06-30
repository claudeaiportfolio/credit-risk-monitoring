"""Companies House client — Basic auth, pagination, parsing, unhappy paths.
Offline via httpx.MockTransport."""

from __future__ import annotations

import base64

import httpx
import pytest

from credit_risk_monitoring.agent.companies_house import CompaniesHouseClient
from credit_risk_monitoring.sources import RateLimiter

_BASE = "https://api.company-information.service.gov.uk"


def _client(transport: httpx.MockTransport, *, key: str = "test-key") -> CompaniesHouseClient:
    inner = httpx.Client(
        transport=transport, base_url=_BASE, auth=httpx.BasicAuth(key, "")
    )
    return CompaniesHouseClient(client=inner, limiter=RateLimiter(0.0))


def test_basic_auth_sends_key_as_username_blank_password() -> None:
    captured: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"company_number": "07098531", "company_name": "X"})

    with _client(httpx.MockTransport(handle), key="my-secret") as ch:
        ch.get_company("07098531")
    scheme, _, token = captured["auth"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == "my-secret:"  # key as user, blank password


def test_get_company_parses_profile() -> None:
    payload = {
        "company_number": "07098531",
        "company_name": "Ensco Global Resources Limited",
        "company_status": "dissolved",
        "type": "ltd",
        "date_of_creation": "2009-12-01",
    }
    with _client(httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as ch:
        profile = ch.get_company("07098531")
    assert profile.status == "dissolved"
    assert "Ensco Global Resources Limited" in profile.context_block()


def test_get_charges_paginates_and_parses_holders() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start_index", "0"))
        if start == 0:
            items = [
                {
                    "charge_code": f"07098531000{i}",
                    "status": "outstanding",
                    "created_on": "2020-09-25",
                    "classification": {"description": "A registered charge"},
                    "persons_entitled": [{"name": "Credit Agricole CIB"}],
                }
                for i in range(100)
            ]
        else:
            items = [
                {
                    "charge_code": "0709853109999",
                    "status": "satisfied",
                    "created_on": "2010-01-01",
                    "classification": {"description": "A registered charge"},
                    "persons_entitled": [{"name": "Lombard North Central"}],
                }
            ]
        return httpx.Response(200, json={"items": items, "total_results": 101})

    with _client(httpx.MockTransport(handle)) as ch:
        charges = ch.get_charges("07098531")
    assert len(charges) == 101  # both pages followed
    assert charges[0].persons_entitled == ("Credit Agricole CIB",)
    assert "Lombard North Central" in charges[-1].summary_line()


def test_search_companies() -> None:
    payload = {
        "items": [
            {
                "company_name": "Wejo Limited",
                "company_number": "08813730",
                "company_status": "administration",
            }
        ]
    }
    with _client(httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as ch:
        items = ch.search_companies("Wejo Limited")
    assert items[0]["company_number"] == "08813730"


def test_unknown_company_raises() -> None:
    with _client(httpx.MockTransport(lambda r: httpx.Response(404, json={}))) as ch, pytest.raises(
        httpx.HTTPStatusError
    ):
        ch.get_company("00000000")


def test_charges_retry_on_429() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
        return httpx.Response(200, json={"items": [], "total_results": 0})

    with _client(httpx.MockTransport(handle)) as ch:
        charges = ch.get_charges("07098531")
    assert charges == []
    assert calls["n"] == 2


def test_missing_api_key_raises_without_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="COMPANIES_HOUSE_API_KEY"):
        CompaniesHouseClient()
