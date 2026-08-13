"""
pdf_sources.py — Multi-tier PDF acquisition.

Acquisition order:
  0a. The manuscript in an OSF project's file storage (above the registration form,
      because an uploaded file IS the paper and the form describes one)
  0c. The row's own URL, downloaded directly (no index, no DOI needed)
  0d. The paper this DOI is a REVIEW of (Crossref `is-review-of`, e.g. PCI → preprint)
  1. OSF preprint direct download  (DOI-pattern based, no API)
  1b. OpenAlex locations[] — every copy it knows of, files then landing pages
  1c. DataCite — the registrant's own metadata, for the DOIs Unpaywall never indexed
       (numbered Tier 3 and 3b in acquire_pdf, whose count starts at the XML tiers)
  2. Unpaywall — all direct PDF URLs
  3. SemanticScholar open-access PDF
  4. CORE.ac.uk aggregator
  5. Europe PMC
  5b. Crossref bibliographic title search — the only route for a row with NO DOI;
      gated on a two-way title match before anything is fetched
  6. Unpaywall landing-page scraper (HTML scraping for repo pages)
  7. SerpAPI / Google Scholar      (consumes quota, last resort)
  8. Playwright headless Chromium  (bypasses JS-rendered paywalls)

Three sources hand back a sections dict instead of a file, and each carries the
content check that says whether what came back is a document rather than a record of
one (_STRUCTURED_SOURCES): Tier 0 OpenAlex GROBID XML and Tier 0b the OSF
registration form, both above the download tiers because a hit there IS the document;
and Tier 11, the row's own page as HTML, at the very bottom because it is the weakest
evidence and the one result that has to be argued for rather than downloaded. A tier that comes
back empty is timestamped and not re-probed for PDF_RETRY_AFTER_DAYS; so is a single
URL the server answered 404/410 for, which holds that URL back without holding back
the other URLs its tier offers.

A downloaded document may be a PDF or a Word file — OSF serves whatever the author
uploaded — and each is saved under its own suffix, because the parsers dispatch on it.
Both are `pdf_source` values of the TIER that supplied them; the format is not a tier.

Tier 8 requires a one-time setup:
    pip install playwright
    playwright install chromium

Public API:
    acquire_pdf(doi_r, title) → dict
        keys: pdf_url, pdf_source, pdf_path, pdf_ok, pdf_url_tried
    download_pdf(url, doi, min_bytes, title) → dict
        keys: success, path, source, reason, title_check, title_coverage

Every downloaded document is checked against the title that was asked for before it
is saved (_title_check): a server can answer the right URL with the wrong paper, and
a wrong document is worse than no document — it parses and codes as evidence.
"""
import gzip
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from .config import (
    CROSSREF_RATE_SEC, OA_CACHE_DIR, OA_XML_CACHE_DIR, OPENALEX_API_KEYS,
    OSF_TOKEN, PDF_CACHE_DIR, RESEARCHER_EMAIL, SERPAPI_KEY, SERPAPI_KEYS, log,
)

from .openalex_keys import (current_index, headers as oa_headers,
                            is_budget_refusal, rotate_key)
from .cache import write_json
from .disambiguation import token_coverage
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
_OSF_RATE_SEC             = 0.5
_DATACITE_RATE_SEC        = 0.5
_HTML_RATE_SEC           = 0.5

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

# The two file formats a download may be. The %PDF test is untouched — a Word file is
# a SECOND recognised type beside it, not a loosening of it, and anything matching
# neither is still refused. OSF serves what the author uploaded, and for preprints
# that is a Word file about as often as a PDF (twelve campaign DOIs probed on
# 2026-08-08: five PDFs, seven .docx), so the format alone was closing works that
# had a full text sitting on the server.
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCUMENT_SUFFIXES = (".pdf", ".docx")

# A Word file's content check — the counterpart of the three in _STRUCTURED_SOURCES,
# asking the same question they do: is this the paper, or a wrapper around a title?
# A ZIP with a word/document.xml is a Word document; a Word document carrying a title
# page and an author line is no more a paper than a repository record page is. The
# threshold is the same as _MIN_OSF_REGISTRATION_CHARS for that reason — same
# question, same answer — and the byte count cannot stand in for it: a Word file's styles,
# theme and font table alone run to several kilobytes.
_MIN_DOCX_CHARS = 1_000

# A server can answer the right URL with the wrong paper: two fetches of one PLOS
# printable URL on 2026-08-08 returned 2,256,430 bytes of a windfall-gains paper and
# 422,071 bytes of the Hamlin/Wynn paper that was asked for. Nothing downstream can
# notice — a wrong document parses, codes and stores as that work's evidence — so the
# document is checked against the row's own title before it is saved.
#
# The statistic is title-token COVERAGE of the opening pages (token_coverage() in
# shared/disambiguation.py), measured over the 61 cached documents that could be
# matched to a row on 2026-08-08:
#
#   right document, title on the page  57 of 61 scored ≥ 0.90
#   right work, no title page          0.33 and 0.50 (two "Supplemental Material for
#                                      …" rows, whose file is the supplement), 0.53
#                                      (a supplementary appendix bundle)
#   no extractable text                1 scanned PDF, 17 chars in the whole file
#   wrong paper                        3,606 title × other-document pairs: median
#                                      0.25, 65% below 0.30, none of the true
#                                      documents there; the observed mis-serve scores
#                                      0.067
#
# 0.30 is the highest cut that refuses none of the 61 — the next real document sits at
# 0.33 — and it refuses 65% of wrong papers and every unrelated one. A stricter cut
# buys more of the mildly-related wrong pairs and starts costing real documents: 0.40
# catches 87% and loses 2, 0.60 catches 99.5% and loses 4. Losing a document costs a
# rung of the ladder; storing the wrong one costs a wrong verdict in a research
# database, but only the cut that keeps every measured document earns that trade
# without guessing. 0.30–0.60 is accepted and recorded as "low" for audit.
_TITLE_MATCH_MIN   = 0.30
_TITLE_MATCH_CLEAR = 0.60
_TITLE_CHECK_PAGES = 3
_TITLE_CHECK_DOCX_CHARS = 6_000
# Below this much extracted text the document said nothing about its identity — a
# scanned page image, a slide deck, a data deposit. Absence of a title is not a
# mismatch, so it is accepted and recorded as unchecked.
_MIN_TITLE_EVIDENCE_CHARS = 200

# Sources that hand back a sections dict instead of a file. They are documents in
# every sense the pipeline cares about, so the retry log is cleared for them exactly
# as it is for a downloaded PDF.
_STRUCTURED_SOURCES = {"openalex_xml", "osf_registration", "html_landing"}


class DocumentSourceUnavailable(Exception):
    """A source never answered — as opposed to answering that it has nothing.

    The retry log is a fourteen-day suppression, so recording a timeout in it turns a
    minute of provider trouble into two weeks of "this source has nothing", and an
    ordinary re-run cannot undo it. Only a real absence may be recorded.
    """


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
        _atomic_write_text(path, json.dumps(entries, ensure_ascii=False))
    except Exception as e:
        log.debug("Retry log write failed (%s): %s", path, e)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a per-process temp file and rename, so a concurrent reader never
    sees a half-written sidecar (a torn read would degrade to {} and re-buy work)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── The PDF already on disk ───────────────────────────────────────────────────
# One naming rule for the saved file and for the tier that supplied it, shared by
# download_pdf(), get_pdf_via_playwright() and acquire_pdf()'s up-front check.

def document_cache_path(doi_or_url: str, suffix: str = ".pdf",
                        cache_dir: "Path | None" = None) -> Path:
    """Where a document for *doi_or_url* is saved. The cache key of every tier.

    The suffix is part of the path rather than always ".pdf" because the parsers
    dispatch on it: a Word file written to a .pdf path would be handed to pdfminer.
    """
    return (cache_dir or PDF_CACHE_DIR) / f"{cache_key(doi_or_url)}{suffix}"


def pdf_cache_path(doi_or_url: str) -> Path:
    """Where a PDF for *doi_or_url* is saved."""
    return document_cache_path(doi_or_url, ".pdf")


def cached_pdf(doi_or_url: str, min_bytes: int = _MIN_PDF_BYTES,
               cache_dir: "Path | None" = None) -> "Path | None":
    """The already-downloaded document for *doi_or_url*, or None when there is none.

    Both recognised formats are looked for, PDF first. Callers must use the path this
    returns rather than rebuilding it: for a Word file it is not pdf_cache_path().

    *cache_dir* defaults to PDF_CACHE_DIR; a caller that holds its own copy of that
    path passes it, so the directory stays patchable where the caller lives (the same
    arrangement `read_parse_cache` has).
    """
    if not doi_or_url:
        return None
    for suffix in _DOCUMENT_SUFFIXES:
        path = document_cache_path(doi_or_url, suffix, cache_dir)
        try:
            if path.exists() and path.stat().st_size >= min_bytes:
                return path
        except OSError as e:
            log.debug("Document cache stat failed (%s): %s", path, e)
    return None


def _provenance_path(doi: str) -> Path:
    return PDF_CACHE_DIR / f"pdfsrc_{cache_key(doi)}.json"


def _read_provenance(doi: str) -> dict:
    """What was recorded for a saved PDF, or {}.

    {"source": tier label, "url": the URL it came from, "name": the file's own name
    where the tier knew one}.
    """
    try:
        path = _provenance_path(doi)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"source": str(data.get("source") or ""),
                        "url": str(data.get("url") or ""),
                        "name": str(data.get("name") or "")}
    except Exception as e:
        log.debug("PDF provenance unreadable for %s: %s", doi, e)
    return {}


def _write_provenance(doi: str, source: str, url: str, title_check: str = "",
                      title_coverage: "float | None" = None,
                      name: str = "") -> None:
    """Record which tier supplied the saved PDF, so a later run can report it.

    The title check's verdict rides along so an accepted-but-weak document ("low", or
    "no_text" for a file that carries no title at all) can be audited afterwards
    without re-reading it. Nothing reads these two fields; they are the audit trail.

    *name* is the file's own name where the tier has one — part of the same audit
    trail: which of a project's files the row was read from.
    """
    if not (doi and source):
        return
    path = _provenance_path(doi)
    record = {"source": source, "url": url}
    if name:
        record["name"] = name
    if title_check:
        record["title_check"] = title_check
        record["title_coverage"] = round(float(title_coverage or 0.0), 3)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(record, ensure_ascii=False))
    except Exception as e:
        log.debug("PDF provenance write failed (%s): %s", path, e)


