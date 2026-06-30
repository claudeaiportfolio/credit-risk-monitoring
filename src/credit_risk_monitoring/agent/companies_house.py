"""UK Companies House client — the foreign-registry hop of the chain.

Companies House is where a UK subsidiary named in an SEC exhibit resolves: its
status, officers, charges (the security a credit investigation is chasing), and
filing history. This is a production client:

* **Auth** — HTTP Basic with the API key as the username and a blank password,
  per the Companies House REST spec. The key is read from
  ``os.environ["COMPANIES_HOUSE_API_KEY"]`` only — never committed, never a
  parameter default.
* **Rate limit** — 600 requests / 5 minutes (one request every ~0.5s). A shared
  :class:`~credit_risk_monitoring.sources.RateLimiter` enforces it.
* **Pagination** — officers / charges / filing-history are paged
  (``items_per_page`` + ``start_index`` against ``total_results``); the list
  helpers follow every page.
* **Unhappy paths** — 404 (unknown company) and 401 (bad/missing key) raise
  ``httpx.HTTPStatusError``; 429/5xx are retried with backoff (honouring
  ``Retry-After``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from credit_risk_monitoring.sources import RateLimiter, request_with_retry

_DEFAULT_BASE = "https://api.company-information.service.gov.uk"

# 600 requests / 5 minutes = 1 / 0.5s. Small headroom keeps us under the ceiling.
_DEFAULT_CH_INTERVAL = float(os.environ.get("COMPANIES_HOUSE_MIN_INTERVAL", "0.5"))

# Companies House caps page size at 100 for list endpoints.
_MAX_PER_PAGE = 100


def _base_url() -> str:
    return os.environ.get("COMPANIES_HOUSE_API_BASE", _DEFAULT_BASE).rstrip("/")


def _api_key() -> str:
    key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not key:
        raise RuntimeError(
            "COMPANIES_HOUSE_API_KEY is not set — the Companies House hop requires "
            "a key (supplied via the invocation surface / Key Vault, never committed)."
        )
    return key


@dataclass(frozen=True)
class CompanyProfile:
    number: str
    name: str
    status: str
    company_type: str
    incorporated_on: str
    raw: dict = field(default_factory=dict)

    @property
    def locator(self) -> str:
        return f"CH {self.number} ({self.name})"

    def context_block(self) -> str:
        return (
            f"Companies House profile {self.locator}:\n"
            f"- status: {self.status}\n"
            f"- type: {self.company_type}\n"
            f"- incorporated: {self.incorporated_on}"
        )


@dataclass(frozen=True)
class Charge:
    charge_id: str
    status: str
    created_on: str
    classification: str
    persons_entitled: tuple[str, ...]
    raw: dict = field(default_factory=dict)

    def summary_line(self) -> str:
        holders = ", ".join(self.persons_entitled) or "(persons-entitled not listed)"
        return f"{self.charge_id or '?'}  {self.status}  created {self.created_on}  [{self.classification}]  -> {holders}"


class CompaniesHouseClient:
    """Synchronous Companies House REST client (Basic-auth, rate-limited)."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        *,
        limiter: RateLimiter | None = None,
        max_retries: int = 4,
    ) -> None:
        # httpx.BasicAuth sends the key as username with a blank password.
        self._client = client or httpx.Client(
            auth=httpx.BasicAuth(_api_key(), ""),
            base_url=_base_url(),
            timeout=timeout,
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter(_DEFAULT_CH_INTERVAL)
        self._max_retries = max_retries

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return request_with_retry(
            self._client,
            "GET",
            path,
            limiter=self._limiter,
            max_retries=self._max_retries,
            params=params,
        )

    # -- resolve a name -> company number ----------------------------------
    def search_companies(self, query: str, *, items: int = 20) -> list[dict]:
        """Search the register by name; returns the raw ``items`` list.

        Used to resolve a subsidiary named in an exhibit to its registered
        number when the number isn't already known.
        """
        data = self._get(
            "/search/companies", params={"q": query, "items_per_page": min(items, _MAX_PER_PAGE)}
        ).json()
        return list(data.get("items", []) or [])

    # -- the company record ------------------------------------------------
    def get_company(self, number: str) -> CompanyProfile:
        data = self._get(f"/company/{number}").json()
        return CompanyProfile(
            number=str(data.get("company_number", number)),
            name=str(data.get("company_name", "")),
            status=str(data.get("company_status", "")),
            company_type=str(data.get("type", "")),
            incorporated_on=str(data.get("date_of_creation", "")),
            raw=data,
        )

    # -- paginated lists ---------------------------------------------------
    def _paginate(self, path: str) -> list[dict]:
        """Follow every page of a list endpoint, returning all ``items``."""
        items: list[dict] = []
        start = 0
        while True:
            data = self._get(
                path, params={"items_per_page": _MAX_PER_PAGE, "start_index": start}
            ).json()
            page = list(data.get("items", []) or [])
            items.extend(page)
            total = int(data.get("total_results", data.get("total_count", len(items)) or len(items)))
            start += len(page)
            if not page or start >= total or len(page) < _MAX_PER_PAGE:
                break
        return items

    def get_officers(self, number: str) -> list[dict]:
        return self._paginate(f"/company/{number}/officers")

    def get_filing_history(self, number: str) -> list[dict]:
        return self._paginate(f"/company/{number}/filing-history")

    def get_charges(self, number: str) -> list[Charge]:
        charges: list[Charge] = []
        for raw in self._paginate(f"/company/{number}/charges"):
            persons = tuple(
                str(p.get("name", "")).strip()
                for p in (raw.get("persons_entitled") or [])
                if p.get("name")
            )
            charges.append(
                Charge(
                    charge_id=str(raw.get("charge_code") or raw.get("id") or ""),
                    status=str(raw.get("status", "")),
                    created_on=str(raw.get("created_on", "")),
                    classification=str(
                        (raw.get("classification") or {}).get("description", "")
                    ),
                    persons_entitled=persons,
                    raw=raw,
                )
            )
        return charges

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CompaniesHouseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
