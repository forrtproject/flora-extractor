from typing import Optional

"""
external_lists.py — Scrapers for I4R list and SCORE CSV.

Public API:
    fetch_bob_reed() → pd.DataFrame   (CANDIDATES_COLS schema)
    fetch_i4r()            → pd.DataFrame  (CANDIDATES_COLS schema)
    load_score_csv(path)   → pd.DataFrame  (CANDIDATES_COLS schema)
"""
import html
import re

import pandas as pd
import requests

from shared.config import log
from shared.schema import CANDIDATES_COLS

# ---------------------------------------------------------------------------
# Bob Reed / Replication Network
#
# The list is maintained in a publicly-published Google Sheet embedded at
# https://replicationnetwork.com/replication-studies/
# Sheet ID: 1kdoiWgi-e5dzsFnZmrXqWebkW3hcMoDxH2437r8QQjk
# Columns: YEAR, JOURNAL, AUTHORS, TITLE  (no DOIs)
# There was previously a ReplicationWiki site but this is no longer maintained
# ---------------------------------------------------------------------------

_BOB_REED_SHEET_ID = "1kdoiWgi-e5dzsFnZmrXqWebkW3hcMoDxH2437r8QQjk"
_BOB_REED_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{_BOB_REED_SHEET_ID}/pub?output=csv"
)


def fetch_replication_network(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
) -> pd.DataFrame:
    """
    Fetch Replication Network list from the publicly-published Google Sheet.

    Parameters
    ----------
    from_year : int, optional
        Earliest publication year (inclusive).
    to_year : int, optional
        Latest publication year (inclusive).

    Returns a DataFrame with CANDIDATES_COLS schema.
    DOIs are not available in the sheet; enrich later via OpenAlex/Crossref.
    """
    import io

    try:
        resp = requests.get(_BOB_REED_CSV_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Bob Reed sheet request failed (%s) — returning 0 rows", exc)
        return pd.DataFrame(columns=CANDIDATES_COLS)

    raw = pd.read_csv(io.StringIO(resp.text), dtype=str).fillna("")

    # Drop unnamed index column Google Sheets sometimes includes
    raw = raw.loc[:, ~raw.columns.str.startswith("Unnamed")]

    cols = {c.strip().upper(): c for c in raw.columns}

    def get(names):
        for n in names:
            if n in cols:
                return raw[cols[n]].replace("", None)
        return pd.Series([None] * len(raw))

    df = pd.DataFrame(
        {
            "doi_r": None,
            "title_r": get(["TITLE"]),
            "abstract_r": None,
            "year_r": pd.to_numeric(get(["YEAR"]), errors="coerce").astype("Int64"),
            "authors_r": get(["AUTHORS"]),
            "journal_r": get(["JOURNAL"]),
            "url_r": None,
            "openalex_id_r": None,
            "source": "bob_reed",
        },
        columns=CANDIDATES_COLS,
    )

    if from_year is not None:
        df = df[df["year_r"].isna() | (df["year_r"] >= from_year)]
    if to_year is not None:
        df = df[df["year_r"].isna() | (df["year_r"] <= to_year)]

    log.info(
        "Bob Reed: %d papers fetched (years: %s–%s)",
        len(df),
        from_year or "any",
        to_year or "any",
    )
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# I4R  —  scraped from the IDEAS/RepEC listing (paginated)
#
# Each paper is one line:
#   <LI class="list-group-item downfree">
#     <B>NNN <A HREF="/p/zbw/i4rdps/NNN.html">Title</A></B>
#     <BR><I>by</I> Author1 &amp; Author2
#
# Year groups are delimited by:
#   <h3>YYYY</h3>
#
# The series spans multiple pages (i4rdps.html, i4rdps2.html, …).
# Pagination links appear as: href="i4rdpsN.html"
# ---------------------------------------------------------------------------

_REPEC_URL = "https://ideas.repec.org/s/zbw/i4rdps.html"
_REPEC_BASE = "https://ideas.repec.org"
_REPEC_SERIES_BASE = "https://ideas.repec.org/s/zbw/"

_YEAR_RE = re.compile(r"<h3>(20\d\d)</h3>", re.IGNORECASE)
_PAPER_RE = re.compile(
    r"<LI[^>]*list-group-item[^>]*>\s*"
    r"<B>\s*(\d+)\s+"
    r'<A HREF="(/p/zbw/i4rdps/\d+\.html)">(.*?)</A>'
    r"</B><BR><I>by</I>\s*(.*?)(?:\s*$)",
    re.IGNORECASE,
)
# Matches pagination hrefs like href="i4rdps2.html", href="i4rdps10.html"
_PAGE_LINK_RE = re.compile(r'href="(i4rdps\d+\.html)"', re.IGNORECASE)


def _parse_repec_page(text: str) -> list[dict]:
    """Extract paper rows from one RepEC HTML page."""
    rows = []
    current_year = None
    for line in text.splitlines():
        ym = _YEAR_RE.search(line)
        if ym:
            current_year = int(ym.group(1))
            continue
        pm = _PAPER_RE.search(line)
        if not pm:
            continue
        _paper_no, href, raw_title, raw_authors = pm.groups()
        rows.append(
            {
                "doi_r": None,
                "title_r": html.unescape(raw_title).strip(),
                "abstract_r": None,
                "year_r": current_year,
                "authors_r": html.unescape(raw_authors).strip() or None,
                "journal_r": "I4R Discussion Paper",
                "url_r": _REPEC_BASE + href,
                "openalex_id_r": None,
                "source": "i4r",
            }
        )
    return rows


def fetch_i4r(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
) -> pd.DataFrame:
    """
    Scrape I4R discussion papers from the IDEAS/RepEC series (all pages).

    Parameters
    ----------
    from_year : int, optional
        Earliest publication year (inclusive). None = no lower bound.
    to_year : int, optional
        Latest publication year (inclusive). None = no upper bound.

    Returns a DataFrame with CANDIDATES_COLS schema.
    DOIs are not available on this page; enrich later via OpenAlex/Crossref.
    """
    try:
        resp = requests.get(_REPEC_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("I4R/RepEC request failed (%s) — returning 0 rows", exc)
        return pd.DataFrame(columns=CANDIDATES_COLS)

    page1_text = resp.text
    rows = _parse_repec_page(page1_text)

    # Discover additional pages from pagination links (e.g. i4rdps2.html, i4rdps3.html)
    extra_pages = sorted(set(_PAGE_LINK_RE.findall(page1_text)))
    for page_file in extra_pages:
        url = _REPEC_SERIES_BASE + page_file
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            rows.extend(_parse_repec_page(r.text))
        except requests.RequestException as exc:
            log.warning("I4R/RepEC page %s failed (%s) — skipping", url, exc)

    if not rows:
        log.warning("I4R scraper found no papers at %s", _REPEC_URL)
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(rows, columns=CANDIDATES_COLS)

    if from_year is not None:
        df = df[df["year_r"].isna() | (df["year_r"] >= from_year)]
    if to_year is not None:
        df = df[df["year_r"].isna() | (df["year_r"] <= to_year)]
    yr = df["year_r"].dropna()
    log.info(
        "I4R: %d papers scraped (%d–%d)",
        len(df),
        int(yr.min()) if len(yr) else 0,
        int(yr.max()) if len(yr) else 0,
    )
    return df