def verified_cached_document(doi_or_url: str, title: str,
                             cache_dir: "Path | None" = None) -> "Path | None":
    """The document cached for *doi_or_url*, unless it is some other paper than *title*.

    For the readers that find a saved document by identifier rather than taking the
    path acquire_pdf just returned: a row resolved before the acquisition rung never
    calls acquire_pdf at all, so a mis-served file an earlier run stored would reach
    the parsers without anything having checked it. A mismatch is discarded here too,
    so the next run acquires afresh.
    """
    path = cached_pdf(doi_or_url, cache_dir=cache_dir)
    if path is None or not title.strip():
        return path
    verdict, coverage = _title_check(path.read_bytes(), path.suffix, title)
    if verdict != "mismatch":
        return path
    log.warning("  Cached document for %s is not %r (title coverage %.2f) — "
                "discarding it: %s", doi_or_url, title[:80], coverage, path)
    _discard_document(doi_or_url, path)
    return None


def _discard_document(doi_or_url: str, path: "Path") -> None:
    """Remove a saved document and the provenance beside it."""
    for victim in (path, _provenance_path(doi_or_url)):
        try:
            victim.unlink(missing_ok=True)
        except Exception as e:
            log.debug("Document discard failed (%s): %s", victim, e)


# ── Unpaywall ─────────────────────────────────────────────────────────────────

def _fetch_unpaywall_data(doi: str) -> Optional[dict]:
    """Fetch raw Unpaywall JSON for *doi* (cached), or None when it has no record.

    Unpaywall indexes CROSSREF ONLY. Every DataCite DOI — PsychArchives, ZBW
    JournalData, most university repositories — gets a 404 with an HTML body
    (measured 2026-08-08 for 10.23668, 10.15456 and 10.17605), which is a permanent
    answer about this API's coverage, not about the paper. It is cached as absence.

    Raises DocumentSourceUnavailable when the API did not answer, because the caller
    records a tier failure as a fourteen-day retry suppression and a timeout must
    never buy that.
    """
    doi = clean_doi(doi)
    if not doi:
        return None

    cf = OA_CACHE_DIR / f"unpaywall_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return None if data.get("__none__") else data

    throttle("unpaywall", UNPAYWALL_RATE_SEC)

    try:
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": RESEARCHER_EMAIL},
            timeout=15,
        )
        if r.status_code in (404, 422):
            write_json(cf, {"__none__": True})
            return None
        if r.status_code != 200:
            raise DocumentSourceUnavailable(f"Unpaywall HTTP {r.status_code}")
        data = r.json()
    except DocumentSourceUnavailable:
        raise
    except Exception as e:
        raise DocumentSourceUnavailable(f"Unpaywall error for {doi}: {e}") from e

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

# Every OSF preprint server files its downloads under the same osf.io/download/<guid>/,
# so the REGISTRANT is not the test — `osf.io/<guid>` as the DOI suffix is. The old
# pattern was `10.3123\d`, and it cost two things measured against the 2026-08-07
# export: 10.31219 (OSF Preprints) never matched at all (12 rows), and because the
# pattern forbade an underscore, neither did any versioned DOI on any registrant
# (30 rows, e.g. 10.31234/osf.io/d3x9p_v1).
#
# The version suffix stays in the URL. Probed 2026-08-08: `osf.io/download/d3x9p_v1/`
# serves the version the DOI names, while the bare guid serves the LATEST version, and
# for that work the two are different files.
#
# 10.17605 is excluded because it is the REGISTRATION registrant, whose downloads are
# not files: five probed on 2026-08-08 answered HTTP 500, as the ten probed on
# 2026-08-07 did. Tier 0b reads a registration through the API instead.
_OSF_REGISTRATION_REGISTRANT = "10.17605"
_OSF_PREPRINT_DOI = re.compile(r"^(10\.\d{4,5})/osf\.io/([a-z0-9]+(?:_v\d+)?)$",
                               re.IGNORECASE)


def get_osf_pdf_url(doi: str) -> Optional[str]:
    """
    Construct a direct OSF download URL from a preprint DOI.
    Covers: 10.31234/osf.io/{id}  (PsyArXiv),
            10.31219/osf.io/{id}  (OSF Preprints),
            10.31235/osf.io/{id}  (SocArXiv), and versioned forms such as
            10.31234/osf.io/{id}_v2.
    """
    doi = clean_doi(doi)
    if not doi:
        return None
    m = _OSF_PREPRINT_DOI.match(doi)
    if not m or m.group(1) == _OSF_REGISTRATION_REGISTRANT:
        return None
    return f"https://osf.io/download/{m.group(2).lower()}/"


# ── OSF registration content ──────────────────────────────────────────────────
# A registration is a container, not a file: `osf.io/download/<guid>/` answers HTTP
# 500 for every one of the ten campaign DOIs probed on 2026-08-07. Its content lives
# in the API instead, as the filled-in registration form — which is the document, not
# a substitute for one. 33% of the 2026-08-06 worklist is on the 10.17605 registrant.
#
# Which registrations are worth reading is Stage 2's decision, made on the template:
# `osf-registration-protocol` (live, discard) drops the preregistration templates and
# `osf-registration-completed` (live, screen_expensive) admits the post-completion forms
# and the Open-Ended Registrations carrying the replication stem. Stage 2 sees a
# template only through the overlay text the backfill wrote, so prospective
# registrations can still reach Stage 3 (audit of 2026-08-13, issue #196): the
# Open-Ended arm admits plan freezes, and a record the backfill never reached carries no
# template line for either rule to read. Keeping them out is Stage 2's work — tightening
# those two specs — not this tier's.

_OSF_REGISTRATION_API = "https://api.osf.io/v2/registrations/{guid}/"

# Below this, the form carries a title, an author line and little else — no design,
# no hypotheses, nothing to read. Measured 2026-08-07 over four campaign
# registrations: 5,748 / 7,750 / 3,961 chars of real content against one 326-char
# shell. Anything in between is rare, so the threshold is not a fine judgement.
_MIN_OSF_REGISTRATION_CHARS = 1_000


# Path segments that come BEFORE the guid rather than being one. Taking the first
# five-character token after "osf.io/" read these as guids: `osf.io/download/hgwkv/`
# gave "download", which is a real identifier collision — 55 pool works carry a
# download-shaped URL, so all of them keyed one entry, were asked about once, and wore
# that single answer between them (`osf_identifier()` in search/fetch_abstracts.py).
_OSF_URL_PREFIXES = {"download", "project", "registrations", "nodes", "preprints",
                     "v2", "api", "files", "wiki", "search", "dashboard", "settings",
                     "institutions", "collections", "quickfiles", "explore"}
_OSF_GUID = re.compile(r"^[a-z0-9]{5,}$")


def osf_registration_guid(doi_or_url: str) -> str:
    """The OSF guid in a registration DOI or an osf.io URL, or "".

    Both forms reach Stage 3: `10.17605/osf.io/<guid>` as a DOI, and bare
    `osf.io/<guid>` (and `api.osf.io/v2/nodes/<guid>/`) as the url_r of a row that
    never resolved to a DOI at all.

    The guid is the first path segment after `osf.io/` that is not one of the segments
    OSF puts in FRONT of a guid. A preprint is the exception in shape rather than in
    kind — `osf.io/preprints/<server>/<guid>` has two — so its guid is the last
    segment, and a version suffix (`d3x9p_v4`) is dropped: the versions of one preprint
    are one record.
    """
    text = str(doi_or_url or "").strip()
    if not text:
        return ""
    m = re.search(r"osf\.io/(.+)$", text, re.IGNORECASE)
    if not m:
        return ""
    parts = [p for p in m.group(1).split("?")[0].split("#")[0].lower().split("/") if p]
    if parts and parts[0] == "preprints":
        parts = parts[-1:]
    guid = next((p.split("_v")[0] for p in parts
                 if p not in _OSF_URL_PREFIXES and _OSF_GUID.match(p.split("_v")[0])),
                "")
    return guid


def osf_registration_has_content(registration: "dict | None") -> bool:
    """True when a registration carries enough text to read.

    The same rule as `openalex_xml_has_content()`, and for the same reason: a
    content-free result is truthy, so without this test the ladder codes a row from
    a title and an author list.
    """
    sections = (registration or {}).get("sections") or {}
    body = f"{sections.get('abstract') or ''}{sections.get('raw_text') or ''}"
    return len(body.strip()) >= _MIN_OSF_REGISTRATION_CHARS


def get_osf_registration(guid: str) -> "dict | None":
    """The OSF registration form for *guid* as a sections dict, or None.

    The shape is the one `get_openalex_fulltext()` returns, so everything downstream
    — parse_all, best_parse_result, the full-text rung — reads it unchanged. The form
    fields are joined WITH their question labels: "q6.question" alone is unreadable,
    and the label is what tells the model whether it is looking at a hypothesis, a
    design or a result.

    A 404 is a definitive absence and is cached. A 5xx or a timeout is not an answer
    and is never cached. A result that fails the content check is not cached as a
    document either.
    """
    if not guid:
        return None
    cf = OA_CACHE_DIR / f"osfreg_{cache_key(guid)}.json"
    if cf.exists():
        try:
            cached = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        if cached is not None:
            if cached.get("__none__"):
                return None
            # Every cached form carries the template it was filed under. An entry
            # written before that field existed is re-fetched once, so the provenance is
            # complete for whatever reads the cache. The absence check above comes
            # first: a `__none__` entry has no template either, and re-fetching it would
            # never converge.
            if "registration_supplement" in cached:
                return cached if osf_registration_has_content(cached) else None

    try:
        throttle("osf", _OSF_RATE_SEC)
        resp = requests.get(_OSF_REGISTRATION_API.format(guid=guid),
                            headers=_osf_headers(), timeout=30)
    except Exception as e:
        raise DocumentSourceUnavailable(f"OSF fetch failed for {guid}: {e}") from e
    if resp.status_code == 404:
        # A definitive absence, and the only OSF answer worth remembering.
        write_json(cf, {"__none__": True})
        return None
    if resp.status_code != 200:
        raise DocumentSourceUnavailable(
            f"OSF registration {guid} → HTTP {resp.status_code}")

    try:
        attrs = resp.json()["data"]["attributes"]
    except Exception as e:
        raise DocumentSourceUnavailable(
            f"OSF registration {guid} unreadable: {e}") from e

    description = str(attrs.get("description") or "").strip()
    responses = attrs.get("registration_responses") or {}
    parts: list[str] = []
    title = str(attrs.get("title") or "").strip()
    if title:
        parts.append(f"TITLE: {title}")
    for label, value in responses.items():
        if isinstance(value, str) and value.strip():
            parts.append(f"{label}: {value.strip()}")
        elif isinstance(value, list):
            joined = "; ".join(str(v).strip() for v in value if str(v).strip())
            if joined:
                parts.append(f"{label}: {joined}")

    registration = {
        # The template the form was filed under — the one field that says whether it
        # describes a study that has happened. Kept as provenance beside the text.
        "registration_supplement": str(
            attrs.get("registration_supplement") or "").strip(),
        "sections": {
            "abstract":   description,
            "intro":      "",
            "methods":    "",
            "raw_text":   "\n\n".join(parts),
            "references": [],
        },
    }
    if not osf_registration_has_content(registration):
        log.info("  OSF registration %s carries %d chars — no document", guid,
                 len(f"{description}{registration['sections']['raw_text']}".strip()))
        return None
    write_json(cf, registration)
    return registration


