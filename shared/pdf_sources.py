"""
pdf_sources.py — Multi-tier PDF acquisition.

Acquisition order:
  1. OSF preprint direct download  (DOI-pattern based, no API)
  2. Unpaywall — all direct PDF URLs
  3. SemanticScholar open-access PDF
  4. CORE.ac.uk aggregator
  5. Europe PMC
  6. Unpaywall landing-page scraper (HTML scraping for repo pages)
  7. SerpAPI / Google Scholar      (consumes quota, last resort)
  8. Playwright headless Chromium  (bypasses JS-rendered paywalls)

Tier 8 requires a one-time setup:
    pip install playwright
    playwright install chromium

Public API:
    acquire_pdf(doi_r, title) → dict
        keys: pdf_url, pdf_source, pdf_path, pdf_ok, pdf_url_tried
    download_pdf(url, doi, min_bytes) → dict
        keys: success, path, source, reason
"""
import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .config import (
    OA_CACHE_DIR, PDF_CACHE_DIR, RESEARCHER_EMAIL,
    SERPAPI_KEY, SERPAPI_KEYS, UNPAYWALL_RATE_SEC, log,
)
from .utils import clean_doi, cache_key

# ── Shared rate-limit state ───────────────────────────────────────────────────
_unpaywall_last = 0.0
_ss_last        = 0.0


# ── Unpaywall ─────────────────────────────────────────────────────────────────

def _fetch_unpaywall_data(doi: str) -> Optional[dict]:
    """Fetch raw Unpaywall JSON for *doi* (cached)."""
    global _unpaywall_last
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"unpaywall_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            return json.load(fh)

    wait = UNPAYWALL_RATE_SEC - (time.time() - _unpaywall_last)
    if wait > 0:
        time.sleep(wait)
    _unpaywall_last = time.time()

    try:
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": RESEARCHER_EMAIL},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        log.debug("Unpaywall error for %s: %s", doi, e)
        return None

    with cf.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return data


def get_all_unpaywall_pdf_urls(doi: str) -> list[dict]:
    """
    Return ALL open-access PDF candidates for *doi* from Unpaywall, ordered:
      1. best_oa_location direct PDF
      2. other oa_locations direct PDFs
      3. best_oa_location landing page
      4. other oa_locations landing pages

    Each item: {"url", "type": "pdf"|"landing", "host", "license"}
    """
    data = _fetch_unpaywall_data(doi)
    if not data:
        return []

    seen:    set[str]   = set()
    results: list[dict] = []

    def _add(url, url_type, host, license_):
        if url and url not in seen:
            seen.add(url)
            results.append({"url": url, "type": url_type,
                            "host": host or "", "license": license_ or ""})

    best = data.get("best_oa_location") or {}
    _add(best.get("url_for_pdf"), "pdf",     best.get("host_type"), best.get("license"))
    for loc in data.get("oa_locations", []):
        _add(loc.get("url_for_pdf"), "pdf",  loc.get("host_type"), loc.get("license"))
    _add(best.get("url"),           "landing", best.get("host_type"), best.get("license"))
    for loc in data.get("oa_locations", []):
        _add(loc.get("url"),        "landing", loc.get("host_type"), loc.get("license"))

    return results


# ── SemanticScholar ───────────────────────────────────────────────────────────

def get_semanticscholar_pdf_url(doi: str) -> Optional[str]:
    """
    Query Semantic Scholar Graph API for an open-access PDF URL.
    No API key required. Rate limit: 100 req/5 min → sleep 1 s between calls.
    """
    global _ss_last
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"ss_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        wait = 1.0 - (time.time() - _ss_last)
        if wait > 0:
            time.sleep(wait)
        _ss_last = time.time()

        try:
            r = requests.get(
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                params={"fields": "openAccessPdf,externalIds"},
                headers={"User-Agent": f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})"},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as e:
            log.debug("SemanticScholar error for %s: %s", doi, e)
            return None

        with cf.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    return (data.get("openAccessPdf") or {}).get("url")


# ── CORE.ac.uk ────────────────────────────────────────────────────────────────

