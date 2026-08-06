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

Tier 0 (OpenAlex GROBID XML) sits above all of these: when it returns a result with
content, that IS the document and the download tiers are skipped. A tier that comes
back empty is timestamped and not re-probed for PDF_RETRY_AFTER_DAYS; so is a single
URL the server answered 404/410 for, which holds that URL back without holding back
the other URLs its tier offers.

Tier 8 requires a one-time setup:
    pip install playwright
    playwright install chromium

Public API:
    acquire_pdf(doi_r, title) → dict
        keys: pdf_url, pdf_source, pdf_path, pdf_ok, pdf_url_tried
    download_pdf(url, doi, min_bytes) → dict
        keys: success, path, source, reason
"""
import gzip
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from .config import (
    OA_CACHE_DIR, OA_XML_CACHE_DIR, OPENALEX_API_KEYS, PDF_CACHE_DIR,
    RESEARCHER_EMAIL, SERPAPI_KEY, SERPAPI_KEYS, log,
)

from .openalex_keys import (current_index, headers as oa_headers,
                            is_budget_refusal, rotate_key)
from .cache import write_json
from .rate_limit import throttle
from .utils import clean_doi, cache_key

# Seconds between calls to each source the waterfall asks. These APIs are free and
# keyless and ask only for a mailto; the intervals are politeness, not quotas, and
# they are nobody's business but this module's — these endpoints are reached from
# here and nowhere else. Each interval is taken from the one reservation queue in
# shared/rate_limit.py, because the waterfall runs on Stage 3's worker threads and
# a per-call sleep spaces nothing once there is more than one caller.
UNPAYWALL_RATE_SEC        = 0.5
_SEMANTICSCHOLAR_RATE_SEC = 1.0     # documented limit: 100 requests / 5 minutes
_CORE_RATE_SEC            = 0.6
_EUROPEPMC_RATE_SEC       = 0.3
_OPENALEX_RATE_SEC        = 0.1

# The key is optional: without it you get the polite pool (mailto= parameter);
# with it you get higher rate limits and access to content.openalex.org bulk
# endpoints. Built per request so a key rotated out mid-run is not still in use here.
def _oa_request_headers() -> dict:
    return {**oa_headers(), "Accept": "application/json"}


# ── Retry delays for tiers that came back empty ───────────────────────────────
# Whether a document can be had changes on the scale of WEEKS — a paper gets deposited
# in a repository, a publisher opens its archive, OpenAlex finishes parsing a PDF — not
# between two runs of the pipeline. data/target_pending.csv is reopened by every Stage 3
# run and almost none of its rows have a document, so each run re-paid the whole
# eleven-tier waterfall: uncached failed downloads, landing-page scrapes, a headless
# Chromium launch per row, and a metered OpenAlex content request.
#
# A recorded failure is a RETRY DELAY, never a verdict: the tier is re-probed once the
# delay lapses, and nothing is ever recorded as definitive. Two things are therefore
# never recorded — a tier skipped for a missing API key or a missing package (a key
# added tomorrow must take effect tomorrow), and any outcome of a tier that did produce
# a document.
PDF_RETRY_AFTER_DAYS    = 14
OA_XML_RETRY_AFTER_DAYS = 14

# Playwright reasons that mean "this machine cannot run the tier", not "no PDF exists".
_PLAYWRIGHT_SKIP_REASONS = {"playwright_not_installed", "no_doi"}

# Smallest byte count that can be a real article PDF; the default of every tier that
# writes one, and of the up-front "is it already on disk" check.
_MIN_PDF_BYTES = 5_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_log_path(directory: Path, key_source: str) -> Path:
    """One file per DOI/work holding {slot: iso timestamp} — not one per (row, tier)."""
    return directory / f"retry_{cache_key(key_source)}.json"


def _read_retry_log(path: Path) -> dict:
    """The recorded failure timestamps, or {} when there are none or the file is bad.

    Any failure to read degrades to "probe everything": this cache only ever suppresses
    work, so losing it costs money, while crashing on it costs the run.
    """
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        log.debug("Retry log unreadable (%s): %s", path, e)
    return {}


def _retry_suppressed(entries: dict, slot: str, after_days: float) -> bool:
    """True when *slot* failed less than *after_days* ago, so it is not re-probed yet."""
    stamp = entries.get(slot)
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(str(stamp))
    except Exception:
        return False           # unparseable stamp → re-probe
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when) < timedelta(days=after_days)


def _write_retry_log(path: Path, entries: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("Retry log write failed (%s): %s", path, e)


# ── The PDF already on disk ───────────────────────────────────────────────────
# One naming rule for the saved file and for the tier that supplied it, shared by
# download_pdf(), get_pdf_via_playwright() and acquire_pdf()'s up-front check.

def pdf_cache_path(doi_or_url: str) -> Path:
    """Where a PDF for *doi_or_url* is saved. The cache key of every tier."""
    return PDF_CACHE_DIR / f"{cache_key(doi_or_url)}.pdf"


def cached_pdf(doi_or_url: str, min_bytes: int = _MIN_PDF_BYTES) -> "Path | None":
    """The already-downloaded PDF for *doi_or_url*, or None when there is none."""
    if not doi_or_url:
        return None
    path = pdf_cache_path(doi_or_url)
    try:
        if path.exists() and path.stat().st_size >= min_bytes:
            return path
    except OSError as e:
        log.debug("PDF cache stat failed (%s): %s", path, e)
    return None


def _provenance_path(doi: str) -> Path:
    return PDF_CACHE_DIR / f"pdfsrc_{cache_key(doi)}.json"


def _read_provenance(doi: str) -> dict:
    """{"source": tier label, "url": the URL it came from} for a saved PDF, or {}."""
    try:
        path = _provenance_path(doi)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"source": str(data.get("source") or ""),
                        "url": str(data.get("url") or "")}
    except Exception as e:
        log.debug("PDF provenance unreadable for %s: %s", doi, e)
    return {}


def _write_provenance(doi: str, source: str, url: str) -> None:
    """Record which tier supplied the saved PDF, so a later run can report it."""
    if not (doi and source):
        return
    path = _provenance_path(doi)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source": source, "url": url},
                                   ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("PDF provenance write failed (%s): %s", path, e)


# ── Unpaywall ─────────────────────────────────────────────────────────────────

def _fetch_unpaywall_data(doi: str) -> Optional[dict]:
    """Fetch raw Unpaywall JSON for *doi* (cached)."""
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"unpaywall_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            return json.load(fh)

    throttle("unpaywall", UNPAYWALL_RATE_SEC)

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

    write_json(cf, data)
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
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"ss_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        throttle("semanticscholar", _SEMANTICSCHOLAR_RATE_SEC)

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

        write_json(cf, data)

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
        throttle("core", _CORE_RATE_SEC)
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

        write_json(cf, data)

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
        throttle("europepmc", _EUROPEPMC_RATE_SEC)
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

        write_json(cf, data)

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
        throttle("openalex", _OPENALEX_RATE_SEC)
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
        write_json(cf, data)

    oa = data.get("open_access") or {}
    return oa.get("oa_url") or None


# ── OpenAlex GROBID XML (Tier 0) ──────────────────────────────────────────────

def openalex_xml_has_content(oa_xml: "dict | None") -> bool:
    """True when an OpenAlex GROBID-XML result carries any text to read.

    Every one of the 60 results cached before 2026-08 was a 174-byte shell —
    every section empty, no references — and a shell is truthy, so the ladder's
    "no document" guard let it through and the row was coded as `llm_fulltext`
    from nothing at all. A result with no section text and no references is no
    document, whatever the API said about has_content.
    """
    sections = (oa_xml or {}).get("sections") or {}
    if sections.get("references"):
        return True
    return any(str(sections.get(name) or "").strip()
               for name in sections if name != "references")


def _decode_openalex_xml(response: "requests.Response") -> str:
    """Text of a content.openalex.org GROBID-XML response, gunzipped if need be.

    The endpoint serves a gzip *file* (Content-Type: application/gzip,
    Content-Disposition: …grobid.xml.gz) with no Content-Encoding header, so
    requests does not decompress it and ``response.text`` is mojibake — which
    lxml rejects and parse_tei_sections silently turns into an empty shell. The
    magic bytes are the reliable signal; the content type is the belt-and-braces.
    """
    try:
        payload = response.content or b""
        ctype   = str(response.headers.get("Content-Type", "") or "").lower()
        if payload[:2] == b"\x1f\x8b" or "gzip" in ctype:
            return gzip.decompress(payload).decode("utf-8", errors="replace")
    except Exception as e:  # not gzip after all, or a mocked response object
        log.debug("OpenAlex XML gunzip fell through: %s", e)
    return response.text


_OA_XML_RETRY_DELAYS = [1, 2, 4]  # the repo-standard 3× backoff


def _oa_xml_get(url: str, params: "dict | None" = None,
                timeout: int = 30) -> "requests.Response | None":
    """GET an OpenAlex URL with key rotation and the standard backoff.

    OPENALEX_API_KEYS is a rotation list — a key that has spent its daily budget
    refuses with a 429 while the next key still has funds, and this tier is metered
    per download, so it drains a key faster than the free endpoints do. Unlike
    Stage 3's client this never raises: the XML tier is optional, and a row with no
    OpenAlex full text falls through to the PDF waterfall rather than stopping the
    run. Returns the 200 response, or None.
    """
    attempt = 0
    # Rotating to a fresh key is not a retry of a throttled request, so it must not
    # consume the backoff budget (same reasoning as _oa_get in openalex_client).
    while attempt <= len(_OA_XML_RETRY_DELAYS):
        key_idx = current_index()
        try:
            r = requests.get(url, headers=_oa_request_headers(),
                             params=params or {}, timeout=timeout)
        except Exception as e:
            log.debug("OpenAlex request failed for %s: %s", url, e)
            return None
        if r.status_code == 200:
            return r
        if r.status_code == 429 and is_budget_refusal(r):
            if rotate_key(key_idx):
                continue
            log.warning("OpenAlex budget exhausted on every key — skipping the "
                        "GROBID-XML tier for %s", url)
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt >= len(_OA_XML_RETRY_DELAYS):
                break
            delay = _OA_XML_RETRY_DELAYS[attempt]
            log.warning("OpenAlex %s for %s — retry %d/%d in %ds", r.status_code,
                        url, attempt + 1, len(_OA_XML_RETRY_DELAYS), delay)
            time.sleep(delay)
            attempt += 1
            continue
        log.debug("OpenAlex request returned %s for %s", r.status_code, url)
        return None

    log.warning("OpenAlex request failed after retries: %s", url)
    return None


def get_openalex_fulltext(openalex_id: str) -> "dict | None":
    """
    Fetch pre-parsed GROBID XML from OpenAlex content API for a work.

    Steps:
      1. GET api.openalex.org/works/W{id}?select=has_content,content_urls
         — only proceeds when has_content.grobid_xml == true.
      2. Download content.openalex.org/works/W{id}.grobid-xml
      3. Parse TEI XML via shared.grobid.parse_tei_sections()
      4. Cache result in OA_XML_CACHE_DIR/oa_xml_{hash}.json

    Returns {"source": "openalex_xml", "sections": {...}, "xml_url": str} or None.
    Never speculatively hits content.openalex.org. A content-free result is None,
    and is not cached: it is not an answer about the paper, it is a broken fetch or
    parse, and caching it made the breakage permanent and invisible.

    COST: the content endpoint is metered — X-RateLimit-Cost-USD: 0.01 per
    download (100 credits against a $1/day free tier). Hence the two guards below:
    the tier is skipped outright without an OpenAlex key (no key means a 401 that
    still costs a request and yields nothing), and every successful fetch is
    cached, so a work is paid for once.
    """
    if not openalex_id:
        return None

    oa_id = openalex_id.strip()
    if not oa_id.startswith("W"):
        oa_id = f"W{oa_id}"

    key        = cache_key(oa_id)
    cache_file = OA_XML_CACHE_DIR / f"oa_xml_{key}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        if cached is not None:
            if openalex_xml_has_content(cached):
                return cached
            log.warning("OpenAlex XML cache for %s is a content-free shell — "
                        "ignoring it and re-fetching", oa_id)

    # A content-free answer is still never cached AS a success — but re-asking on every
    # run re-paid the metered download every run. The last content-free fetch is
    # timestamped instead, and within the delay the tier answers "no usable XML" without
    # a request; after it, the download runs again and content that appeared meanwhile
    # is picked up.
    retry_path = _retry_log_path(OA_XML_CACHE_DIR, oa_id)
    retries    = _read_retry_log(retry_path)
    if _retry_suppressed(retries, "content_free", OA_XML_RETRY_AFTER_DAYS):
        log.debug("OpenAlex XML for %s was content-free less than %d days ago — "
                  "not re-fetching yet", oa_id, OA_XML_RETRY_AFTER_DAYS)
        return None

    if not OPENALEX_API_KEYS:
        log.info("No OpenAlex API key — skipping the GROBID-XML tier for %s", oa_id)
        return None

    # Step 1 — check has_content flag
    throttle("openalex", _OPENALEX_RATE_SEC)
    r = _oa_xml_get(
        f"https://api.openalex.org/works/{oa_id}",
        params={"select": "has_content,content_urls", "mailto": RESEARCHER_EMAIL},
        timeout=15,
    )
    if r is None:
        return None
    try:
        data = r.json()
    except Exception as e:
        log.debug("OpenAlex has_content response was not JSON for %s: %s", oa_id, e)
        return None

    has_xml = (data.get("has_content") or {}).get("grobid_xml", False)
    if not has_xml:
        return None

    xml_url = (data.get("content_urls") or {}).get(
        "grobid_xml",
        f"https://content.openalex.org/works/{oa_id}.grobid-xml",
    )

    # Step 2 — download the XML (this is the metered request)
    r2 = _oa_xml_get(xml_url)
    if r2 is None:
        return None
    try:
        xml_text = _decode_openalex_xml(r2)
    except Exception as e:
        log.debug("OpenAlex XML decode failed for %s: %s", oa_id, e)
        return None

    # Step 3 — parse using the existing TEI parser
    try:
        from .grobid import parse_tei_sections
        sections = parse_tei_sections(xml_text)
    except Exception as e:
        log.debug("OpenAlex XML parse failed for %s: %s", oa_id, e)
        return None

    result = {"source": "openalex_xml", "sections": sections, "xml_url": xml_url}

    if not openalex_xml_has_content(result):
        log.warning("OpenAlex reported grobid_xml for %s but the parsed result is "
                    "empty (no sections, no references) — treating it as no document",
                    oa_id)
        _write_retry_log(retry_path, {**retries, "content_free": _now_iso()})
        return None

    # Step 4 — cache
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.debug("OpenAlex XML cache write failed: %s", e)
    if retries:
        # Content arrived; the delay that was holding the re-fetch back has done its job.
        try:
            retry_path.unlink(missing_ok=True)
        except Exception as e:
            log.debug("Retry log delete failed (%s): %s", retry_path, e)

    log.info("OpenAlex XML acquired for %s (%d refs)", oa_id,
             len(sections.get("references", [])))
    return result


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
        write_json(cf, results)

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
    pdf_path = pdf_cache_path(doi)
    have     = cached_pdf(doi, min_bytes)
    if have is not None:
        return {"success": True, "path": have, "source": "cache", "reason": ""}

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

# HTTP statuses that are EVIDENCE OF ABSENCE for this URL: the server answered, and
# its answer was that there is no document here. Nothing else qualifies — a timeout, a
# connection error, a 429 and every 5xx are the server failing to answer, and a 401/403
# is a refusal to serve a document that does exist. Recording one of those would
# checkpoint a transient failure as a definitive miss.
_PERMANENT_HTTP_STATUS = {404, 410}


def _url_failure_path(url: str) -> Path:
    """The record of a permanently dead URL. Prefixed so it cannot collide with a DOI."""
    return _retry_log_path(PDF_CACHE_DIR, f"url:{url}")


def _url_is_gone(url: str) -> bool:
    """True when this URL answered "no document here" less than the window ago.

    The per-tier record in acquire_pdf holds a whole tier back for a DOI; this holds one
    dead URL back, so the OTHER URLs a tier offers are still tried. Both are retry
    delays on the same PDF_RETRY_AFTER_DAYS window, never verdicts: the file only ever
    holds statuses from _PERMANENT_HTTP_STATUS, so any live stamp in it suppresses.
    """
    entries = _read_retry_log(_url_failure_path(url))
    return any(_retry_suppressed(entries, slot, PDF_RETRY_AFTER_DAYS)
               for slot in entries)


def download_pdf(url: str, doi: str = "", min_bytes: int = _MIN_PDF_BYTES) -> dict:
    """
    Download a PDF and save to PDF_CACHE_DIR.

    Cache key = MD5 of doi (or url if doi missing), so repeat calls skip download.

    Returns: {"success", "path", "source", "reason"}
    """
    if not url:
        return {"success": False, "path": None, "source": "", "reason": "no_url"}

    pdf_path = pdf_cache_path(doi or url)
    have     = cached_pdf(doi or url, min_bytes)
    if have is not None:
        return {"success": True, "path": have, "source": "cache", "reason": ""}

    if _url_is_gone(url):
        log.debug("  URL answered gone less than %d days ago — not re-fetching: %s",
                  PDF_RETRY_AFTER_DAYS, url)
        return {"success": False, "path": None, "source": "", "reason": "url_gone"}

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
        if r.status_code in _PERMANENT_HTTP_STATUS:
            _write_retry_log(_url_failure_path(url),
                             {f"http_{r.status_code}": _now_iso()})
            return {"success": False, "path": None, "source": "",
                    "reason": f"http_{r.status_code}"}
        r.raise_for_status()
        content = b"".join(r.iter_content(chunk_size=65_536))

        if not content.startswith(b"%PDF"):
            return {"success": False, "path": None, "source": "", "reason": "not_a_pdf"}
        if len(content) < min_bytes:
            return {"success": False, "path": None, "source": "", "reason": "file_too_small"}

        pdf_path.write_bytes(content)
        try:                     # the URL serves a document after all
            _url_failure_path(url).unlink(missing_ok=True)
        except Exception as e:
            log.debug("URL failure record delete failed (%s): %s", url, e)
        return {"success": True, "path": pdf_path, "source": "download", "reason": ""}

    except Exception as e:
        return {"success": False, "path": None, "source": "",
                "reason": f"download_error: {e}"}


# ── Orchestrator ──────────────────────────────────────────────────────────────

def acquire_pdf(doi_r: str, title: str = "", openalex_id: str = "") -> dict:
    """
    Try every PDF source in priority order for *doi_r*.

    Returns:
        pdf_url        str
        pdf_source     str
        pdf_path       str | None
        pdf_ok         bool
        pdf_url_tried  list[str]
        openalex_xml   dict | None  — structured content from OpenAlex GROBID XML (Tier 0)
    """
    doi_r     = clean_doi(doi_r)
    dl        = {"success": False, "path": None, "reason": ""}
    pdf_url   = ""
    pdf_src   = ""
    all_tried: list[str] = []
    oa_xml    = None

    # Per-DOI record of which tiers came back empty, and when (PDF_RETRY_AFTER_DAYS).
    retry_path = _retry_log_path(PDF_CACHE_DIR, doi_r) if doi_r else None
    retries    = _read_retry_log(retry_path) if retry_path is not None else {}
    failures: dict[str, str] = {}

    def _held(tier: str) -> bool:
        return _retry_suppressed(retries, tier, PDF_RETRY_AFTER_DAYS)

    def _failed(tier: str) -> None:
        failures[tier] = _now_iso()

    def _try(url: str, label: str) -> bool:
        nonlocal dl, pdf_url, pdf_src
        all_tried.append(url)
        dl = download_pdf(url, doi=doi_r)
        if dl["success"]:
            pdf_url, pdf_src = url, label
            # Also written on a download_pdf cache hit, which is how a PDF saved
            # before this record existed acquires one.
            _write_provenance(doi_r, label, url)
            return True
        log.debug("  %s failed (%s): %s", label, dl.get("reason"), url)
        return False

    def _result() -> dict:
        if retry_path is not None:
            # An XML with content is a document too: if its cache is later lost,
            # the download tiers must be probeable again, not held for two weeks.
            if dl["success"] or pdf_src == "openalex_xml":
                try:
                    retry_path.unlink(missing_ok=True)
                except Exception as e:
                    log.debug("Retry log delete failed (%s): %s", retry_path, e)
            elif failures:
                _write_retry_log(retry_path, {**retries, **failures})
        return {
            "pdf_url"       : pdf_url,
            "pdf_source"    : pdf_src if dl["success"] else (pdf_src or "none"),
            "pdf_path"      : str(dl["path"]) if dl.get("path") else None,
            "pdf_ok"        : dl["success"],
            "pdf_url_tried" : all_tried,
            "openalex_xml"  : oa_xml,
        }

    # Tier 0 — OpenAlex GROBID XML (structured content, no PDF file needed)
    # A result with content IS the document: link_original parses it exactly as it
    # parses a downloaded PDF, so running the ten download tiers underneath it buys
    # nothing and costs the whole waterfall.
    # The AUTHORITATIVE content-free-XML guard: a shell never leaves acquire_pdf as a
    # document, and get_openalex_fulltext neither returns nor caches one as a success.
    if openalex_id:
        oa_xml = get_openalex_fulltext(openalex_id)
        if oa_xml and openalex_xml_has_content(oa_xml):
            log.info("  [%s] OpenAlex XML acquired (source=openalex_xml) — "
                     "skipping the download tiers", doi_r)
            pdf_src = "openalex_xml"
            return _result()
        oa_xml = None

    # The PDF is already on disk — no tier can add anything to a file we have.
    # This used to happen by accident, inside the download_pdf() cache hit of whichever
    # tier first re-derived a URL for the DOI, so which tier "supplied" the document
    # depended on the tier order and on every URL lookup above it. The tier that really
    # supplied it is recorded next to the file, and replayed here. A PDF saved before
    # that record existed has none and falls through to the waterfall as before — where
    # the first cache hit writes the record for next time.
    if doi_r and cached_pdf(doi_r) is not None:
        prov = _read_provenance(doi_r)
        if prov.get("source"):
            dl      = {"success": True, "path": pdf_cache_path(doi_r),
                       "source": "cache", "reason": ""}
            pdf_url = prov["url"]
            pdf_src = prov["source"]
            if pdf_url:
                all_tried.append(pdf_url)
            log.debug("  [%s] PDF already on disk (source=%s)", doi_r, pdf_src)
            return _result()

    # Tier 1 — arXiv direct (before any API calls; the URL is a DOI pattern, so a
    # non-arXiv DOI is not a tier failure — there was nothing to ask.)
    arxiv = get_arxiv_pdf_url(doi_r, title)
    if arxiv and not _held("arxiv"):
        if not _try(arxiv, "arxiv"):
            _failed("arxiv")

    # Tier 2 — OSF preprint
    if not dl["success"] and not _held("osf"):
        osf = get_osf_pdf_url(doi_r)
        if osf and not _try(osf, "osf"):
            _failed("osf")

    # Tier 3 — OpenAlex OA URL
    if not dl["success"] and not _held("openalex_oa"):
        oa_url = get_openalex_oa_url(doi_r)
        if not (oa_url and _try(oa_url, "openalex_oa")):
            _failed("openalex_oa")

    # Tier 4 — Unpaywall direct PDFs
    # Every other tier is guarded by `if not dl["success"]`; this one was not, so a
    # DOI already served by arXiv/OSF/OpenAlex still cost an Unpaywall round-trip.
    # Tier 8 is the only other consumer of uw_landing/uw_direct and it sits inside
    # an `if not dl["success"]` block, so the empty list is never reached.
    need_unpaywall = (not dl["success"]
                      and not (_held("unpaywall_pdf") and _held("landing")))
    uw_all     = get_all_unpaywall_pdf_urls(doi_r) if need_unpaywall else []
    uw_direct  = [u for u in uw_all if u["type"] == "pdf"]
    uw_landing = [u for u in uw_all if u["type"] == "landing"]

    if not dl["success"] and not _held("unpaywall_pdf"):
        if not any(_try(cand["url"], "unpaywall_pdf") for cand in uw_direct):
            _failed("unpaywall_pdf")

    # Tier 5 — SemanticScholar
    if not dl["success"] and not _held("semanticscholar"):
        ss = get_semanticscholar_pdf_url(doi_r)
        if not (ss and _try(ss, "semanticscholar")):
            _failed("semanticscholar")

    # Tier 6 — CORE
    if not dl["success"] and not _held("core"):
        core = get_core_pdf_url(doi_r)
        if not (core and _try(core, "core")):
            _failed("core")

    # Tier 7 — Europe PMC
    if not dl["success"] and not _held("europepmc"):
        epmc = get_europepmc_pdf_url(doi_r)
        if not (epmc and _try(epmc, "europepmc")):
            _failed("europepmc")

    # Tier 8 — Scrape Unpaywall landing pages
    if not dl["success"] and not _held("landing"):
        won = False
        for cand in uw_landing:
            scraped = scrape_pdf_from_landing_page(cand["url"])
            if scraped and _try(scraped, f"landing_{cand['host'] or 'repo'}"):
                won = True
                break
        if not won:
            _failed("landing")

    # Tier 9 — SerpAPI (quota-limited, last HTTP resort before browser)
    # No key is a SKIP, not a failure: a key added tomorrow must be used tomorrow.
    if not dl["success"] and not _held("serpapi"):
        if not SERPAPI_KEYS:
            log.debug("  [%s] no SerpAPI key — tier skipped, not recorded", doi_r)
        else:
            serp = get_serpapi_pdf_url(doi_r, title)
            if not (serp and _try(serp, "serpapi")):
                _failed("serpapi")

    # Tier 10 — Playwright headless Chromium
    if not dl["success"] and not _held("playwright"):
        log.info("  [%s] All HTTP tiers failed — trying Playwright headless", doi_r)
        pw_result = get_pdf_via_playwright(doi_r)
        if pw_result["success"]:
            pdf_url = f"https://doi.org/{doi_r}"
            pdf_src = "playwright"
            dl      = pw_result
            all_tried.append(pdf_url)
            _write_provenance(doi_r, "playwright", pdf_url)
        elif pw_result.get("reason") not in _PLAYWRIGHT_SKIP_REASONS:
            _failed("playwright")

    return _result()