# ── OSF file storage ──────────────────────────────────────────────────────────
# A registration or project is a container, and the registration FORM is only one of
# the things in it: authors routinely upload the manuscript itself to the project's
# file storage. That file is the paper; the form is a description of it. So the
# storage is asked first and the form is the fallback.
#
# The listing is a JSON:API tree. Two things make it more than one request: the
# response pages (links.next), and a registration wraps everything in an "Archive of
# OSF Storage" folder whose children are reached through
# relationships.files.links.related.href.

_OSF_FILES_API = "https://api.osf.io/v2/{node_type}/{guid}/files/osfstorage/"

# Both node types are asked because the guid alone does not say which it is: a
# registration answers under /registrations/ and 404s under /nodes/, and vice versa.
_OSF_NODE_TYPES = ("nodes", "registrations")

# The listing walk's budget. A deep project with many folders could otherwise cost
# dozens of requests for a document that is almost always in the first page.
_OSF_MAX_LIST_REQUESTS = 15

# How many of the ranked candidates are downloaded before the tier gives up. Each one
# is a real download and a title check, so the ranking has to be right for the top
# few and the cap is what stops a project of forty files from costing forty fetches.
_OSF_MAX_DOWNLOADS = 5

_OSF_FILE_SUFFIXES = (".pdf", ".docx")

# What a filename says about whether it is the paper. Both lists are read against the
# whole filename, lowercased. The exclusions are the file kinds an OSF project carries
# BESIDE the manuscript — measured on campaign projects: supplements, measures,
# instruction scripts, Qualtrics exports, data-wrangling logs, author correspondence.
_OSF_NAME_POSITIVE = re.compile(
    r"(?i)manuscript|preprint|report|paper|final|\bmain\b|thesis"
    r"|stage\s*2|\brrr\b|article|\bprint\b")
_OSF_NAME_EXCLUDE = re.compile(
    r"(?i)supplement|material|measure|instruction|question|transcript|codebook"
    r"|wrangl|correspondence|ethic|qualtrics|survey|analys|appendix|syntax"
    r"|\bdata\b|dataset|_data|\blog\b")
# A Stage-1 Registered Report, a preregistration snapshot or an analysis plan is the
# manuscript BEFORE the study ran — its "results" are simulated placeholders (measured
# 2026-08-09: the 10.17605/osf.io/4jykd pick opens with "Abstract, method, and
# results were written using a randomized dataset"; audit of 2026-08-13, issue #196:
# 10.17605/osf.io/zya9n was coded from an analytic-plan DOCX). Such files are ranked
# behind every other candidate, not dropped: for a project that never deposited
# a final report they are still the best available statement of the target.
_OSF_NAME_PREREG = re.compile(
    r"(?i)prereg|pre-reg|stage\s*-?\s*1|snapshot|protocol|registration"
    r"|\bplan\b|analytic|proposal")

# Filename tokens covering this much of the row title make the file the paper's own
# name, whatever else the name says.
_OSF_TITLE_OVERLAP_MIN = 0.5


def _osf_headers() -> dict:
    # The token turns "not yours" into a listing for embargoed-but-shared projects
    # (the abstract backfill already sends it; 20 of the current no-document rows
    # are api.osf.io nodes that answer 401 anonymously).
    headers = {"User-Agent": f"flora-extractor ({RESEARCHER_EMAIL})",
               "Accept": "application/json"}
    if OSF_TOKEN:
        headers["Authorization"] = f"Bearer {OSF_TOKEN}"
    return headers


def _osf_get_json(url: str, params: "dict | None" = None) -> "dict | None":
    """One OSF API GET. None when OSF answered "not here / not yours" (401/403/404).

    Raises DocumentSourceUnavailable for everything else, because the caller records a
    fourteen-day suppression and only a real absence may buy one.
    """
    throttle("osf", _OSF_RATE_SEC)
    try:
        resp = requests.get(url, headers=_osf_headers(), params=params or {},
                            timeout=30)
    except Exception as e:
        raise DocumentSourceUnavailable(f"OSF files fetch failed for {url}: {e}") from e
    if resp.status_code in (401, 403, 404):
        return None
    if resp.status_code != 200:
        raise DocumentSourceUnavailable(f"OSF files {url} → HTTP {resp.status_code}")
    try:
        return resp.json()
    except Exception as e:
        raise DocumentSourceUnavailable(f"OSF files {url} unreadable: {e}") from e


def _walk_osf_storage(url: str, budget: list[int], incomplete: list[bool],
                      params: "dict | None" = None) -> "list[dict] | None":
    """Every file under *url*, following pagination and one level into each folder.

    Returns None when OSF answered 401/403/404 for the first request — the caller
    reads that as "this node type is not the one", not as "there are no files".
    *budget* is a one-element list so the recursion shares one request count, and
    *incomplete* is one more: it is set whenever any request after the first failed to
    answer, so the caller knows the listing it got is a part of the tree rather than
    the whole of it. Only a whole one may be cached — a truncated listing cached as
    definitive makes the files it missed permanently invisible.
    """
    files: list[dict] = []
    next_url: "str | None" = url
    next_params = params
    first = True
    while next_url and budget[0] > 0:
        budget[0] -= 1
        try:
            data = _osf_get_json(next_url, next_params)
        except DocumentSourceUnavailable:
            if first:
                raise       # the node type never answered at all — the caller's call
            incomplete[0] = True
            return files
        if data is None:
            if first:
                return None
            incomplete[0] = True    # a page of a walk that had already started
            return files
        first = False
        next_params = None          # links.next already carries the page parameters
        for entry in (data.get("data") or []):
            attrs = entry.get("attributes") or {}
            kind = str(attrs.get("kind") or "")
            name = str(attrs.get("name") or "")
            if kind == "file":
                links = entry.get("links") or {}
                guid = str(attrs.get("guid") or "").strip()
                download = str(links.get("download") or "")
                if not download and guid:
                    download = f"https://osf.io/download/{guid}/"
                if download:
                    files.append({"name": name,
                                  "size": int(attrs.get("size") or 0),
                                  "download": download})
            elif kind == "folder" and budget[0] > 0:
                related = (((entry.get("relationships") or {}).get("files") or {})
                           .get("links") or {}).get("related") or {}
                href = str(related.get("href") or "")
                if href:
                    # The page size goes down with it: a folder listed at OSF's
                    # default of ten pages through the request budget.
                    try:
                        inner = _walk_osf_storage(href, budget, incomplete,
                                                  {"page[size]": 100})
                    except DocumentSourceUnavailable:
                        inner = None
                    if inner is None:
                        # A folder that refused or never answered is a part of the
                        # tree we did not see, not an empty one.
                        incomplete[0] = True
                    files.extend(inner or [])
        next_url = ((data.get("links") or {}).get("next") or None)
    return files


def list_osf_files(guid: str) -> list[dict]:
    """Every .pdf/.docx in *guid*'s OSF storage: [{"name", "size", "download"}].

    Cached per guid, including a definitive empty listing — but only a COMPLETE
    listing, and only for PDF_RETRY_AFTER_DAYS. OSF storage is mutable by design (the
    manuscript is uploaded after the registration this tier reads), so an immortal
    cache would answer the 14-day re-probe with the listing that made it fail. Raises
    DocumentSourceUnavailable when neither node type answered at all — an empty list
    means OSF said there is nothing, never that it failed to say.
    """
    if not guid:
        return []
    cf = OA_CACHE_DIR / f"osffiles_{cache_key(guid)}.json"
    if cf.exists():
        try:
            cached = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        # No fetched_at at all is an entry written before the expiry existed: stale.
        if isinstance(cached, dict) and _retry_suppressed(
                {"listing": str(cached.get("fetched_at") or "")}, "listing",
                PDF_RETRY_AFTER_DAYS):
            return list(cached.get("files") or [])

    budget = [_OSF_MAX_LIST_REQUESTS]
    incomplete = [False]
    files: "list[dict] | None" = None
    outages: list[str] = []
    for node_type in _OSF_NODE_TYPES:
        url = _OSF_FILES_API.format(node_type=node_type, guid=guid)
        try:
            found = _walk_osf_storage(url, budget, incomplete,
                                      params={"page[size]": 100})
        except DocumentSourceUnavailable as exc:
            outages.append(str(exc))
            continue
        if found is not None:
            files = found
            break
    if files is None:
        if outages:
            raise DocumentSourceUnavailable("; ".join(outages))
        files = []      # both node types answered "not here" — a real absence

    documents = [f for f in files
                 if f["name"].lower().endswith(_OSF_FILE_SUFFIXES)]
    if budget[0] > 0 and not incomplete[0]:
        # A walk that ran out of requests, or that lost a page or a folder to a
        # failure, saw part of the tree; caching a partial listing would make the
        # unseen part invisible until the entry expired.
        write_json(cf, {"files": documents, "fetched_at": _now_iso()})
    return documents