def get_core_pdf_url(doi: str) -> Optional[str]:
    """Query CORE.ac.uk for a downloadable PDF URL. No API key needed."""
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"core_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        time.sleep(0.6)
        try:
            r = requests.get(
                "https://api.core.ac.uk/v3/works",
                params={"q": f'doi:"{doi}"', "limit": 1},
                headers={"User-Agent": f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})"},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as e:
            log.debug("CORE error for %s: %s", doi, e)
            return None

        with cf.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    for item in (data.get("results") or []):
        url = item.get("downloadUrl") or item.get("fullTextUrl")
        if url:
            return url
    return None


# ── Europe PMC ────────────────────────────────────────────────────────────────

def get_europepmc_pdf_url(doi: str) -> Optional[str]:
    """Query Europe PMC for a PMC full-text PDF URL."""
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"epmc_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        time.sleep(0.3)
        try:
            r = requests.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": f'DOI:"{doi}"', "format": "json",
                        "resultType": "core", "pageSize": 1},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as e:
            log.debug("EuropePMC error for %s: %s", doi, e)
            return None

        with cf.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    for item in ((data.get("resultList") or {}).get("result") or []):
        pmc_id = item.get("pmcid", "")
        if pmc_id:
            return (f"https://europepmc.org/backend/ptpmcrender.fcgi"
                    f"?accid={pmc_id}&blobtype=pdf")
    return None


# ── OSF preprint ──────────────────────────────────────────────────────────────

def get_osf_pdf_url(doi: str) -> Optional[str]:
    """
    Construct a direct OSF download URL from a preprint DOI.
    Covers: 10.31234/osf.io/{id}  (PsyArXiv),
            10.31235/osf.io/{id}  (SocArXiv), etc.
    """
    doi = clean_doi(doi)
    if not doi:
        return None
    m = re.match(r"^10\.3123\d/osf\.io/([a-z0-9]+)$", doi, re.IGNORECASE)
    if m:
        return f"https://osf.io/download/{m.group(1)}/"
    return None


# ── arXiv ─────────────────────────────────────────────────────────────────────

def get_arxiv_pdf_url(doi: str, title: str = "") -> Optional[str]:
    """
    Return a direct arXiv PDF URL if the DOI or title indicates an arXiv paper.
    Handles DOIs like 10.48550/arXiv.2301.12345 and arXiv:2301.12345 patterns.
    """
    doi = clean_doi(doi)

    # DOI-based arXiv detection (e.g. 10.48550/arXiv.2301.12345)
    m = re.match(r"^10\.48550/arxiv\.(\d{4}\.\d{4,5})$", doi, re.IGNORECASE)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"

    # Title-based detection: look for "arXiv:XXXX.XXXXX" pattern
    if title:
        m = re.search(r"arxiv[:\s]+(\d{4}\.\d{4,5})", title, re.IGNORECASE)
        if m:
            return f"https://arxiv.org/pdf/{m.group(1)}"

    return None


# ── OpenAlex OA URL ───────────────────────────────────────────────────────────

def get_openalex_oa_url(doi: str) -> Optional[str]:
    """
    Query OpenAlex for the open_access.oa_url field for this DOI.
    Returns the OA PDF/landing URL, or None.
    Cached in OA_CACHE_DIR as oa_<hash>.json.
    """
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"oa_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        time.sleep(0.1)
        try:
            r = requests.get(
                f"https://api.openalex.org/works/doi:{doi}",
                params={"select": "open_access", "mailto": RESEARCHER_EMAIL},
                headers={"User-Agent": f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})"},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception as e:
            log.debug("OpenAlex OA URL error for %s: %s", doi, e)
            return None
        with cf.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    oa = data.get("open_access") or {}
    return oa.get("oa_url") or None


# ── Landing-page HTML scraper ─────────────────────────────────────────────────

