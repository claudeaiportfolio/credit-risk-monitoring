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

import contextlib
import os
import re
import threading
import time
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


def _archives_base() -> str:
    """Base for filing-directory + document fetches (www.sec.gov/Archives)."""
    return os.environ.get("SEC_EDGAR_ARCHIVES_BASE", "https://www.sec.gov/Archives")


# ---------------------------------------------------------------------------
# Shared HTTP primitives — rate limiting + retry/backoff
# ---------------------------------------------------------------------------


class RateLimiter:
    """Minimal monotonic-clock rate limiter (>= ``min_interval`` between calls).

    SEC asks for <= ~10 requests/second across data.sec.gov + www.sec.gov, so a
    single limiter is shared across an :class:`EdgarClient`'s calls regardless of
    which host they hit. Thread-safe so a sync client used from a thread pool
    stays polite. ``acquire`` blocks only as long as needed.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


# Status codes worth retrying: SEC/CH transient throttling + gateway errors.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    limiter: RateLimiter,
    max_retries: int = 4,
    backoff_base: float = 0.5,
    backoff_cap: float = 8.0,
    **kwargs: object,
) -> httpx.Response:
    """Issue one rate-limited request with bounded exponential backoff.

    Honours an upstream ``Retry-After`` header when present (SEC and Companies
    House both send it on 429). Retries transient statuses and transport errors;
    raises ``httpx.HTTPStatusError`` on a non-retryable non-2xx, and re-raises the
    last transport error if every attempt fails. Deterministic backoff (no
    jitter) keeps tests reproducible; the cap bounds total wait.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        limiter.acquire()
        try:
            resp = client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            time.sleep(min(backoff_base * (2**attempt), backoff_cap))
            continue
        if resp.status_code in _RETRY_STATUS and attempt < max_retries:
            retry_after = resp.headers.get("Retry-After")
            delay = min(backoff_base * (2**attempt), backoff_cap)
            if retry_after is not None:
                with contextlib.suppress(ValueError):
                    delay = min(float(retry_after), backoff_cap)
            time.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    # Exhausted retries on transport errors.
    assert last_exc is not None
    raise last_exc


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

    @property
    def oldest_filing_date(self) -> str:
        """ISO date of the oldest filing held (``""`` if none) — lets a caller
        decide whether a requested ``date_from`` reaches before the loaded window
        and therefore needs pagination into ``filings.files``."""
        dates = [f.filing_date for f in self.filings if f.filing_date]
        return min(dates) if dates else ""

    def matching(
        self,
        *,
        form_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[FilingRef, ...]:
        """Filings matching a form type and/or inclusive ISO filing-date range.

        ``form_type`` matches case-insensitively on the exact form *and* its
        amendments (``"8-K"`` also surfaces ``"8-K/A"``). Dates are EDGAR's
        ``YYYY-MM-DD`` strings, so lexicographic comparison is chronological.
        This is what makes a historical filing discoverable: the issuer's recent
        block holds ~1000 filings, far more than any sane render cap, so the model
        narrows by form+date instead of scanning newest-first.
        """
        ft = form_type.strip().upper() if form_type else None
        lo = date_from.strip() if date_from else None
        hi = date_to.strip() if date_to else None
        out: list[FilingRef] = []
        for f in self.filings:
            form = f.form.upper()
            if ft and not (form == ft or form.startswith(ft + "/")):
                continue
            if lo and f.filing_date and f.filing_date < lo:
                continue
            if hi and f.filing_date and f.filing_date > hi:
                continue
            out.append(f)
        return tuple(out)

    def context_block(
        self,
        limit: int = 25,
        *,
        form_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        """Compact text rendering handed to the LLM.

        Unfiltered, it renders the newest ``limit`` filings (the healthy-issuer
        screen). With any ``form_type``/``date_from``/``date_to`` it renders the
        *matching* rows (newest first, capped at ``limit``) and reports the total
        match count plus an overflow hint — so a 2020 8-K buried ~400 rows deep is
        surfaced when the model knows the issuer and the rough event date.
        """
        filtered = bool(form_type or date_from or date_to)
        if not filtered:
            head = f"EDGAR recent filings for {self.issuer_name} (CIK {self.cik}):"
            rows = [f"- {f.summary_line()}" for f in self.filings[:limit]]
            if not rows:
                rows = ["- (no recent filings returned)"]
            return "\n".join([head, *rows])

        matches = self.matching(form_type=form_type, date_from=date_from, date_to=date_to)
        crit = []
        if form_type:
            crit.append(f"form {form_type}")
        if date_from:
            crit.append(f"from {date_from}")
        if date_to:
            crit.append(f"to {date_to}")
        head = (
            f"EDGAR filings for {self.issuer_name} (CIK {self.cik}) "
            f"matching {', '.join(crit)} — {len(matches)} match(es):"
        )
        rows = [f"- {f.summary_line()}" for f in matches[:limit]]
        if not rows:
            rows = ["- (no filings match this form/date filter)"]
        lines = [head, *rows]
        if len(matches) > limit:
            lines.append(
                f"… {len(matches) - limit} more match(es) not shown — narrow the "
                "date range or form type."
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class FilingDocument:
    """One document inside a filing's accession directory."""

    name: str
    type: str
    description: str
    url: str

    def summary_line(self) -> str:
        bits = [self.type or "?", self.name]
        if self.description:
            bits.append(f"— {self.description}")
        return "  ".join(bits)


@dataclass(frozen=True)
class FilingDirectory:
    """The contents of one accession (the 8-K + its exhibits)."""

    cik: str
    accession: str
    primary_document_url: str
    documents: tuple[FilingDocument, ...]

    @property
    def locator(self) -> str:
        return f"EDGAR acc {self.accession} (CIK {self.cik})"

    def exhibits(self) -> tuple[FilingDocument, ...]:
        """Documents that look like exhibits (EX-*) rather than the primary doc."""
        return tuple(
            d for d in self.documents if d.type.upper().startswith("EX") or "ex" in d.name.lower()
        )

    def index_block(self, limit: int = 40) -> str:
        head = f"Documents in {self.locator}:"
        rows = [f"- {d.summary_line()}\n  {d.url}" for d in self.documents[:limit]]
        if not rows:
            rows = ["- (no documents listed)"]
        return "\n".join([head, *rows])


@dataclass(frozen=True)
class FetchedDocument:
    """A filing body or exhibit fetched as text."""

    url: str
    content_type: str
    text: str

    @property
    def locator(self) -> str:
        return self.url

    def context_block(self, limit: int = 12000) -> str:
        body = self.text[:limit]
        suffix = "" if len(self.text) <= limit else f"\n…[truncated, {len(self.text)} chars total]"
        return f"Document {self.url} ({self.content_type}):\n{body}{suffix}"


_TAG_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAGS_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n\s*\n\s*\n+")


def html_to_text(raw: str) -> str:
    """Best-effort HTML-to-text for EDGAR filing bodies/exhibits.

    EDGAR documents are large HTML; the agent only needs readable text to find
    named entities/exhibits. We strip script/style, drop tags, unescape the few
    entities that matter, and collapse whitespace. Deliberately dependency-free
    (no bs4) — robustness over fidelity for an extraction-oriented read.
    """
    import html as _html

    no_scripts = _TAG_RE.sub(" ", raw)
    # Preserve block boundaries so entity names don't run together.
    spaced = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6])\s*>", "\n", no_scripts)
    text = _TAGS_RE.sub(" ", spaced)
    text = _html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


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


def accession_nodashes(accession: str) -> str:
    """``0001104659-20-096796`` -> ``000110465920096796`` (archive-path form)."""
    digits = accession.replace("-", "").strip()
    if not digits.isdigit():
        raise ValueError(f"malformed accession {accession!r}")
    return digits


# SEC asks consumers to stay at or under ~10 requests/second. One limiter is
# shared across all of a client's calls (data.sec.gov + www.sec.gov). The
# default 0.11s interval keeps us just under 10 req/s with headroom.
_DEFAULT_EDGAR_INTERVAL = float(os.environ.get("SEC_EDGAR_MIN_INTERVAL", "0.11"))


class EdgarClient:
    """Synchronous EDGAR retrieval spine for the credit investigation.

    Extends C2's one-shot submissions-index call into the full chain the Arm A
    agent walks: recent-filings index -> a specific filing's document directory
    -> the 8-K body and its exhibits. Every request is rate-limited (SEC's
    ~10 req/s ceiling) and retried with backoff on transient throttling/gateway
    errors; a descriptive ``User-Agent`` is sent (SEC 403s a generic one).

    The methods map one-to-one onto the canonical retrieval source types the
    branch-correctness scorer reads:

    * ``fetch_submissions_index`` -> ``EDGAR_SUBMISSIONS_INDEX``
    * ``fetch_filing`` (directory + body) -> ``EDGAR_FILING``
    * ``fetch_exhibit`` (a named exhibit document) -> ``EDGAR_EXHIBIT``
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        *,
        limiter: RateLimiter | None = None,
        max_retries: int = 4,
    ) -> None:
        self._timeout = timeout
        self._client = client or httpx.Client(
            headers={"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter(_DEFAULT_EDGAR_INTERVAL)
        self._max_retries = max_retries

    def _get(self, url: str) -> httpx.Response:
        return request_with_retry(
            self._client, "GET", url, limiter=self._limiter, max_retries=self._max_retries
        )

    # -- hop 1: the recent-filings index -----------------------------------
    def fetch_submissions_index(self, cik: str, *, paginate: bool = False) -> SubmissionsIndex:
        """Fetch an issuer's recent-filings index from data.sec.gov.

        The base document carries the most recent ~1000 filings under
        ``filings.recent``; older filings are paginated into the files listed
        under ``filings.files``. ``paginate=True`` follows those (each is the
        same column-array shape at top level) and merges them in date order —
        needed only when a target filing predates the recent window.

        Raises ``httpx.HTTPStatusError`` on a non-2xx (e.g. an unknown CIK).
        """
        padded = normalize_cik(cik)
        data = self._get(f"{_submissions_base()}/CIK{padded}.json").json()
        recent = data.get("filings", {}).get("recent", {})
        filings = list(_filing_refs_from_arrays(recent))

        if paginate:
            for extra in data.get("filings", {}).get("files", []) or []:
                name = extra.get("name")
                if not name:
                    continue
                page = self._get(f"{_submissions_base()}/{name}").json()
                filings.extend(_filing_refs_from_arrays(page))

        return SubmissionsIndex(
            cik=padded,
            issuer_name=data.get("name", ""),
            filings=tuple(filings),
        )

    def find_filings(
        self,
        cik: str,
        *,
        form_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        paginate: bool = False,
    ) -> tuple[FilingRef, ...]:
        """Locate an issuer's filings by form type and/or filing-date window.

        This is the *addressing* lookup that turns "the issuer's 8-K from August
        2020" into a concrete accession — the step a real analyst does via EDGAR
        full-text search before opening a filing. It is deliberately a helper, not
        a scored source hop: in a credit chain the SOURCE reached is the filing,
        not the index used to find its address (the submissions index is a scored
        source only for the healthy-issuer screen). Paginates automatically when
        the requested ``date_from`` predates the loaded recent window.
        """
        index = self.fetch_submissions_index(cik, paginate=paginate)
        if not paginate and date_from:
            oldest = index.oldest_filing_date
            if oldest and date_from < oldest:
                index = self.fetch_submissions_index(cik, paginate=True)
        return index.matching(form_type=form_type, date_from=date_from, date_to=date_to)

    # -- hop 2: a specific filing's directory + body -----------------------
    def fetch_filing_directory(self, cik: str, accession: str) -> FilingDirectory:
        """List the documents (primary doc + exhibits) inside one accession."""
        cik_int = str(int(normalize_cik(cik)))
        acc = accession_nodashes(accession)
        base = f"{_archives_base()}/edgar/data/{cik_int}/{acc}"
        data = self._get(f"{base}/index.json").json()
        items = data.get("directory", {}).get("item", []) or []
        docs: list[FilingDocument] = []
        for it in items:
            name = str(it.get("name", ""))
            if not name or name.lower() in {"index.json", "0001.txt"}:
                continue
            docs.append(
                FilingDocument(
                    name=name,
                    type=str(it.get("type", "")),
                    description=str(it.get("description", "")),
                    url=f"{base}/{name}",
                )
            )
        primary = _pick_primary_document(docs)
        return FilingDirectory(
            cik=normalize_cik(cik),
            accession=accession,
            primary_document_url=primary.url if primary else "",
            documents=tuple(docs),
        )

    def fetch_document(self, url: str) -> FetchedDocument:
        """Fetch one filing document/exhibit and return it as readable text."""
        resp = self._get(url)
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.text
        text = html_to_text(raw) if ("html" in ctype.lower() or _looks_like_html(raw)) else raw
        return FetchedDocument(url=url, content_type=ctype or "text/plain", text=text)

    def fetch_filing(
        self, cik: str, accession: str, *, primary_document: str | None = None
    ) -> tuple[FilingDirectory, FetchedDocument]:
        """Fetch a filing's directory and the body of its primary document.

        ``primary_document`` (from the submissions index) pins the body when the
        directory's heuristic pick would be ambiguous; otherwise the largest /
        first matching ``.htm`` is used. The pin is honored ONLY if it names a
        document that actually exists in this accession's directory: a guessed or
        stale filename falls back to the directory's own primary pick rather than
        constructing a URL that 404s (which would otherwise abort an
        investigation on a hallucinated filename).
        """
        directory = self.fetch_filing_directory(cik, accession)
        body_url = ""
        if primary_document:
            match = next(
                (d for d in directory.documents if d.name == primary_document), None
            )
            body_url = match.url if match else ""
        body_url = body_url or directory.primary_document_url
        if not body_url:
            raise ValueError(f"no primary document found for accession {accession!r}")
        body = self.fetch_document(body_url)
        return directory, body

    def fetch_exhibit(self, url: str) -> FetchedDocument:
        """Fetch a named exhibit document (a distinct hop from the filing body)."""
        return self.fetch_document(url)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _filing_refs_from_arrays(block: dict) -> list[FilingRef]:
    forms = block.get("form", []) or []
    dates = block.get("filingDate", []) or []
    accessions = block.get("accessionNumber", []) or []
    docs = block.get("primaryDocument", []) or []
    descs = block.get("primaryDocDescription", []) or []
    out: list[FilingRef] = []
    for i in range(len(forms)):
        out.append(
            FilingRef(
                form=forms[i],
                filing_date=dates[i] if i < len(dates) else "",
                accession=accessions[i] if i < len(accessions) else "",
                primary_document=docs[i] if i < len(docs) else "",
                primary_doc_description=descs[i] if i < len(descs) else "",
            )
        )
    return out


# SEC directory listings interleave the real filing documents with generated
# wrapper artefacts (the SGML header dump ``*-index-headers.html``, the
# ``*-index.html`` cover page, the ``R\d+.htm`` XBRL viewer fragments,
# ``FilingSummary``/``Financial_Report``). None of these is the filing body, but
# the directory ``type`` field is frequently just an icon name (e.g. ``text.gif``)
# rather than the document type, so an EX-by-type filter alone lets a wrapper win.
# Skip wrappers by NAME, and fall back to an exhibit-by-name filter when the type
# field is uninformative — otherwise the primary pick lands on the index header
# page (which contains no filing body) and an accession-only open reads garbage.
_SEC_WRAPPER_RE = re.compile(
    r"(-index(-headers)?\.html?$)|(^r\d+\.html?$)|(filingsummary)|(financial_report)",
    re.IGNORECASE,
)
_EXHIBIT_NAME_RE = re.compile(r"(_ex[\d.])|(-ex\d)|(^ex-?\d)", re.IGNORECASE)


def _pick_primary_document(docs: list[FilingDocument]) -> FilingDocument | None:
    """Heuristic primary-document pick: the first non-exhibit filing ``.htm``,
    excluding SEC-generated wrapper/index/XBRL-viewer artefacts."""
    htmls = [
        d
        for d in docs
        if d.name.lower().endswith((".htm", ".html")) and not _SEC_WRAPPER_RE.search(d.name)
    ]
    non_ex = [
        d
        for d in htmls
        if not d.type.upper().startswith("EX") and not _EXHIBIT_NAME_RE.search(d.name)
    ]
    if non_ex:
        return non_ex[0]
    return htmls[0] if htmls else (docs[0] if docs else None)


def _looks_like_html(raw: str) -> bool:
    head = raw[:512].lower()
    return "<html" in head or "<!doctype html" in head or "<body" in head