def rank_osf_files(files: list[dict], title: str = "") -> list[dict]:
    """*files*, best manuscript candidate first, with the non-manuscripts dropped.

    A name that hits an exclusion and carries fewer than two positive signals is not
    offered at all: the title check downstream is the backstop, and a supplement to the
    right paper passes it. Ties break to PDF over Word, then to the larger file.
    """
    scored: list[tuple] = []
    for entry in files:
        name = str(entry.get("name") or "")
        stem = re.sub(r"[_\-.,&]+", " ", name.rsplit(".", 1)[0])
        score = len(set(m.group(0).lower()
                        for m in _OSF_NAME_POSITIVE.finditer(name)))
        if _OSF_NAME_EXCLUDE.search(name):
            # One positive word does not survive an exclusion: "final test
            # questions.pdf" carries "final" and is a questionnaire. Two positive
            # signals do — that is a manuscript whose name happens to mention its
            # analyses. The row's title is deliberately NOT one of them:
            # "Supplementary materials for <row title>.pdf" carries the whole title
            # and is the supplement, so the bonus below ranks non-excluded names and
            # rescues nothing.
            if score < 2:
                continue
            score -= 1
        elif title.strip() and token_coverage(title, stem) >= _OSF_TITLE_OVERLAP_MIN:
            score += 2
        is_pdf = name.lower().endswith(".pdf")
        prereg = 1 if _OSF_NAME_PREREG.search(name) else 0
        scored.append((prereg, -score, 0 if is_pdf else 1,
                       -int(entry.get("size") or 0), name, entry))
    scored.sort(key=lambda row: row[:5])
    return [row[5] for row in scored]


# ── HTML as a document ────────────────────────────────────────────────────────
# 30% of the 2026-08-06 worklist carries no DOI, only a repository URL, and
# `acquire_pdf` was never told that URL — of 22 probed on 2026-08-07, 17 answered
# HTTP 200 with HTML and exactly 1 was a PDF. Those pages are the only thing that
# exists for those rows.
#
# The danger is the page that restates the abstract and nothing else: coding a row
# from it looks like full text and is not. The acceptance rule is therefore about
# STRUCTURE, not length — the parsed page must yield body text beyond the abstract,
# or a reference list. An abstract-only landing page yields neither and is refused,
# whatever its byte count, because repository chrome (menus, licence boilerplate,
# "cite this item") inflates length without adding a word of the paper.

_HTML_STRIP_TAGS = ("script", "style", "noscript", "nav", "header", "footer",
                    "form", "aside", "svg", "button")

# The two thresholds that separate a paper from a record page. Measured 2026-08-07
# over eight pages — three full texts (PLOS, PMC, eLife) and five repository landing
# pages (econpapers, ideas.repec, eScholarship-style, PubMed, handle.net):
#
#   text beyond the abstract   landing 0 – 1,706 chars   full text 49,193 – 71,641
#   reference block            landing 0 chars          full text 0 / 9,771 / 71,185
#
# There is nothing in between, so neither number is a fine judgement. Section headings
# are NOT the test: the splitter found no intro in any of the three full texts, because
# HTML articles head their sections in ways a PDF-oriented splitter does not recognise.
# What survives that is the crude measure — how much text there is that is not the
# abstract — which is exactly the thing a page restating the abstract does not have.
_MIN_HTML_BEYOND_ABSTRACT_CHARS = 10_000
_MIN_HTML_REFERENCES_CHARS      = 2_000

# A block passing on length alone is not enough — it must also read like scholarship.
# Measured 2026-08-07: the five landing pages mention 0-3 years in their whole text,
# the three full texts 60+ in their reference blocks alone.
_MIN_SCHOLARLY_YEARS = 8
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _cited_year_count(text: str) -> int:
    """How many four-digit years *text* mentions — the cheapest "is this scholarly
    prose or page furniture" signal there is.

    A bibliography is mostly years; so is a paper's body. A repository record page has
    one or two (the deposit date, the publication year), however much chrome sits
    around them.
    """
    return len(_YEAR_RE.findall(text))


def html_document_has_content(document: "dict | None") -> bool:
    """True when parsed HTML carries a paper rather than a record of one.

    This IS the landing-page test. Two ways to pass, and each needs the length signal
    AND the year-density signal, because neither is sufficient alone:

      * a real bibliography — a big block after a references heading that is dense
        with years;
      * a real body — text far beyond the abstract, dense with years.

    Length alone was the first version of this test and it was too weak. The subtracted
    abstract is whatever the PDF-oriented splitter happened to label, and on an HTML
    page it often labels nothing at all: with `abstract == ""` the test collapsed to
    "is this page 10,000 characters", which a repository page full of related items,
    comments and metadata can be. Likewise any 2,000 characters following something
    that looked like a references heading passed, whether or not it was a bibliography.
    """
    sections = (document or {}).get("sections") or {}
    raw = str(sections.get("raw_text") or "")
    abstract = str(sections.get("abstract") or "")
    references = str(sections.get("references_raw") or "")

    if (len(references) >= _MIN_HTML_REFERENCES_CHARS
            and _cited_year_count(references) >= _MIN_SCHOLARLY_YEARS):
        return True
    return ((len(raw) - len(abstract)) >= _MIN_HTML_BEYOND_ABSTRACT_CHARS
            and _cited_year_count(raw) >= _MIN_SCHOLARLY_YEARS)


def get_html_document(url: str) -> "dict | None":
    """*url*'s page as a sections dict, or None when it is not a document.

    Returns the shape `get_openalex_fulltext()` returns, so the rest of the pipeline
    reads it unchanged. Parsing reuses the same splitter pair as `parse_pdfminer`, so
    "what counts as an intro / a reference list" has one definition across PDF and
    HTML rather than two that drift.

    A non-200 is never cached: it is a server's mood, not a statement about the page.
    A page that fails the content check is cached as a definitive absence, because the
    page's SHAPE is what failed and re-fetching it tomorrow gives the same shape.
    """
    if not url:
        return None
    cf = OA_CACHE_DIR / f"htmldoc_{cache_key(url)}.json"
    if cf.exists():
        try:
            cached = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            cached = None
        if cached is not None:
            if cached.get("__none__"):
                return None
            return cached if html_document_has_content(cached) else None

    try:
        throttle("html_landing", _HTML_RATE_SEC)
        resp = requests.get(
            url, timeout=30, allow_redirects=True,
            headers={"User-Agent": f"flora-extractor ({RESEARCHER_EMAIL})"})
    except Exception as e:
        raise DocumentSourceUnavailable(f"HTML fetch failed for {url}: {e}") from e
    if resp.status_code >= 500:
        raise DocumentSourceUnavailable(f"HTML {url} → HTTP {resp.status_code}")
    if resp.status_code != 200:
        # A 4xx is the server saying this page is not there for us — an answer, but
        # not one worth caching as a document verdict, since access can change.
        log.debug("HTML %s → HTTP %s", url, resp.status_code)
        return None
    if "html" not in resp.headers.get("content-type", "").lower():
        return None

    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(resp.content)
        for tag in _HTML_STRIP_TAGS:
            for node in tree.iter(tag):
                node.getparent().remove(node) if node.getparent() is not None else None
        text = tree.text_content()
    except Exception as e:
        log.debug("HTML parse failed (%s): %s", url, e)
        return None

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()

    from .grobid import _parse_references_block, _split_sections
    sections = _split_sections(text)
    document = {"sections": {
        "abstract":       sections.get("abstract", ""),
        "intro":          sections.get("intro", ""),
        "methods":        sections.get("methods", ""),
        "raw_text":       text,
        "references":     _parse_references_block(sections.get("references_raw", "")),
        # Kept because the acceptance test reads its SIZE: the entry parser is built
        # for PDF reference blocks and finds one or two entries in an HTML
        # bibliography, so its count says nothing about whether a bibliography exists.
        "references_raw": sections.get("references_raw", ""),
    }}
    if not html_document_has_content(document):
        log.info("  [%s] HTML is a landing page, not a document — %d chars beyond "
                 "its abstract, no reference block", url[:70],
                 max(0, len(text) - len(sections.get("abstract", ""))))
        write_json(cf, {"__none__": True})
        return None
    write_json(cf, document)
    return document


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

def get_openalex_locations(doi: str) -> list[dict]:
    """Every copy OpenAlex knows of for *doi*, ordered, in get_all_unpaywall_pdf_urls'
    shape: {"url", "type": "pdf"|"landing", "host", "license"}.

    This used to ask for `open_access` alone and return the single `oa_url`, and the
    `locations[]` array — which is where the repository mirrors are — was never
    requested. That decides rows: a publisher copy behind Cloudflare while a
    university repository serves the same paper freely, and works whose one green copy
    is dead where a second would have answered. A single-entity lookup by DOI is free,
    so the wider `select` costs nothing; it must not become a search query.

    Ordering, measured 2026-08-08 over the 88 location URLs beyond `oa_url` that 60
    no-document rows carry: 2 answered with a PDF, both of them `landing_page_url`
    and both on locations OpenAlex marks `is_oa: false`. So landing pages are tried
    rather than only scraped, and `is_oa` is not an ordering key — filtering on it
    would have dropped both hits. Files before pages, and `oa_url` stays first, since
    that is the one URL the tier used to try.

    Raises DocumentSourceUnavailable when OpenAlex did not answer: the caller turns a
    tier failure into a fourteen-day retry suppression.
    """
    doi = clean_doi(doi)
    if not doi:
        return []

    # Its own cache file: the old oa_<hash>.json entries hold the narrow `select` and
    # carry no locations, so reading them would answer a wider question with a
    # narrower cached answer. They are left on disk unread — the refetch is free.
    cf = OA_CACHE_DIR / f"oaloc_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("__none__"):
            return []
    else:
        throttle("openalex", _OPENALEX_RATE_SEC)
        try:
            r = requests.get(
                f"https://api.openalex.org/works/doi:{doi}",
                params={"select": "open_access,best_oa_location,locations",
                        "mailto": RESEARCHER_EMAIL},
                headers={"User-Agent": f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})"},
                timeout=15,
            )
            if r.status_code == 404:
                write_json(cf, {"__none__": True})
                return []
            if r.status_code != 200:
                raise DocumentSourceUnavailable(f"OpenAlex HTTP {r.status_code}")
            data = r.json()
        except DocumentSourceUnavailable:
            raise
        except Exception as e:
            raise DocumentSourceUnavailable(
                f"OpenAlex locations error for {doi}: {e}") from e
        write_json(cf, data)

    seen:    set[str]   = set()
    results: list[dict] = []

    def _add(url, url_type, loc: dict) -> None:
        if url and url not in seen:
            seen.add(url)
            source = loc.get("source") or {}
            results.append({"url": url, "type": url_type,
                            "host": source.get("host_organization_name") or "",
                            "license": loc.get("license") or ""})

    best = data.get("best_oa_location") or {}
    locations = [best] + list(data.get("locations") or [])
    _add((data.get("open_access") or {}).get("oa_url"), "pdf", best)
    for loc in locations:
        _add((loc or {}).get("pdf_url"), "pdf", loc or {})
    for loc in locations:
        _add((loc or {}).get("landing_page_url"), "landing", loc or {})
    return results