def scrape_pdf_from_landing_page(landing_url: str) -> Optional[str]:
    """
    Scrape an institutional repository landing page for a direct PDF link.
    Covers HAL, DSpace, Pure, and generic repos.
    """
    if not landing_url:
        return None
    try:
        r = requests.get(
            landing_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; academic research bot)",
                     "Accept"    : "text/html,application/xhtml+xml"},
            timeout=20,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        html     = r.text
        base     = re.match(r"https?://[^/]+", landing_url)
        base_url = base.group(0) if base else ""

        pdf_links: list[str] = []
        for pat in [
            r'href=["\']([^"\']+\.pdf[^"\']*)["\']',          # direct .pdf href
            r'href=["\']([^"\']+/document)["\']',              # HAL /document
            r'href=["\']([^"\']+/bitstream/[^"\']+)["\']',     # DSpace bitstream
            r'href=["\']([^"\']*download[^"\']*\.pdf[^"\']*)["\']',  # generic download
        ]:
            for m in re.finditer(pat, html, re.IGNORECASE):
                pdf_links.append(m.group(1))

        resolved: list[str] = []
        seen:     set[str]  = set()
        for link in pdf_links:
            url = (link if link.startswith("http")
                   else ("https:" + link if link.startswith("//")
                         else base_url + link))
            if url not in seen:
                seen.add(url)
                resolved.append(url)

        main = [u for u in resolved
                if not re.search(r"(?i)supplement|appendix|supp_|_s\d", u)]
        return (main or resolved)[0] if (main or resolved) else None

    except Exception as e:
        log.debug("Landing-page scrape failed (%s): %s", landing_url, e)
        return None


# ── HTML text extraction fallback ─────────────────────────────────────────────

def extract_html_text_as_fulltext(url: str, doi: str = "") -> Optional[str]:
    """
    Download a URL that returned HTML (not PDF) and extract visible text.
    Useful for landing pages (PsyArXiv, OSF, some journals) that expose
    the abstract and sometimes the full text in HTML.

    Saves extracted text to PDF_CACHE_DIR/<hash>.txt (max 50 000 chars).
    Returns the text, or None if extraction fails or yields too little text.
    """
    key        = cache_key(doi or url)
    cache_file = PDF_CACHE_DIR / f"{key}.txt"

    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; academic research bot)",
                "Accept"    : "text/html,application/xhtml+xml",
            },
            timeout=20,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        ct = r.headers.get("content-type", "")
        if "html" not in ct.lower():
            return None

        from lxml import etree
        parser = etree.HTMLParser()
        tree   = etree.fromstring(r.content, parser)

        # Remove script, style, nav, footer to reduce noise
        for tag in tree.xpath("//script | //style | //nav | //footer | //header"):
            parent = tag.getparent()
            if parent is not None:
                parent.remove(tag)

        raw  = " ".join(tree.xpath("//text()"))
        text = re.sub(r"\s+", " ", raw).strip()

        if len(text) < 300:   # too little content to be useful
            return None

        text = text[:50_000]
        cache_file.write_text(text, encoding="utf-8")
        log.info("HTML text extracted (%d chars) from %s", len(text), url)
        return text

    except Exception as e:
        log.debug("HTML text extraction failed (%s): %s", url, e)
        return None


# ── SerpAPI ───────────────────────────────────────────────────────────────────

