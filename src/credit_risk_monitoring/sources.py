"""Source taxonomy + the minimal retrieval helpers the eval depends on.

A credit-deterioration investigation walks a *chain* of primary sources: an SEC
filing names an exhibit, the exhibit names a subsidiary, the subsidiary resolves
at a foreign registry, the registry exposes a charge. The branch-correctness
scorer measures whether a run traversed the right *kinds* of source in the right
dependency order, so every retrieval — by the single-shot baseline now, by the
Arm A agent later — is tagged with a canonical :class:`SourceType`.

This module owns three things:

1. :class:`SourceType` — the canonical taxonomy, and ``classify_source`` which
   maps the fixtures' free-text ``ground_truth_path[].source`` strings onto it.
   One classifier, used by both the fixture loader (to derive the expected
   chain) and any agent (to tag what it actually retrieved), keeps the two
   sides comparable.

2. ``EdgarClient`` — a *minimal* EDGAR retrieval. C2 only needs the one-shot
   "pull the issuer's recent filings index" call the single-shot baseline makes;
   the client is structured so the C3 production spine extends it (filing-body
   and exhibit fetches) rather than replacing it. We deliberately build only the
   one-shot surface here.

Application code reads ``os.environ`` only (User-Agent, base URLs) — never a
.env loader; the invocation surface (Makefile / ``uv run --env-file``) owns that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

import httpx


class SourceType(StrEnum):
    """Canonical source kinds a credit investigation traverses.

    Coarse enough to be robust to the fixtures' free-text source descriptions,
    fine enough that an ordered sequence of these types *is* the dependency
    chain the branch-correctness scorer checks. ``EDGAR_SUBMISSIONS_INDEX`` is
    distinct from ``EDGAR_FILING`` precisely because it is the correct *single*
    source for a healthy-issuer screen (the controls) — a glance at the filings
    list — whereas a real distress chain starts by opening a specific filing.
    """

    EDGAR_SUBMISSIONS_INDEX = "edgar_submissions_index"
    EDGAR_FILING = "edgar_filing"
    EDGAR_EXHIBIT = "edgar_exhibit"
    COMPANIES_HOUSE = "companies_house"
    EXTERNAL_RATING = "external_rating"


def classify_source(source: str) -> SourceType:
    """Map a free-text ``ground_truth_path[].source`` string to a SourceType.

    Order is load-bearing: a few descriptions name more than one artefact
    (e.g. the trap's ``"8-K (Item 1.01) + amendment exhibits"`` — the *filing*
    is the hop, reading its exhibit is part of that same retrieval), so more
    specific markers are tested before the generic filing marker.

    Raises ``ValueError`` on an unclassifiable string rather than guessing — an
    unmapped source is a fixture/loader drift bug we want surfaced loudly.
    """
    s = source.lower()
    if "submissions index" in s or "submissions" in s:
        return SourceType.EDGAR_SUBMISSIONS_INDEX
    if "companies house" in s:
        return SourceType.COMPANIES_HOUSE
    if "s&p" in s or "rating" in s:
        return SourceType.EXTERNAL_RATING
    if "8-k" in s or "10-k" in s:
        return SourceType.EDGAR_FILING
    if "exhibit" in s:
        return SourceType.EDGAR_EXHIBIT
    raise ValueError(f"cannot classify source description: {source!r}")


# ---------------------------------------------------------------------------
# Minimal EDGAR retrieval (the only live source C2 needs)
# ---------------------------------------------------------------------------

# SEC requires a descriptive User-Agent on every request and rate-limits to
# ~10 req/s. We read the UA from the environment so a real deployment sets its
# own contact string; the default is a clearly-labelled fallback.
_DEFAULT_UA = "credit-risk-monitoring-eval (contact: set SEC_EDGAR_USER_AGENT)"


def _user_agent() -> str:
    return os.environ.get("SEC_EDGAR_USER_AGENT", _DEFAULT_UA)


def _submissions_base() -> str:
    return os.environ.get("SEC_EDGAR_SUBMISSIONS_BASE", "https://data.sec.gov/submissions")


@dataclass(frozen=True)
class FilingRef:
    """One row from an issuer's recent-filings index."""

    form: str
    filing_date: str
    accession: str
    primary_document: str
    primary_doc_description: str

    def summary_line(self) -> str:
        desc = f" — {self.primary_doc_description}" if self.primary_doc_description else ""
        return f"{self.filing_date}  {self.form:<8}  acc {self.accession}{desc}"


@dataclass(frozen=True)
class SubmissionsIndex:
    """The result of the single-shot baseline's one retrieval.

    Holds enough of the issuer's recent-filings index to answer a credit-screen
    question shallowly — which is exactly the baseline's deliberate limitation:
    it can see *that* an 8-K exists, never read the exhibit body behind it.
    """

    cik: str
    issuer_name: str
    filings: tuple[FilingRef, ...]

    @property
    def locator(self) -> str:
        return f"data.sec.gov submissions CIK {self.cik}"

    def context_block(self, limit: int = 25) -> str:
        """Compact text rendering handed to the LLM as the sole evidence."""
        head = f"EDGAR recent filings for {self.issuer_name} (CIK {self.cik}):"
        rows = [f"- {f.summary_line()}" for f in self.filings[:limit]]
        if not rows:
            rows = ["- (no recent filings returned)"]
        return "\n".join([head, *rows])


def normalize_cik(cik: str) -> str:
    """EDGAR submissions paths use a zero-padded 10-digit CIK.

    Fixtures sometimes carry compound CIK fields (``"0001657853 / 0000047129"``)
    for issuers with a holding co + operating co; we take the first.
    """
    first = cik.split("/")[0].strip()
    digits = "".join(ch for ch in first if ch.isdigit())
    if not digits:
        raise ValueError(f"no digits in CIK {cik!r}")
    return digits.zfill(10)


class EdgarClient:
    """Thin synchronous EDGAR client — only the one-shot index call C2 needs.

    Structured as the seam the C3 production spine extends (a real agent adds
    ``fetch_filing`` / ``fetch_exhibit`` / Companies House lookups as further
    hops); C2 builds only ``fetch_submissions_index``.
    """

    def __init__(self, client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._client = client or httpx.Client(
            headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
        )

    def fetch_submissions_index(self, cik: str) -> SubmissionsIndex:
        """Fetch an issuer's recent-filings index from data.sec.gov.

        Raises ``httpx.HTTPStatusError`` on a non-2xx (e.g. an unknown CIK)
        rather than papering over it — the baseline records the failure as a
        tool_error in the trace.
        """
        padded = normalize_cik(cik)
        url = f"{_submissions_base()}/CIK{padded}.json"
        resp = self._client.get(url)
        resp.raise_for_status()
        data = resp.json()

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        descs = recent.get("primaryDocDescription", [])

        filings: list[FilingRef] = []
        for i in range(len(forms)):
            filings.append(
                FilingRef(
                    form=forms[i],
                    filing_date=dates[i] if i < len(dates) else "",
                    accession=accessions[i] if i < len(accessions) else "",
                    primary_document=docs[i] if i < len(docs) else "",
                    primary_doc_description=descs[i] if i < len(descs) else "",
                )
            )
        return SubmissionsIndex(
            cik=padded,
            issuer_name=data.get("name", ""),
            filings=tuple(filings),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