# ── DataCite ──────────────────────────────────────────────────────────────────
#
# Unpaywall indexes Crossref only, so for the institutional-repository share of this
# corpus — 10.23668 PsychArchives, 10.15456 ZBW, 10.18718, 10.26686, 10.4121, most
# university repositories — every DOI-keyed tier above asks an index that has never
# heard of the registrant. DataCite is that registrant's own metadata, keyless and
# free: attributes.url is the landing page, attributes.contentUrl is sometimes the
# file itself.

def get_datacite_urls(doi: str) -> list[dict]:
    """Acquisition candidates from DataCite metadata, in get_openalex_locations' shape.

    contentUrl is a file when it is there at all, so it is offered first; the landing
    page follows, for the same reason OpenAlex's are tried and then scraped.

    Raises DocumentSourceUnavailable when the API did not answer. A 404 is DataCite
    saying the DOI is not one of its own — a permanent answer, cached as absence.
    """
    doi = clean_doi(doi)
    if not doi:
        return []

    cf = OA_CACHE_DIR / f"datacite_{cache_key(doi)}.json"
    if cf.exists():
        with cf.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("__none__"):
            return []
    else:
        throttle("datacite", _DATACITE_RATE_SEC)
        try:
            r = requests.get(
                f"https://api.datacite.org/dois/{requests.utils.quote(doi, safe='')}",
                headers={"User-Agent": f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})"},
                timeout=15,
            )
            if r.status_code == 404:
                write_json(cf, {"__none__": True})
                return []
            if r.status_code != 200:
                raise DocumentSourceUnavailable(f"DataCite HTTP {r.status_code}")
            data = r.json()
        except DocumentSourceUnavailable:
            raise
        except Exception as e:
            raise DocumentSourceUnavailable(f"DataCite error for {doi}: {e}") from e
        write_json(cf, data)

    attrs = ((data.get("data") or {}).get("attributes") or {})
    content = attrs.get("contentUrl")
    if isinstance(content, str):
        content = [content]
    results: list[dict] = []
    seen: set[str] = set()
    for url, kind in ([(u, "pdf") for u in (content or [])]
                      + [(attrs.get("url"), "landing")]):
        if isinstance(url, str) and url.startswith("http") and url not in seen:
            seen.add(url)
            results.append({"url": url, "type": kind,
                            "host": str(attrs.get("publisher") or ""), "license": ""})
    return results


# ── Crossref: the reviewed paper, and the paper behind a bare title ────────────
#
# Two rows the DOI-keyed tiers cannot serve at all:
#
#   * a Peer Community In recommendation (10.24072/pci.rr.*). Its own DOI has no
#     document worth reading — the paper is the preprint it reviews, named in the
#     Crossref record's `relation["is-review-of"]` (measured: a PCI RR DOI points at a
#     10.31234 PsyArXiv preprint).
#   * a row with no DOI at all — 223 of the 2026-08-07 no-document rows. Nothing above
#     the HTML tier can be asked about them, because every index above is keyed on a
#     DOI the row does not have. A bibliographic title search is the only route to one.
#
# Both then acquire ANOTHER paper's document, which is the wrong-paper risk this module
# already spends a title check on. The search route therefore gates twice: the metadata
# titles must match before a byte is fetched, and the bytes must pass _title_check.

_CROSSREF_UA = "FLoRA-DisambiguationPipeline/1.0 (mailto:{email})"

# Shorter than this, a title is too generic for a bibliographic search to be evidence
# of anything ("Replication", "Study 2").
_MIN_SEARCH_TITLE_CHARS = 30

# The two-way coverage a Crossref title hit must clear before its document is fetched.
# Higher than the download-time _TITLE_MATCH_CLEAR, and separate from it: that one
# grades bytes that were already fetched for a KNOWN DOI, while this one decides which
# other paper's DOI to fetch at all, and the near-miss it must refuse — the original
# under the replication's title — is this corpus's most common pair.
_SEARCH_TITLE_MATCH_MIN = 0.80

# The vocabulary that makes a title a replication's rather than the original's.
_REPLICATION_STEM = re.compile(r"(?i)replicat|reproduc|re-?examin|revisit")

# The one registrant whose `is-review-of` relation means "this record has no text of
# its own; the reviewed paper is the paper": Peer Community In. Crossref carries the
# same relation on book reviews, published commentaries and review reports, and each
# of those IS a publication with its own full text — redirecting such a row to the
# work it discusses would code it from the wrong paper.
_REVIEW_RELATION_REGISTRANTS = ("10.24072",)


def _crossref_get(url: str, params: dict, cache_file: Path,
                  none_on_404: bool = True) -> "dict | None":
    """A cached Crossref GET. None when Crossref has no such record (404).

    Raises DocumentSourceUnavailable when the API did not answer — the shape
    `_fetch_unpaywall_data` uses, and for the same reason.

    *none_on_404* is what /works/{doi} and the search endpoint disagree about: for a
    record lookup a 404 IS the answer "no such DOI", while a search answers "nothing
    found" with 200 and an empty items list, so a 404 there is the service
    misbehaving. Caching that would disable the title search for the title forever.
    """
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            return None if data.get("__none__") else data

    throttle("crossref", CROSSREF_RATE_SEC)
    try:
        r = requests.get(url, params=params, timeout=20,
                         headers={"User-Agent": _CROSSREF_UA.format(email=RESEARCHER_EMAIL)})
        if r.status_code == 404:
            if not none_on_404:
                raise DocumentSourceUnavailable(f"Crossref HTTP 404 for {url}")
            write_json(cache_file, {"__none__": True})
            return None
        if r.status_code != 200:
            raise DocumentSourceUnavailable(f"Crossref HTTP {r.status_code} for {url}")
        data = r.json()
    except DocumentSourceUnavailable:
        raise
    except Exception as e:
        raise DocumentSourceUnavailable(f"Crossref error for {url}: {e}") from e

    write_json(cache_file, data)
    return data


def crossref_reviewed_doi(doi: str) -> str:
    """The DOI this review/recommendation is OF, or "" when it reviews nothing."""
    doi = clean_doi(doi)
    if not doi:
        return ""
    data = _crossref_get(f"https://api.crossref.org/works/{doi}", {},
                         OA_CACHE_DIR / f"crmeta_{cache_key(doi)}.json")
    relation = ((data or {}).get("message") or {}).get("relation") or {}
    for item in (relation.get("is-review-of") or []):
        if str((item or {}).get("id-type") or "").lower() == "doi":
            reviewed = clean_doi(str(item.get("id") or ""))
            if reviewed and reviewed != doi:
                return reviewed
    return ""


def crossref_title_matches(title: str) -> list[str]:
    """DOIs of Crossref works whose own title is *title*, strictest match only.

    Coverage is asymmetric, so both directions are required: one direction alone
    accepts a subset title, and "Replication of X" against the original "X" is exactly
    the pair this route must not follow. Both directions at 0.60 do not stop that pair
    either — measured, "A direct replication of Ego depletion and moral judgment"
    against "Ego depletion and moral judgment" scores 0.71/1.00, because the added
    words are a minority of the tokens. Hence two gates: a coverage cut high enough
    that a handful of added words fails it, and the vocabulary the added words are
    drawn from. In this corpus the row IS the replication, so a row title that says so
    against a hit title that does not is the modal wrong pair, not an edge case.
    """
    title = str(title or "").strip()
    if len(title) < _MIN_SEARCH_TITLE_CHARS:
        return []
    data = _crossref_get("https://api.crossref.org/works",
                         {"rows": 5, "query.bibliographic": title},
                         OA_CACHE_DIR / f"crsearch_{cache_key(title)}.json",
                         none_on_404=False)
    row_is_replication = bool(_REPLICATION_STEM.search(title))
    matches: list[str] = []
    for item in (((data or {}).get("message") or {}).get("items") or []):
        hit_title = " ".join(str(t) for t in (item.get("title") or []))
        hit_doi = clean_doi(str(item.get("DOI") or ""))
        if not (hit_doi and hit_title):
            continue
        if row_is_replication and not _REPLICATION_STEM.search(hit_title):
            continue
        if (token_coverage(title, hit_title) >= _SEARCH_TITLE_MATCH_MIN
                and token_coverage(hit_title, title) >= _SEARCH_TITLE_MATCH_MIN):
            matches.append(hit_doi)
    return matches


def crossref_title(doi: str) -> str:
    """The title Crossref holds for *doi*, or "" when it holds none."""
    doi = clean_doi(doi)
    if not doi:
        return ""
    data = _crossref_get(f"https://api.crossref.org/works/{doi}", {},
                         OA_CACHE_DIR / f"crmeta_{cache_key(doi)}.json")
    titles = ((data or {}).get("message") or {}).get("title") or []
    return " ".join(str(t) for t in titles).strip()


def document_urls_for_doi(doi: str) -> tuple[list[str], bool]:
    """(candidate document URLs for *doi*, did any source fail to answer).

    The cheap DOI-keyed lookups of the waterfall, over ANOTHER paper's DOI: the two
    tiers that follow a relation or a title search need a document for a DOI that is
    not the row's, and re-entering acquire_pdf would cache it under that other DOI,
    write it a retry log and label it with that run's tier instead of the tier that
    really found it.

    The outage flag is what keeps a provider's bad minute out of the retry log.
    """
    doi = clean_doi(doi)
    if not doi:
        return [], False

    urls: list[str] = []
    outage = False
    osf = get_osf_pdf_url(doi)
    if osf:
        urls.append(osf)
    try:
        urls += [u["url"] for u in get_all_unpaywall_pdf_urls(doi) if u["type"] == "pdf"]
    except DocumentSourceUnavailable as exc:
        outage = True
        log.info("  Unpaywall unavailable for related %s: %s", doi, exc)
    try:
        # Files only. The row's own landing pages are scraped one tier down; these
        # DOIs have no such tier, so a landing page here can only be fetched raw,
        # refused as not_a_pdf, and counted as the tier having tried something.
        urls += [u["url"] for u in get_openalex_locations(doi) if u["type"] == "pdf"]
    except DocumentSourceUnavailable as exc:
        outage = True
        log.info("  OpenAlex unavailable for related %s: %s", doi, exc)

    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))], outage


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

# How many of a landing page's PDF links are downloaded before the tier gives up.
# Each is a real fetch, and beyond the first few the hrefs are cited-by links and
# journal front matter rather than this paper.
_MAX_LANDING_CANDIDATES = 4