def get_serpapi_pdf_url(doi: str, title: str = "") -> Optional[str]:
    """
    Search Google Scholar via SerpAPI for a PDF link.
    Rotates through SERPAPI_KEYS on 429 or quota errors.
    Returns first PDF URL found, or None.
    """
    if not SERPAPI_KEYS:
        return None

    query = f'"{doi}"' if doi else f'"{title}"'
    cf    = OA_CACHE_DIR / f"serp_{cache_key(query)}.json"

    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            results = json.load(fh)
    else:
        results = None
        for key_idx, api_key in enumerate(SERPAPI_KEYS):
            key_label = f"key {key_idx+1}/{len(SERPAPI_KEYS)}"
            try:
                r = requests.get(
                    "https://serpapi.com/search",
                    params={"engine": "google_scholar", "q": query,
                            "api_key": api_key, "num": "5"},
                    timeout=20,
                )
                if r.status_code == 429:
                    log.warning("SerpAPI quota exhausted on %s", key_label)
                    continue
                if r.status_code != 200:
                    log.warning("SerpAPI HTTP %s on %s", r.status_code, key_label)
                    continue
                body = r.json()
                # quota error returned as 200 with error field
                if "error" in body and "quota" in body["error"].lower():
                    log.warning("SerpAPI quota error on %s: %s", key_label, body["error"])
                    continue
                results = body
                break
            except Exception as e:
                log.warning("SerpAPI exception on %s: %s", key_label, e)

        if results is None:
            return None
        with cf.open("w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False)

    for organic in results.get("organic_results", []):
        for res in organic.get("resources", []):
            link = res.get("link", "")
            if link.lower().endswith(".pdf") or "pdf" in link.lower():
                return link
    return None


# ── Playwright headless browser ───────────────────────────────────────────────

# CSS selectors tried in order on publisher landing pages.
# Most specific (publisher-branded) first, generic fallbacks last.
_PDF_SELECTORS = [
    # Elsevier / ScienceDirect
    "a.pdf-download-btn-link",
    "a[data-aa-name='btn-download-pdf']",
    # Springer / Nature
    "a.c-pdf-download__link",
    "a[data-track-action='download pdf']",
    # Wiley
    "a.pdf-download",
    "a[href*='/doi/pdf/']",
    "a[href*='/doi/epdf/']",
    # Taylor & Francis
    "a[href*='/doi/pdf/10.']",
    # APA PsycNet
    "a[data-test='download-pdf']",
    # Cambridge
    "a.btn--pdf",
    # Oxford University Press
    "a.al-link.pdf",
    # SAGE
    "a[href*='/doi/pdf/']",
    # Generic fallbacks
    "a[href$='.pdf']",
    "a[href*='/pdf/']",
    "a[href*='=pdf']",
    "a:has-text('Download PDF')",
    "a:has-text('Full Text PDF')",
    "a:has-text('View PDF')",
    "button:has-text('Download PDF')",
]

# Publisher domains that require a real browser (bot-detection / JS rendering)
_HEADLESS_DOMAINS = {
    "elsevier.com", "sciencedirect.com", "springer.com", "springerlink.com",
    "nature.com", "wiley.com", "onlinelibrary.wiley.com", "tandfonline.com",
    "apa.org", "psycnet.apa.org", "cambridge.org", "oup.com",
    "sagepub.com", "informs.org", "pnas.org", "science.org",
    "jneurosci.org", "cell.com", "thelancet.com", "bmj.com",
}


def _is_headless_candidate(doi: str) -> bool:
    """
    True for DOIs whose publishers are known to require a real browser.
    Also returns True when doi is empty (so the DOI landing page is always tried).
    """
    if not doi:
        return True
    doi_lower = doi.lower()
    # Springer: 10.1007, Nature: 10.1038, Wiley: 10.1002, Taylor: 10.1080
    headless_prefixes = ("10.1007/", "10.1038/", "10.1002/", "10.1080/",
                         "10.1037/", "10.1016/", "10.1017/", "10.1093/",
                         "10.1177/", "10.1111/")
    return any(doi_lower.startswith(p) for p in headless_prefixes)


def get_pdf_via_playwright(doi: str, min_bytes: int = 5_000) -> dict:
    """
    Launch a headless Chromium browser, navigate to the DOI landing page,
    and attempt to download a PDF by:
      1. Intercepting any network response whose Content-Type is application/pdf
      2. Clicking the first matching PDF download link/button

    Returns the same dict shape as download_pdf():
        {"success", "path", "source": "playwright", "reason"}

    Requires:  pip install playwright && playwright install chromium
    """
    doi = clean_doi(doi)
    if not doi:
        return {"success": False, "path": None, "source": "", "reason": "no_doi"}

    # Check cache first — if a PDF was already saved for this DOI, skip browser
    key      = cache_key(doi)
    pdf_path = PDF_CACHE_DIR / f"{key}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size >= min_bytes:
        return {"success": True, "path": pdf_path, "source": "cache", "reason": ""}

    # On Windows, threads (including Jupyter worker threads) use SelectorEventLoop
    # by default, which cannot launch subprocesses.  Switch to ProactorEventLoop
    # so Playwright can spawn its Chromium driver process.
    import sys as _sys
    if _sys.platform == "win32":
        import asyncio as _aio
        try:
            _aio.set_event_loop_policy(_aio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.info("Playwright not installed — skipping headless tier "
                 "(run: pip install playwright && playwright install chromium)")
        return {"success": False, "path": None, "source": "",
                "reason": "playwright_not_installed"}

    captured: dict = {"bytes": None, "url": ""}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = ctx.new_page()

        # ── Intercept PDF responses sent inline (Content-Type: application/pdf) ─
        def _on_response(response):
            if captured["bytes"]:
                return
            ct = response.headers.get("content-type", "")
            if "application/pdf" in ct:
                try:
                    captured["bytes"] = response.body()
                    captured["url"]   = response.url
                    log.debug("Playwright intercepted inline PDF: %s", response.url)
                except Exception:
                    pass

        page.on("response", _on_response)

        # ── Navigate to the DOI landing page ─────────────────────────────────
        landing = f"https://doi.org/{doi}"
        try:
            page.goto(landing, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)   # let JS render
        except PWTimeout:
            log.debug("Playwright: page load timeout for %s", doi)

        # ── If inline PDF was served directly, we already have bytes ──────────
        if captured["bytes"] and captured["bytes"][:4] == b"%PDF":
            pdf_path.write_bytes(captured["bytes"])
            ctx.close(); browser.close()
            return {"success": True, "path": pdf_path,
                    "source": "playwright", "reason": ""}

        # ── Try clicking a download link / button ─────────────────────────────
        for selector in _PDF_SELECTORS:
            try:
                el = page.query_selector(selector)
                if el is None:
                    continue

                href = el.get_attribute("href") or ""

                # If href points directly to a PDF URL, download it with requests
                if href and (".pdf" in href.lower() or "/pdf/" in href.lower()
                             or "=pdf" in href.lower()):
                    if href.startswith("/"):
                        # Resolve relative URL against current page origin
                        origin = re.match(r"https?://[^/]+", page.url)
                        href   = (origin.group(0) if origin else "") + href
                    if href.startswith("http"):
                        # Download via normal requests (has cookies from ctx if needed)
                        try:
                            raw = requests.get(
                                href,
                                headers={"User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                                )},
                                timeout=60,
                                stream=True,
                            )
                            content = b"".join(raw.iter_content(65_536))
                            if content[:4] == b"%PDF" and len(content) >= min_bytes:
                                pdf_path.write_bytes(content)
                                ctx.close(); browser.close()
                                return {"success": True, "path": pdf_path,
                                        "source": "playwright", "reason": ""}
                        except Exception:
                            pass

                # Otherwise click and wait for a download event
                with ctx.expect_download(timeout=20_000) as dl_info:
                    el.click()
                download = dl_info.value
                tmp      = download.path()
                if tmp:
                    content = Path(tmp).read_bytes()
                    if content[:4] == b"%PDF" and len(content) >= min_bytes:
                        pdf_path.write_bytes(content)
                        ctx.close(); browser.close()
                        return {"success": True, "path": pdf_path,
                                "source": "playwright", "reason": ""}

            except PWTimeout:
                log.debug("Playwright: download timeout for selector '%s'", selector)
            except Exception as e:
                log.debug("Playwright: selector '%s' failed: %s", selector, e)

        ctx.close()
        browser.close()

    # Check once more — the response interceptor may have fired after a click
    if captured["bytes"] and captured["bytes"][:4] == b"%PDF":
        pdf_path.write_bytes(captured["bytes"])
        return {"success": True, "path": pdf_path,
                "source": "playwright", "reason": ""}

    return {"success": False, "path": None, "source": "",
            "reason": "playwright_no_pdf_found"}


# ── Download helper ───────────────────────────────────────────────────────────

def download_pdf(url: str, doi: str = "", min_bytes: int = 5_000) -> dict:
    """
    Download a PDF and save to PDF_CACHE_DIR.

    Cache key = MD5 of doi (or url if doi missing), so repeat calls skip download.

    Returns: {"success", "path", "source", "reason"}
    """
    if not url:
        return {"success": False, "path": None, "source": "", "reason": "no_url"}

    key      = cache_key(doi or url)
    pdf_path = PDF_CACHE_DIR / f"{key}.pdf"

    if pdf_path.exists() and pdf_path.stat().st_size >= min_bytes:
        return {"success": True, "path": pdf_path, "source": "cache", "reason": ""}

    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*;q=0.9",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
            },
            timeout=60,
            stream=True,
        )
        r.raise_for_status()
        content = b"".join(r.iter_content(chunk_size=65_536))

        if not content.startswith(b"%PDF"):
            return {"success": False, "path": None, "source": "", "reason": "not_a_pdf"}
        if len(content) < min_bytes:
            return {"success": False, "path": None, "source": "", "reason": "file_too_small"}

        pdf_path.write_bytes(content)
        return {"success": True, "path": pdf_path, "source": "download", "reason": ""}

    except Exception as e:
        return {"success": False, "path": None, "source": "",
                "reason": f"download_error: {e}"}


# ── Orchestrator ──────────────────────────────────────────────────────────────

def acquire_pdf(doi_r: str, title: str = "") -> dict:
    """
    Try every PDF source in priority order for *doi_r*.

    Returns:
        pdf_url        str
        pdf_source     str
        pdf_path       str | None
        pdf_ok         bool
        pdf_url_tried  list[str]
        html_text      str  — extracted landing-page text when PDF unavailable
    """
    doi_r     = clean_doi(doi_r)
    dl        = {"success": False, "path": None, "reason": ""}
    pdf_url   = ""
    pdf_src   = ""
    all_tried: list[str] = []

    def _try(url: str, label: str) -> bool:
        nonlocal dl, pdf_url, pdf_src
        all_tried.append(url)
        dl = download_pdf(url, doi=doi_r)
        if dl["success"]:
            pdf_url, pdf_src = url, label
            return True
        log.debug("  %s failed (%s): %s", label, dl.get("reason"), url)
        return False

    # Tier 1 — arXiv direct (before any API calls)
    arxiv = get_arxiv_pdf_url(doi_r, title)
    if arxiv and _try(arxiv, "arxiv"):
        pass

    # Tier 2 — OSF preprint
    if not dl["success"]:
        osf = get_osf_pdf_url(doi_r)
        if osf and _try(osf, "osf"):
            pass

    # Tier 3 — OpenAlex OA URL
    if not dl["success"]:
        oa_url = get_openalex_oa_url(doi_r)
        if oa_url and _try(oa_url, "openalex_oa"):
            pass

    # Tier 4 — Unpaywall direct PDFs
    uw_all     = get_all_unpaywall_pdf_urls(doi_r)
    uw_direct  = [u for u in uw_all if u["type"] == "pdf"]
    uw_landing = [u for u in uw_all if u["type"] == "landing"]

    if not dl["success"]:
        for cand in uw_direct:
            if _try(cand["url"], "unpaywall_pdf"):
                break

    # Tier 5 — SemanticScholar
    if not dl["success"]:
        ss = get_semanticscholar_pdf_url(doi_r)
        if ss:
            _try(ss, "semanticscholar")

    # Tier 6 — CORE
    if not dl["success"]:
        core = get_core_pdf_url(doi_r)
        if core:
            _try(core, "core")

    # Tier 7 — Europe PMC
    if not dl["success"]:
        epmc = get_europepmc_pdf_url(doi_r)
        if epmc:
            _try(epmc, "europepmc")

    # Tier 8 — Scrape Unpaywall landing pages
    if not dl["success"]:
        for cand in uw_landing:
            scraped = scrape_pdf_from_landing_page(cand["url"])
            if scraped and _try(scraped, f"landing_{cand['host'] or 'repo'}"):
                break

    # Tier 9 — SerpAPI (quota-limited, last HTTP resort before browser)
    if not dl["success"]:
        serp = get_serpapi_pdf_url(doi_r, title)
        if serp:
            _try(serp, "serpapi")

    # Tier 10 — Playwright headless Chromium
    if not dl["success"]:
        log.info("  [%s] All HTTP tiers failed — trying Playwright headless", doi_r)
        pw_result = get_pdf_via_playwright(doi_r)
        if pw_result["success"]:
            pdf_url = f"https://doi.org/{doi_r}"
            pdf_src = "playwright"
            dl      = pw_result
            all_tried.append(pdf_url)

    # Tier 11 — HTML text extraction (fallback when no PDF available)
    # If all PDF tiers failed but we have a URL, extract visible text.
    html_text = ""
    if not dl["success"]:
        best_url = (uw_landing[0]["url"] if uw_landing else None) or \
                   (uw_direct[0]["url"] if uw_direct else None)
        if best_url:
            html_text = extract_html_text_as_fulltext(best_url, doi_r) or ""
            if html_text:
                log.info("  [%s] HTML text fallback: %d chars", doi_r, len(html_text))

    return {
        "pdf_url"       : pdf_url,
        "pdf_source"    : pdf_src if dl["success"] else ("html_text" if html_text else "none"),
        "pdf_path"      : str(dl["path"]) if dl.get("path") else None,
        "pdf_ok"        : dl["success"],
        "pdf_url_tried" : all_tried,
        "html_text"     : html_text,
    }
