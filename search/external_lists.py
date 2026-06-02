"""
external_lists.py — Scrapers for I4R list and Replication Network.

Public API:
    fetch_replication_network() → pd.DataFrame   (CANDIDATES_COLS schema)
    fetch_i4r()                 → pd.DataFrame   (CANDIDATES_COLS schema)

fetch_i4r() enriches each paper by fetching its individual IDEAS page (abstract,
EconStor PDF URL) and running an OpenAlex title search (DOI, openalex_id_r).
All enrichment requests are cached under cache/i4r/ so repeat runs are instant.
"""
import html
import re
import time
from typing import Optional

import pandas as pd
import requests

from shared.cache import read_cache, write_cache
from shared.config import CACHE_DIR, RESEARCHER_EMAIL, log
from shared.schema import CANDIDATES_COLS
from shared.utils import cache_key, clean_doi

# ---------------------------------------------------------------------------
# Bob Reed / Replication Network
#
# The list is maintained in a publicly-published Google Sheet embedded at
# https://replicationnetwork.com/replication-studies/
# Sheet ID: 1kdoiWgi-e5dzsFnZmrXqWebkW3hcMoDxH2437r8QQjk
# Columns: YEAR, JOURNAL, AUTHORS, TITLE  (no DOIs)
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
# Listing pages give: paper number, title, authors, year, IDEAS URL.
# Individual paper pages add: abstract (meta description), EconStor PDF URL.
# OpenAlex title search adds: doi_r, openalex_id_r (where indexed).
#
# Listing HTML structure (one line per paper):
#   <LI class="list-group-item downfree">
#     <B>NNN <A HREF="/p/zbw/i4rdps/NNN.html">Title</A></B>
#     <BR><I>by</I> Author1 &amp; Author2
#
# Year groups are delimited by: <h3>YYYY</h3>
# Pagination links appear as: href="i4rdpsN.html"
# ---------------------------------------------------------------------------

_REPEC_URL = "https://ideas.repec.org/s/zbw/i4rdps.html"
_REPEC_BASE = "https://ideas.repec.org"
_REPEC_SERIES_BASE = "https://ideas.repec.org/s/zbw/"

_I4R_CACHE_DIR = CACHE_DIR / "i4r"

_YEAR_RE = re.compile(r"<h3>(20\d\d)</h3>", re.IGNORECASE)
_PAPER_RE = re.compile(
    r"<LI[^>]*list-group-item[^>]*>\s*"
    r"<B>\s*(\d+)\s+"
    r'<A HREF="(/p/zbw/i4rdps/\d+\.html)">(.*?)</A>'
    r"</B><BR><I>by</I>\s*(.*?)(?:\s*$)",
    re.IGNORECASE,
)
_PAGE_LINK_RE = re.compile(r'href="(i4rdps\d+\.html)"', re.IGNORECASE)

# Matches the abstract and the EconStor PDF radio-button input on individual paper pages.
# The form structure is: <INPUT TYPE="radio" NAME="url" VALUE="https://...pdf" checked>
_DETAIL_ABSTRACT_RE = re.compile(
    r'<META NAME="description" CONTENT="([^"]+)"',
    re.IGNORECASE,
)
_DETAIL_PDF_RE = re.compile(
    r'<INPUT[^>]+TYPE="radio"[^>]+NAME="url"[^>]+VALUE="(https://[^"]+)"',
    re.IGNORECASE,
)


def _parse_repec_page(text: str) -> list[dict]:
    """Extract paper rows from one RepEC listing page."""
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
        authors = html.unescape(raw_authors).strip() or None
        # ref_r: "Surname · Year · Journal"
        if authors:
            first = authors.split("&")[0].strip()
            surname = first.split(",")[0].strip() if "," in first else (first.split()[-1] if first else "")
        else:
            surname = ""
        ref_r = " · ".join(s for s in [surname, str(current_year) if current_year else "", "I4R Discussion Paper"] if s)
        rows.append(
            {
                "doi_r": None,
                "title_r": html.unescape(raw_title).strip(),
                "abstract_r": None,
                "year_r": current_year,
                "authors_r": authors,
                "journal_r": "I4R Discussion Paper",
                "url_r": _REPEC_BASE + href,
                "openalex_id_r": None,
                "source": "i4r",
                "ref_r": ref_r,
            }
        )
    return rows