def scrape_pdf_from_landing_page(landing_url: str) -> list[str]:
    """Direct PDF links found on a repository landing page, best candidate first.

    Covers HAL, DSpace, Pure, and generic repos. Every resolved candidate is returned
    rather than only the first: a page routinely offers the publisher's paywalled copy,
    a mirror and the accepted manuscript, and the first href is as often the one that
    403s as the one that serves. The caller tries them in turn.
    """
    if not landing_url:
        return []
    try:
        r = requests.get(
            landing_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; academic research bot)",
                     "Accept"    : "text/html,application/xhtml+xml"},
            timeout=20,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return []
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
        return (main or resolved)[:_MAX_LANDING_CANDIDATES]

    except Exception as e:
        log.debug("Landing-page scrape failed (%s): %s", landing_url, e)
        return []


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


# The two identities the browser tier asks under, for the same reason download_pdf
# has two: a host that answers one with a challenge page answers the other with the
# file. Anubis-style proof-of-work walls are the measured case — they wave a declared
# bot through and put a JavaScript challenge in front of a claimed Chrome.
_PW_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/124.0.0.0 Safari/537.36")


def _pw_bot_ua() -> str:
    return f"flora-extractor ({RESEARCHER_EMAIL})"


def get_pdf_via_playwright(doi: str, min_bytes: int = 5_000, title: str = "",
                           url_r: str = "") -> dict:
    """
    Launch a headless Chromium browser, navigate to the paper's landing page,
    and attempt to download a PDF by:
      1. Intercepting any network response whose Content-Type is application/pdf
      2. Clicking the first matching PDF download link/button

    The landing page is `doi.org/<doi>`, or the ROW'S OWN URL when there is no DOI.
    Without that fallback the tier refused 223 of the 2026-08-07 no-document rows
    up front — the rows with nothing but a URL, which are exactly the ones every
    DOI-keyed tier above has already failed on.

    Returns the same dict shape as download_pdf():
        {"success", "path", "source": "playwright", "reason"}

    Requires:  pip install playwright && playwright install chromium
    """
    doi = clean_doi(doi)
    url_r = str(url_r or "").strip()
    landing = (f"https://doi.org/{doi}" if doi
               else (url_r if url_r.lower().startswith(("http://", "https://")) else ""))
    if not landing:
        return {"success": False, "path": None, "source": "", "reason": "no_doi"}

    # The saved file is keyed the way every other tier keys it: the DOI when there is
    # one, the row's URL otherwise. A rebuilt pdf_cache_path(doi) would be a path
    # under an empty key for a DOI-less row.
    cache_id = doi or url_r
    pdf_path = pdf_cache_path(cache_id)
    have     = cached_pdf(cache_id, min_bytes)
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
        from playwright.sync_api import (Error as PWError, sync_playwright,
                                          TimeoutError as PWTimeout)
    except ImportError:
        log.info("Playwright not installed — skipping headless tier "
                 "(run: pip install playwright && playwright install chromium)")
        return {"success": False, "path": None, "source": "",
                "reason": "playwright_not_installed"}

    def _save(content: bytes) -> "dict | None":
        """The tier's four write sites, each of which must pass the title check.

        A browser reaches the same servers requests does and is handed the same wrong
        documents; the difference is only how the bytes arrive.
        """
        verdict, coverage = _title_check(content, ".pdf", title)
        if verdict == "mismatch":
            log.warning("  Playwright document for %s is not %r (title coverage "
                        "%.2f) — refused", cache_id, title[:80], coverage)
            return None
        pdf_path.write_bytes(content)
        return {"success": True, "path": pdf_path, "source": "playwright",
                "reason": "", "title_check": verdict, "title_coverage": coverage}

    def _attempt(user_agent: str) -> dict:
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
                user_agent=user_agent,
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

            # ── Navigate to the landing page ─────────────────────────────────
            try:
                page.goto(landing, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3_000)   # let JS render
            except PWTimeout:
                log.debug("Playwright: page load timeout for %s", cache_id)
            except PWError as exc:
                # Any other navigation failure — a bad certificate, a refused
                # connection, a DNS miss. Only PWTimeout was caught, so these escaped
                # the tier and aborted the whole ROW: two works on the frozen dev
                # sample were written api_error by ERR_CERT_COMMON_NAME_INVALID on
                # doi.org, having never reached the abstract rung that would have
                # named their original. One acquisition tier failing is that tier
                # failing.
                log.debug("Playwright: navigation failed for %s: %s", cache_id, exc)

            # ── If inline PDF was served directly, we already have bytes ──────────
            if captured["bytes"] and captured["bytes"][:4] == b"%PDF":
                saved = _save(captured["bytes"])
                ctx.close(); browser.close()
                if saved:
                    return saved
                return {"success": False, "path": None, "source": "",
                        "reason": "wrong_document"}

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
                                    headers={"User-Agent": user_agent},
                                    timeout=60,
                                    stream=True,
                                )
                                content = b"".join(raw.iter_content(65_536))
                                if content[:4] == b"%PDF" and len(content) >= min_bytes:
                                    saved = _save(content)
                                    if saved:
                                        ctx.close(); browser.close()
                                        return saved
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
                            saved = _save(content)
                            if saved:
                                ctx.close(); browser.close()
                                return saved

                except PWTimeout:
                    log.debug("Playwright: download timeout for selector '%s'", selector)
                except Exception as e:
                    log.debug("Playwright: selector '%s' failed: %s", selector, e)

            ctx.close()
            browser.close()

        # Check once more — the response interceptor may have fired after a click
        if captured["bytes"] and captured["bytes"][:4] == b"%PDF":
            saved = _save(captured["bytes"])
            if saved:
                return saved
            return {"success": False, "path": None, "source": "",
                    "reason": "wrong_document"}

        return {"success": False, "path": None, "source": "",
                "reason": "playwright_no_pdf_found"}

    result = _attempt(_PW_CHROME_UA)
    if result["reason"] == "playwright_no_pdf_found":
        # The page had no document for a browser. That can be the page's answer, or an
        # answer to who asked — a challenge wall serves one to a claimed Chrome and the
        # real page to a bot that says what it is. Only this reason is retried: a wrong
        # document was a real document, and a missing browser is a missing browser.
        log.debug("  No document as Chrome — asking %s again as flora-extractor",
                  landing)
        result = _attempt(_pw_bot_ua())
    return result


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


# The two identities a download is asked under, in order. Neither is a superset of the
# other, so a URL that gives one identity no document is asked again under the other.
#
# The browser identity gets past publisher bot walls. The polite identity gets past the
# repositories that do the opposite — serve a challenge page to a spoofed browser and
# the file to a named crawler with a contact address. econstor.eu is exactly that:
# measured 2026-08-08, `econstor.eu/bitstream/10419/318610/1/JCRE-2025-2.pdf` answers
# the Chrome identity with a 4,806-byte HTML interstitial and the FLoRA identity with
# 774,018 bytes of PDF.
#
# The second attempt is made only when the first ANSWERED and its answer was not a
# document — a refusal, a timeout and a 5xx are all the same to both identities, and
# a 404/410 is the server saying there is nothing here for anyone. So the extra
# request is paid on rows that were about to get nothing, and nowhere else.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}


def _polite_headers() -> dict:
    """Built per call so a changed RESEARCHER_EMAIL takes effect without a reimport."""
    return {"User-Agent": f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})",
            "Accept": "application/pdf,*/*;q=0.9"}


def _document_format(content: bytes, url: str) -> tuple[str, str]:
    """(suffix, "") when these bytes are a document, ("", reason) when they are not.

    A PDF is its magic number. A Word file is a ZIP that carries a word/document.xml
    with enough text in it — the ZIP magic alone is any ZIP, an epub or a data archive
    included, and a Word file holding a title page is no more a paper than a
    repository record page is.
    """
    if content.startswith(_PDF_MAGIC):
        return ".pdf", ""
    if content[:4] == _ZIP_MAGIC:
        from .pdf_parsing import docx_text   # lazy: pdf_parsing pulls in grobid
        text = docx_text(content).strip()
        if not text:
            return "", "not_a_pdf"
        if len(text) < _MIN_DOCX_CHARS:
            log.info("  Word file carries %d chars — no document: %s", len(text), url)
            return "", "docx_no_content"
        return ".docx", ""
    return "", "not_a_pdf"


def _front_matter_text(content: bytes, suffix: str) -> str:
    """The opening text of a document — where a paper prints its title, or nothing.

    Three pages, not one: the two supplements in the measured sample and several
    journal PDFs put a cover sheet, a badge disclosure or a running head ahead of the
    title block, and page 1 alone scored them 0.06–0.15 lower for no reason to do with
    identity.
    """
    if suffix == ".docx":
        from .pdf_parsing import docx_text   # lazy: pdf_parsing pulls in grobid
        return docx_text(content)[:_TITLE_CHECK_DOCX_CHARS]
    try:
        import io
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(content), maxpages=_TITLE_CHECK_PAGES) or ""
    except Exception as exc:
        # Unreadable front matter is no evidence either way, and _title_check reads it
        # as such. A PDF pdfminer cannot open is still a PDF the parsers may manage.
        log.debug("Front matter unreadable (%s): %s", suffix, exc)
        return ""


def _title_check(content: bytes, suffix: str, title: str) -> tuple[str, float]:
    """Is this document the paper that was asked for? (verdict, coverage).

    Verdicts: "match" · "low" · "mismatch" · "no_text" (nothing to check against).
    """
    if not title.strip():
        return "no_text", 0.0
    text = _front_matter_text(content, suffix)
    if len(text.strip()) < _MIN_TITLE_EVIDENCE_CHARS:
        return "no_text", 0.0
    # Titles come off OpenAlex doubly escaped ("&amp;amp;"), and an "amp" token in the
    # title that no PDF can carry lowers every score it appears in.
    coverage = token_coverage(html.unescape(html.unescape(title)), text)
    if coverage < _TITLE_MATCH_MIN:
        return "mismatch", coverage
    return ("match" if coverage >= _TITLE_MATCH_CLEAR else "low"), coverage


