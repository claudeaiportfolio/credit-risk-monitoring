"""Source-type classifier + CIK normalisation."""

from __future__ import annotations

import pytest

from credit_risk_monitoring.sources import SourceType, classify_source, normalize_cik


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("8-K (Items 1.01/1.03/2.04)", SourceType.EDGAR_FILING),
        ("8-K (Item 1.03)", SourceType.EDGAR_FILING),
        ("Exhibit 10.1 (RSA), Exhibit D debtor table", SourceType.EDGAR_EXHIBIT),
        ("Exhibit EX-10.1 (Forbearance Agreement)", SourceType.EDGAR_EXHIBIT),
        ("Companies House company record", SourceType.COMPANIES_HOUSE),
        ("Companies House charges", SourceType.COMPANIES_HOUSE),
        ("Companies House charge (MR01)", SourceType.COMPANIES_HOUSE),
        ("S&P Global Ratings action", SourceType.EXTERNAL_RATING),
        ("EDGAR submissions index", SourceType.EDGAR_SUBMISSIONS_INDEX),
        # The trap names both an 8-K and its exhibits — the filing is the hop.
        ("8-K (Item 1.01) + amendment exhibits", SourceType.EDGAR_FILING),
    ],
)
def test_classify_source(description: str, expected: SourceType) -> None:
    assert classify_source(description) == expected


def test_classify_source_unmappable_raises() -> None:
    with pytest.raises(ValueError, match="cannot classify"):
        classify_source("some unknown primary source")


def test_normalize_cik_pads_to_ten() -> None:
    assert normalize_cik("789019") == "0000789019"
    assert normalize_cik("0000789019") == "0000789019"


def test_normalize_cik_takes_first_of_compound() -> None:
    # Holding-co / operating-co compound field.
    assert normalize_cik("0001657853 / 0000047129") == "0001657853"


def test_normalize_cik_no_digits_raises() -> None:
    with pytest.raises(ValueError, match="no digits"):
        normalize_cik("n/a")