def _fetch_i4r_paper_detail(ideas_url: str) -> dict:
    """
    Fetch abstract and EconStor PDF URL from an individual IDEAS paper page.
    Cached by URL — never hits the network twice for the same paper.
    """
    key = cache_key("i4r_detail_" + ideas_url)
    cached = read_cache(_I4R_CACHE_DIR, key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(ideas_url, timeout=20, headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("I4R detail fetch failed for %s (%s)", ideas_url, exc)
        result: dict = {"abstract_r": None, "pdf_url": None}
        write_cache(_I4R_CACHE_DIR, key, result)
        return result

    abstract_m = _DETAIL_ABSTRACT_RE.search(resp.text)
    pdf_m = _DETAIL_PDF_RE.search(resp.text)

    abstract = html.unescape(abstract_m.group(1)).strip() if abstract_m else None
    # IDEAS prepends "Downloadable! " to every abstract
    if abstract and abstract.startswith("Downloadable! "):
        abstract = abstract[len("Downloadable! "):]

    result = {
        "abstract_r": abstract,
        "pdf_url": pdf_m.group(1) if pdf_m else None,
    }
    write_cache(_I4R_CACHE_DIR, key, result)
    return result


def _title_word_overlap(a: str, b: str) -> float:
    """Jaccard overlap of lowercase word sets — used to guard OpenAlex title matches."""
    wa = set(re.sub(r"[^\w\s]", "", a.lower()).split())
    wb = set(re.sub(r"[^\w\s]", "", b.lower()).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _lookup_i4r_openalex(title: str) -> dict:
    """
    Title-search OpenAlex to find doi_r and openalex_id_r for an I4R paper.
    Uses the 'search' parameter (handles colons, apostrophes) with a word-overlap
    guard to reject false-positive matches.
    Cached by title — never queries OpenAlex twice for the same paper.
    Returns {"doi_r": ..., "openalex_id_r": ...} with None values on miss.
    """
    key = cache_key("i4r_oa_" + title)
    cached = read_cache(_I4R_CACHE_DIR, key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "search": title[:120],
                "mailto": RESEARCHER_EMAIL,
                "per_page": 5,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as exc:
        log.warning("OpenAlex title search failed for I4R paper '%s': %s", title[:60], exc)
        result: dict = {"doi_r": None, "openalex_id_r": None}
        write_cache(_I4R_CACHE_DIR, key, result)
        return result

    # Keep only results whose title has ≥50% word overlap with our query
    for candidate in results:
        oa_title = candidate.get("title", "") or ""
        if _title_word_overlap(title, oa_title) >= 0.5:
            doi = clean_doi(candidate.get("doi", "") or "")
            oa_id = (candidate.get("id", "") or "").replace("https://openalex.org/", "")
            result = {"doi_r": doi or None, "openalex_id_r": oa_id or None}
            write_cache(_I4R_CACHE_DIR, key, result)
            return result

    result = {"doi_r": None, "openalex_id_r": None}
    write_cache(_I4R_CACHE_DIR, key, result)
    return result


def fetch_i4r(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    enrich: bool = True,
) -> pd.DataFrame:
    """
    Scrape I4R discussion papers from the IDEAS/RepEC series (all pages).

    With enrich=True (default), each paper's individual IDEAS page is fetched
    for its abstract and EconStor PDF URL, and OpenAlex is queried by title
    for a DOI and openalex_id_r. All enrichment requests are cached under
    cache/i4r/ so only the first run makes network calls.

    Parameters
    ----------
    from_year : int, optional
        Earliest publication year (inclusive). None = no lower bound.
    to_year : int, optional
        Latest publication year (inclusive). None = no upper bound.
    enrich : bool
        If True, fetch individual paper pages and query OpenAlex for DOIs.

    Returns a DataFrame with CANDIDATES_COLS schema.
    """
    try:
        resp = requests.get(_REPEC_URL, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("I4R/RepEC request failed (%s) — returning 0 rows", exc)
        return pd.DataFrame(columns=CANDIDATES_COLS)

    page1_text = resp.text
    rows = _parse_repec_page(page1_text)

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

    if enrich and len(df) > 0:
        log.info("I4R: enriching %d papers (abstract + PDF + DOI)…", len(df))
        abstracts, pdf_urls, dois, oa_ids = [], [], [], []
        n_cached_detail = 0
        n_cached_oa = 0

        for i, row in df.iterrows():
            ideas_url = row["url_r"]

            # Check whether detail page is cached before sleeping
            detail_key = cache_key("i4r_detail_" + ideas_url)
            detail_cached = read_cache(_I4R_CACHE_DIR, detail_key)
            if detail_cached is None:
                time.sleep(0.3)
                detail = _fetch_i4r_paper_detail(ideas_url)
            else:
                detail = detail_cached
                n_cached_detail += 1

            abstracts.append(detail.get("abstract_r"))
            pdf_url = detail.get("pdf_url")
            # Use EconStor PDF URL as url_r (direct OA PDF, more useful than the IDEAS page)
            pdf_urls.append(pdf_url if pdf_url else ideas_url)

            # Check whether OpenAlex lookup is cached before sleeping
            oa_key = cache_key("i4r_oa_" + row["title_r"])
            oa_cached = read_cache(_I4R_CACHE_DIR, oa_key)
            if oa_cached is None:
                time.sleep(0.3)
                oa = _lookup_i4r_openalex(row["title_r"])
            else:
                oa = oa_cached
                n_cached_oa += 1

            dois.append(oa.get("doi_r"))
            oa_ids.append(oa.get("openalex_id_r"))

        df = df.copy()
        df["abstract_r"] = abstracts
        df["url_r"] = pdf_urls
        df["doi_r"] = dois
        df["openalex_id_r"] = oa_ids

        n_with_abstract = sum(1 for a in abstracts if a)
        n_with_doi = sum(1 for d in dois if d)
        log.info(
            "I4R enrichment done: %d abstracts, %d DOIs (from OpenAlex), "
            "%d detail cached, %d OA cached",
            n_with_abstract, n_with_doi, n_cached_detail, n_cached_oa,
        )

    yr = df["year_r"].dropna()
    log.info(
        "I4R: %d papers scraped (%d–%d)",
        len(df),
        int(yr.min()) if len(yr) else 0,
        int(yr.max()) if len(yr) else 0,
    )
    return df.reset_index(drop=True)