def download_pdf(url: str, doi: str = "", min_bytes: int = _MIN_PDF_BYTES,
                 title: str = "", referer: str = "") -> dict:
    """
    Download a document and save it to PDF_CACHE_DIR.

    *referer* is the page a scraped link was found on. Repository and publisher
    servers routinely serve a file to a request that came from their own page and a
    challenge to one that arrived from nowhere, so the scraper passes the landing URL
    and the header overrides the default in either identity.

    Cache key = MD5 of doi (or url if doi missing), so repeat calls skip download.
    A PDF and a Word file are both documents; anything else is refused, and a Word
    file with too little text in it is refused as well, so nothing that failed its
    content check is ever saved. A document whose opening pages are some other paper
    than *title* is refused the same way, and never saved.

    Returns: {"success", "path", "source", "reason", "title_check", "title_coverage"}
    """
    if not url:
        return {"success": False, "path": None, "source": "", "reason": "no_url"}

    have = cached_pdf(doi or url, min_bytes)
    if have is not None:
        verdict, coverage = _title_check(have.read_bytes(), have.suffix, title)
        if verdict != "mismatch":
            return {"success": True, "path": have, "source": "cache", "reason": "",
                    "title_check": verdict, "title_coverage": coverage}
        # The wrong document has to GO, not merely be refused: every tier below asks
        # download_pdf, which would hit this same cache entry before fetching, so a
        # stored mismatch left in place closes the row to every tier for good.
        log.warning("  Cached document is not %r (title coverage %.2f) — discarding "
                    "it and re-fetching: %s", title[:80], coverage, have)
        _discard_document(doi or url, have)

    if _url_is_gone(url):
        log.debug("  URL answered gone less than %d days ago — not re-fetching: %s",
                  PDF_RETRY_AFTER_DAYS, url)
        return {"success": False, "path": None, "source": "", "reason": "url_gone"}

    identities = (_BROWSER_HEADERS, _polite_headers())
    for attempt, headers in enumerate(identities):
        if referer:
            headers = {**headers, "Referer": referer}
        try:
            r = requests.get(url, headers=headers, timeout=60, stream=True)
            if r.status_code in _PERMANENT_HTTP_STATUS:
                _write_retry_log(_url_failure_path(url),
                                 {f"http_{r.status_code}": _now_iso()})
                return {"success": False, "path": None, "source": "",
                        "reason": f"http_{r.status_code}"}
            r.raise_for_status()
            content = b"".join(r.iter_content(chunk_size=65_536))
        except Exception as e:
            return {"success": False, "path": None, "source": "",
                    "reason": f"download_error: {e}"}

        suffix, reason = _document_format(content, url)
        if not suffix:
            # The server answered, and what it sent is not a document. That can be the
            # URL's answer — or it can be an answer to WHO ASKED, which is why the
            # second identity exists. Only the last one's reason is reported.
            if attempt == 0:
                log.debug("  %s to the browser identity: %s — asking again as "
                          "FLoRA", reason, url)
                continue
            return {"success": False, "path": None, "source": "", "reason": reason}

        if len(content) < min_bytes:
            return {"success": False, "path": None, "source": "", "reason": "file_too_small"}

        verdict, coverage = _title_check(content, suffix, title)
        if verdict == "mismatch":
            # Not written to disk and not recorded against the URL: the observed
            # mis-serve was transient — the same URL answered correctly on another
            # fetch — so a 404-style suppression would block the fetch that works.
            # Nor is the second identity asked: these bytes were a real document, and
            # the two-identity retry exists for servers that answer with HTML.
            log.warning("  Document served for %s is not %r (title coverage %.2f) — "
                        "refused: %s", doi or url, title[:80], coverage, url)
            return {"success": False, "path": None, "source": "",
                    "reason": "wrong_document", "title_check": verdict,
                    "title_coverage": coverage}

        pdf_path = document_cache_path(doi or url, suffix)
        pdf_path.write_bytes(content)
        try:                     # the URL serves a document after all
            _url_failure_path(url).unlink(missing_ok=True)
        except Exception as e:
            log.debug("URL failure record delete failed (%s): %s", url, e)
        return {"success": True, "path": pdf_path, "source": "download", "reason": "",
                "title_check": verdict, "title_coverage": coverage}

    return {"success": False, "path": None, "source": "", "reason": "not_a_pdf"}


# ── Orchestrator ──────────────────────────────────────────────────────────────

