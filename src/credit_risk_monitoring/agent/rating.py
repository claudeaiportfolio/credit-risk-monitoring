"""External credit-rating lookup — the rating-agency hop.

One fixture's chain ends at a rating-agency action (an S&P downgrade to 'D'
after a default). Rating-agency press releases are paywalled (the fixtures note
the S&P body returns 403), so this is the one place the *demonstration* corpus
is simplified — but the **production path is real**: the client issues an
authenticated request to a configured ratings data provider
(``RATING_API_BASE`` + ``RATING_API_KEY`` from the environment), rate-limited
and retried like the other spines.

When no rating provider is configured the client returns a clearly-labelled
``configured=False`` result rather than fabricating a rating — the agent then
reports the rating as unverified, which is the honest grounded behaviour. The
hop is still emitted (so the chain/depth scoring is exercised); only the
answer's rating value is gated on a real provider being wired in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from credit_risk_monitoring.sources import RateLimiter, request_with_retry

_DEFAULT_RATING_INTERVAL = float(os.environ.get("RATING_MIN_INTERVAL", "0.2"))


def _rating_base() -> str | None:
    base = os.environ.get("RATING_API_BASE")
    return base.rstrip("/") if base else None


def _rating_key() -> str | None:
    return os.environ.get("RATING_API_KEY")


@dataclass(frozen=True)
class RatingResult:
    subject: str
    rating: str
    action: str
    as_of: str
    agency: str
    configured: bool
    source_locator: str

    def context_block(self) -> str:
        if not self.configured:
            return (
                f"Rating lookup for {self.subject!r}: NO rating provider configured "
                "(RATING_API_BASE/RATING_API_KEY unset). Treat the rating as UNVERIFIED — "
                "report it as such; do not assert a rating value."
            )
        return (
            f"Rating for {self.subject!r} ({self.agency}):\n"
            f"- rating: {self.rating}\n- action: {self.action}\n- as of: {self.as_of}\n"
            f"- source: {self.source_locator}"
        )


class RatingClient:
    """Authenticated rating-provider client (degrades cleanly when unconfigured)."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        *,
        limiter: RateLimiter | None = None,
        max_retries: int = 3,
    ) -> None:
        self._explicit_client = client
        self._client = client
        self._timeout = timeout
        self._limiter = limiter or RateLimiter(_DEFAULT_RATING_INTERVAL)
        self._max_retries = max_retries

    def _ensure_client(self, base: str, key: str) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=base,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    def get_rating(self, subject: str) -> RatingResult:
        base, key = _rating_base(), _rating_key()
        if not base or not key:
            return RatingResult(
                subject=subject,
                rating="",
                action="",
                as_of="",
                agency="",
                configured=False,
                source_locator="(no rating provider configured)",
            )
        client = self._ensure_client(base, key)
        resp = request_with_retry(
            client,
            "GET",
            "/ratings",
            limiter=self._limiter,
            max_retries=self._max_retries,
            params={"entity": subject},
        )
        data = resp.json()
        return RatingResult(
            subject=subject,
            rating=str(data.get("rating", "")),
            action=str(data.get("action", "")),
            as_of=str(data.get("as_of", "")),
            agency=str(data.get("agency", "")),
            configured=True,
            source_locator=str(data.get("source", base)),
        )

    def close(self) -> None:
        if self._client is not None and self._explicit_client is None:
            self._client.close()
            self._client = None