def acquire_pdf(doi_r: str, title: str = "", openalex_id: str = "",
                url_r: str = "") -> dict:
    """
    Try every document source in priority order for *doi_r*.

    *url_r* is the row's own URL, and for 30% of the 2026-08-06 worklist it is the
    only identifier there is. Three tiers use it: the OSF registration lookup, the
    direct download at Tier 0c, and the HTML tier at the bottom.

    Returns:
        pdf_url        str
        pdf_source     str
        pdf_path       str | None
        pdf_ok         bool
        pdf_url_tried  list[str]
        openalex_xml   dict | None  — structured content, from OpenAlex GROBID XML,
                                      an OSF registration form, or a parsed HTML page.
                                      The key's name predates the other two; what it
                                      carries is a sections dict, whatever produced it.
    """
    doi_r     = clean_doi(doi_r)
    url_r     = str(url_r or "").strip()
    dl        = {"success": False, "path": None, "reason": ""}
    pdf_url   = ""
    pdf_src   = ""
    all_tried: list[str] = []
    oa_xml    = None

    # Per-DOI record of which tiers came back empty, and when (PDF_RETRY_AFTER_DAYS).
    # A DOI-less row keys its log on url_r, so it gets a retry log too rather than
    # re-probing every tier on every run.
    retry_key  = doi_r or url_r
    retry_path = _retry_log_path(PDF_CACHE_DIR, retry_key) if retry_key else None
    retries    = _read_retry_log(retry_path) if retry_path is not None else {}
    failures: dict[str, str] = {}
    attempts: dict[str, list[str]] = {}

    def _held(tier: str) -> bool:
        return _retry_suppressed(retries, tier, PDF_RETRY_AFTER_DAYS)

    def _failed(tier: str) -> None:
        failures[tier] = _now_iso()

    def _only_transient(tier: str) -> bool:
        """True when *tier* fetched something and every fetch failed to be answered.

        A fourteen-day suppression is for a source that HAS nothing; a connection
        timeout or a 5xx says nothing at all, and download_pdf reports both as
        download_error. Every other reason — 404/410, not a document, too small, the
        wrong paper — is an answer. A tier that fetched nothing has no attempts here,
        and its emptiness is a real absence.
        """
        tried = attempts.get(tier) or []
        return bool(tried) and all(r.startswith("download_error") for r in tried)

    def _try(url: str, label: str, referer: str = "", want_title: str = "",
             name: str = "") -> bool:
        nonlocal dl, pdf_url, pdf_src
        all_tried.append(url)
        # A tier fetching ANOTHER paper's document checks the bytes against that
        # paper's title, not the row's; everything else asks for the row's own.
        dl = download_pdf(url, doi=doi_r, title=want_title or title, referer=referer)
        if dl["success"]:
            pdf_url, pdf_src = url, label
            # Also written on a download_pdf cache hit, which is how a PDF saved
            # before this record existed acquires one.
            _write_provenance(doi_r or url_r, label, url, dl.get("title_check", ""),
                              dl.get("title_coverage"), name=name)
            return True
        attempts.setdefault(label, []).append(str(dl.get("reason") or ""))
        log.debug("  %s failed (%s): %s", label, dl.get("reason"), url)
        return False

    def _result() -> dict:
        if retry_path is not None:
            # An XML with content is a document too: if its cache is later lost,
            # the download tiers must be probeable again, not held for two weeks.
            if dl["success"] or pdf_src in _STRUCTURED_SOURCES:
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
    # Keyed the way the file itself is keyed — the DOI when there is one, the row's URL
    # otherwise — so a DOI-less row's playwright or row_url document replays its own
    # tier instead of being relabelled by whichever tier re-derives its URL first.
    prov_key = doi_r or url_r
    on_disk = cached_pdf(prov_key) if prov_key else None
    if on_disk is not None and title.strip():
        # This shortcut does not go through download_pdf, so it carries the title
        # check itself — a mis-served file saved by an earlier run would otherwise be
        # replayed here for ever, without any tier ever seeing it. A related_doi
        # document is the REVIEWED work and matches that work's title, not the row's
        # — its acquisition-time verdict (checked against the reviewed title) stands,
        # or the correct preprint would be discarded and re-fetched on every run.
        replay_prov = _read_provenance(prov_key)
        if (replay_prov.get("source") == "related_doi"
                and replay_prov.get("title_check") in ("match", "low")):
            verdict, coverage = replay_prov["title_check"], float(
                replay_prov.get("title_coverage") or 0.0)
        else:
            verdict, coverage = _title_check(on_disk.read_bytes(), on_disk.suffix,
                                             title)
        if verdict == "mismatch":
            log.warning("  [%s] document on disk is not %r (title coverage %.2f) — "
                        "discarding it: %s", prov_key, title[:80], coverage, on_disk)
            _discard_document(prov_key, on_disk)
            on_disk = None
    if on_disk is not None:
        prov = _read_provenance(prov_key)
        if prov.get("source"):
            # The path cached_pdf found, not a rebuilt one: a Word file is saved under
            # .docx, and a rebuilt .pdf path would name a file that is not there.
            dl      = {"success": True, "path": on_disk,
                       "source": "cache", "reason": ""}
            pdf_url = prov["url"]
            pdf_src = prov["source"]
            if pdf_url:
                all_tried.append(pdf_url)
            log.debug("  [%s] PDF already on disk (source=%s)", doi_r, pdf_src)
            return _result()

    # Tier 0b — the OSF registration form. Same deal as Tier 0: it IS the document,
    # so nothing below it can add anything. Some of those forms are PROSPECTIVE — an
    # Open-Ended plan freeze, or a registration Stage 2 could route only on its title
    # because the backfill never gave it a template line — and a plan reads like a
    # result: "we predict a successful replication". Acquiring one is right, coding
    # one is not, and the template guard in `_apply_outcome` is what draws that line.
    osf_guid = osf_registration_guid(doi_r) or osf_registration_guid(url_r)

    # Tier 0a — the manuscript in the OSF project's own file storage, which beats the
    # registration form below: the form describes the study, the uploaded file IS the
    # paper. A project whose storage holds no manuscript falls through to the form.
    if osf_guid and not dl["success"] and not _held("osf_files"):
        try:
            osf_files = list_osf_files(osf_guid)
        except DocumentSourceUnavailable as exc:
            # Never recorded: a source that did not answer has said nothing.
            log.info("  [%s] OSF file listing unavailable: %s", doi_r or url_r, exc)
        else:
            ranked = rank_osf_files(osf_files, title)[:_OSF_MAX_DOWNLOADS]
            if any(_try(entry["download"], "osf_files",
                        name=str(entry.get("name") or "")) for entry in ranked):
                return _result()
            if not _only_transient("osf_files"):
                _failed("osf_files")

    if osf_guid and not dl["success"] and not _held("osf_registration"):
        try:
            registration = get_osf_registration(osf_guid)
        except DocumentSourceUnavailable as exc:
            # NOT recorded as a failure: the retry log suppresses for fourteen days,
            # and a source that never answered has said nothing to remember.
            log.info("  [%s] OSF registration unavailable: %s", doi_r or url_r, exc)
            registration = None
        else:
            if registration is not None:
                log.info("  [%s] OSF registration acquired (source=osf_registration)",
                         doi_r or url_r)
                oa_xml  = registration
                pdf_src = "osf_registration"
                return _result()
            _failed("osf_registration")

    # Tier 0c — the row's own URL, downloaded directly.
    #
    # Every tier below this one derives its candidate URL from the DOI through an
    # external index, and the row's own URL was passed in only to key the retry log
    # and to look for an OSF guid — it was never fetched. Measured 2026-08-08:
    # 10.15456/j1.2025082.1931045150 carries
    # `econstor.eu/bitstream/10419/318610/1/JCRE-2025-2.pdf`, a direct PDF link that
    # answers 200 with 774,018 bytes to this pipeline's own User-Agent, and all eight
    # DOI-keyed tiers had recorded empty for it the day before. They had nothing to
    # say: 10.15456 is not a Crossref registrant, and Unpaywall covers Crossref only.
    # 553 of the 568 no-document rows in the 2026-08-07 export carry a url_r, and 223
    # of them carry one and no DOI at all.
    #
    # It goes above the DOI-keyed tiers because it is free, needs no API and no DOI,
    # and the row already holds the answer. It stays below Tiers 0 and 0b because
    # those hand back parsed structure rather than a file to parse.
    #
    # HTML is left entirely to Tier 11. A page that is really a paper is worth
    # reading, but deciding that takes html_document_has_content() and the section
    # splitter, and running them here would put the weakest evidence in the
    # waterfall ABOVE every PDF tier. download_pdf refuses the page as "not_a_pdf",
    # which is a tier failure and not a URL verdict, so the row falls through to the
    # PDF tiers and reaches Tier 11 with that page still available.
    if url_r.lower().startswith(("http://", "https://")) and not _held("row_url"):
        if not _try(url_r, "row_url"):
            _failed("row_url")

    # Tier 0d — the paper this DOI is a REVIEW OF. A Peer Community In recommendation
    # (10.24072/pci.rr.*) has no full text of its own worth coding; its Crossref record
    # names the preprint it reviewed, and that preprint is the paper. The registrant
    # gate is what keeps the tier to that shape, and it also keeps the Crossref lookup
    # off every other DOI'd row's path.
    if (not dl["success"] and doi_r and not _held("related_doi")
            and doi_r.startswith(_REVIEW_RELATION_REGISTRANTS)):
        try:
            reviewed = crossref_reviewed_doi(doi_r)
        except DocumentSourceUnavailable as exc:
            log.info("  [%s] Crossref unavailable: %s", doi_r, exc)
        else:
            if reviewed:
                urls, outage = document_urls_for_doi(reviewed)
                # The bytes are checked against the REVIEWED paper's title: a
                # recommendation is titled in the recommender's own words, and
                # checking the preprint against those refuses the right document.
                try:
                    reviewed_title = crossref_title(reviewed)
                except DocumentSourceUnavailable as exc:
                    log.info("  [%s] reviewed title unavailable: %s", reviewed, exc)
                    reviewed_title = ""
                log.info("  [%s] is a review of %s — trying its %d document URLs",
                         doi_r, reviewed, len(urls))
                if any(_try(url, "related_doi", want_title=reviewed_title)
                       for url in urls):
                    return _result()
                if not (outage or _only_transient("related_doi")):
                    _failed("related_doi")

    # Tier 1 — arXiv direct (before any API calls; the URL is a DOI pattern, so a
    # non-arXiv DOI is not a tier failure — there was nothing to ask.)
    # The `not dl["success"]` guard every tier below carries: this one used to be
    # first and had nothing above it to succeed.
    arxiv = get_arxiv_pdf_url(doi_r, title) if not dl["success"] else None
    if arxiv and not _held("arxiv"):
        if not _try(arxiv, "arxiv"):
            _failed("arxiv")

    # Tier 2 — OSF preprint
    if not dl["success"] and not _held("osf"):
        osf = get_osf_pdf_url(doi_r)
        if osf and not _try(osf, "osf"):
            _failed("osf")

    # Tier 3 — every copy OpenAlex knows of, not only best_oa_location's one URL.
    # Landing pages included: two of the 88 measured location URLs answered with a
    # PDF, and both were landing pages. The ones that answer with HTML are refused as
    # "not_a_pdf" and go on to Tier 8's scraper.
    oa_locations: list[dict] = []
    if not dl["success"] and not _held("openalex_oa"):
        try:
            oa_locations = get_openalex_locations(doi_r)
        except DocumentSourceUnavailable as exc:
            log.info("  [%s] OpenAlex locations unavailable: %s", doi_r, exc)
        else:
            if not any(_try(cand["url"], "openalex_oa") for cand in oa_locations):
                _failed("openalex_oa")

    # Tier 3b — DataCite, the registrant's own metadata. Every index above is
    # Crossref-shaped, so a repository DOI reaches this tier having been asked about
    # nowhere it is registered.
    datacite: list[dict] = []
    if not dl["success"] and not _held("datacite"):
        try:
            datacite = get_datacite_urls(doi_r)
        except DocumentSourceUnavailable as exc:
            log.info("  [%s] DataCite unavailable: %s", doi_r, exc)
        else:
            if not any(_try(cand["url"], "datacite") for cand in datacite):
                _failed("datacite")

    # Tier 4 — Unpaywall direct PDFs
    # Every other tier is guarded by `if not dl["success"]`; this one was not, so a
    # DOI already served by arXiv/OSF/OpenAlex still cost an Unpaywall round-trip.
    # Tier 8 is the only other consumer of uw_landing/uw_direct and it sits inside
    # an `if not dl["success"]` block, so the empty list is never reached.
    need_unpaywall = (not dl["success"]
                      and not (_held("unpaywall_pdf") and _held("landing")))
    uw_all: list[dict] = []
    if need_unpaywall:
        try:
            uw_all = get_all_unpaywall_pdf_urls(doi_r)
        except DocumentSourceUnavailable as exc:
            # Unpaywall did not answer. Recording that would hold both tiers it feeds
            # for fourteen days on a minute of API trouble.
            log.info("  [%s] Unpaywall unavailable: %s", doi_r, exc)
            need_unpaywall = False
    uw_direct  = [u for u in uw_all if u["type"] == "pdf"]
    uw_landing = [u for u in uw_all if u["type"] == "landing"]

    if need_unpaywall and not dl["success"] and not _held("unpaywall_pdf"):
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

    # Tier 7b — the paper found by its own title in Crossref. The ONLY route for a row
    # with no DOI: every tier above is keyed on one, and 223 of the 2026-08-07
    # no-document rows have none. It also reaches the rows whose DOI is dead in every
    # index above but whose paper is indexed under another one.
    #
    # Two gates, because acquiring some other paper's document is the failure this
    # module already spends a title check on: the Crossref record's title must match
    # the row's in both directions before anything is fetched, and the bytes must then
    # pass _title_check like any other download.
    # A title too short to search is a SKIP, not a failure: the tier was never asked,
    # and a row whose title is filled in tomorrow must reach it tomorrow.
    if (not dl["success"] and len(title.strip()) >= _MIN_SEARCH_TITLE_CHARS
            and not _held("crossref_search")):
        try:
            hits = crossref_title_matches(title)
        except DocumentSourceUnavailable as exc:
            log.info("  [%s] Crossref search unavailable: %s", doi_r or url_r, exc)
        else:
            outage = False
            won = False
            for hit_doi in hits:
                if hit_doi == doi_r:
                    continue        # the DOI-keyed tiers above already asked
                urls, hit_outage = document_urls_for_doi(hit_doi)
                outage = outage or hit_outage
                if any(_try(url, "crossref_search") for url in urls):
                    won = True
                    break
            if won:
                return _result()
            if not (outage or _only_transient("crossref_search")):
                _failed("crossref_search")

    # Tier 8 — Scrape Unpaywall landing pages
    # The landing pages of every index that named one, not Unpaywall's alone: an
    # OpenAlex or DataCite landing page that answered this run with HTML may still be
    # a page with a download link on it, which is what the scraper is for.
    landing_all = uw_landing + [c for c in oa_locations + datacite
                                if c["type"] == "landing"]
    if not dl["success"] and not _held("landing"):
        won = False
        for cand in landing_all:
            # Every link the page offers, in turn, and each fetched as if the click had
            # come from that page: a repository that 403s the first href often serves
            # the second, and several 403 a request with no Referer at all.
            for scraped in scrape_pdf_from_landing_page(cand["url"]):
                if _try(scraped, f"landing_{cand['host'] or 'repo'}",
                        referer=cand["url"]):
                    won = True
                    break
            if won:
                break
        # need_unpaywall is False here only after an Unpaywall outage: the
        # both-tiers-held case cannot enter this block and the success case fails its
        # own guard. Recording a failure then would suppress the tier for fourteen
        # days having never seen the landing pages Unpaywall would have named.
        if not won and need_unpaywall:
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
        pw_result = get_pdf_via_playwright(doi_r, title=title, url_r=url_r)
        if pw_result["success"]:
            pdf_url = f"https://doi.org/{doi_r}" if doi_r else url_r
            pdf_src = "playwright"
            dl      = pw_result
            all_tried.append(pdf_url)
            _write_provenance(doi_r or url_r, "playwright", pdf_url,
                              pw_result.get("title_check", ""),
                              pw_result.get("title_coverage"))
        elif pw_result.get("reason") not in _PLAYWRIGHT_SKIP_REASONS:
            _failed("playwright")

    # Tier 11 — the row's own page, as HTML. Last, because it is the weakest evidence
    # and the only tier whose result has to be argued for rather than downloaded: an
    # abstract-restating record page is refused by html_document_has_content(), so a
    # row that reaches here still ends at no_fulltext_available unless the page really
    # carries the paper.
    if not dl["success"] and url_r and not _held("html_landing"):
        try:
            document = get_html_document(url_r)
        except DocumentSourceUnavailable as exc:
            log.info("  [%s] HTML source unavailable: %s", url_r[:70], exc)
            document = None
        else:
            if document is not None:
                log.info("  [%s] HTML document acquired (source=html_landing)",
                         url_r[:70])
                oa_xml  = document
                pdf_url = url_r
                pdf_src = "html_landing"
                all_tried.append(url_r)
                return _result()
            _failed("html_landing")

    return _result()
