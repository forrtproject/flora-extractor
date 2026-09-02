"""
utils.py — Common helpers shared across all pipeline stages.

Public API:
    clean_doi(doi) → str
    cache_key(text) → str
    pdf_serve_url(doi_r, result) → str
"""
import hashlib
import re
from pathlib import Path

from filelock import FileLock


def csv_lock(path, timeout: float = -1) -> FileLock:
    """Cross-process lock guarding a shared CSV against read-modify-write vs append races.

    `data/extracted.csv` has one writer now (`extract/export.py`, which renames a temp
    file into place), so the race this was built for is gone from that path. It still
    guards the CSVs the `tools/` backfills rewrite in place, where one read-modify-write
    can still clobber another. timeout=-1 blocks until acquired.
    """
    return FileLock(f"{path}.lock", timeout=timeout)


def clean_doi(doi: str) -> str:
    """
    Strip URL prefix from a DOI string and normalise to lowercase.

    Examples:
        "https://doi.org/10.1037/abc123" → "10.1037/abc123"
        "http://dx.doi.org/10.1037/abc123" → "10.1037/abc123"
        "doi:10.1037/abc123"               → "10.1037/abc123"
        "10.1037/abc123/"                  → "10.1037/abc123"
        "10.1037/abc123"                   → "10.1037/abc123"
    """
    if not doi:
        return ""
    doi = str(doi).strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:", "", doi, flags=re.IGNORECASE)
    return doi.strip().lower().rstrip("/")


def bare_work_id(value: str) -> str:
    """
    Normalise an OpenAlex work identifier to its bare form.

    Stage 1 stores openalex_id_r as the full URL, while the validation DB and the
    OpenAlex "work ID" users look up are the bare W-number. Anything that is not a
    W-id (an author/source/institution id, a stray DOI, junk) returns "".

    Examples:
        "https://openalex.org/W2884670852" → "W2884670852"
        "W2884670852"                      → "W2884670852"
        "w2884670852"                      → "W2884670852"
        "https://openalex.org/A5023888391" → ""
    """
    if not value:
        return ""
    tail = str(value).strip().rstrip("/").rsplit("/", 1)[-1]
    return tail.upper() if re.fullmatch(r"[Ww]\d+", tail) else ""


_NON_ARTICLE_DOI_RE = re.compile(r"/reviews/|/decisions/", re.IGNORECASE)

# Prefixes belonging to data repositories that mint DOIs for deposits only — the
# dataset that accompanies a paper, never the paper (issue #141). Each was confirmed
# against DataCite: the prefix's registrant is a data repository and a sample of its
# holdings is deposits throughout (Harvard Dataverse alone carries 816k DOIs, 642 of
# them typed as text, and those are deposited codebooks, not journal articles).
#
# EXCLUDE-ONLY and deliberately incomplete. A prefix belongs here only when it is
# positively identified — a mixed-content prefix would silently drop real papers, so
# Zenodo (10.5281) is absent: CODECHECK certificates live there and the reproduction-of
# arm admits them on purpose. Instances on prefixes not listed here are the title
# rule's job, not this one's.
# APA files PsycEXTRA under 10.1037/e<digits>-<digits>: conference abstracts, posters,
# government reports and unpublished instruments, typed `dataset` in Crossref. The
# published article by the same authors carries an ordinary 10.1037 DOI, so the two
# are told apart by the leading "e" and nothing else. Both wrong originals the Stage 3
# evaluation could not otherwise reach were one of these — "Olivola & Shafir (2013)"
# matched an Olivola conference abstract instead of the martyrdom-effect paper, and
# the surveillance-task replication matched an abstract instead of Olson & Fazio
# (2001). A record of a talk is not the study a replication re-tested.
_PSYCEXTRA_DOI_RE = re.compile(r"^10\.1037/e\d+-\d+$")

# PsycTESTS measure records ("Denial of Mind to Animals … Measure"). NOT a
# non_article_doi drop: FLoRA's curated data records t-DOIs as doi_o on 3 rows
# (flora.csv, checked 2026-08-08), so a hard "never a study" rule would contradict
# curated practice. The pooled resolver carries it as a candidate FLAG instead.
_PSYCTESTS_DOI_RE = re.compile(r"^10\.1037/t\d+-\d+$")


def psyctests_doi(doi: str) -> bool:
    """Whether *doi* is a PsycTESTS measure record rather than an article."""
    return bool(_PSYCTESTS_DOI_RE.match(clean_doi(doi)))

_DATA_REPOSITORY_PREFIXES = frozenset({
    "10.7910",   # Harvard Dataverse
    "10.3886",   # ICPSR / openICPSR
    "10.34894",  # DataverseNL
    "10.18710",  # DataverseNO
    "10.18170",  # Peking University Open Research Data Platform
    "10.21979",  # DR-NTU (Data), NTU Singapore
    "10.11587",  # AUSSDA — Austrian Social Science Data Archive
    "10.15139",  # UNC Dataverse
    "10.18738",  # Texas Data Repository
    "10.2905",   # European Commission JRC Data Catalogue
})


def non_article_doi(doi: str) -> str:
    """Reason string if *doi* is a non-article object (not a study), else "".

    figshare (10.6084) DOIs are data records / figures / posters; a "/reviews/" or
    "/decisions/" segment marks a peer-review or editorial object
    (e.g. 10.7287/peerj.10325v0.1/reviews/2). Neither is the replication itself, so
    both are Stage-2 false positives (issue #17).

    A DOI on a data-repository prefix (_DATA_REPOSITORY_PREFIXES) is the deposit that
    accompanies a paper — "Replication Data for: …" — and carries the word replication
    into the pipeline without being a study (issue #141). The deposit is evidence that
    a replication exists, but the paper itself is in the corpus under its own DOI, so
    discarding the deposit loses nothing.
    """
    doi = clean_doi(doi)
    if not doi:
        return ""
    if doi.startswith("10.6084/"):
        return "figshare_data_record"
    if _PSYCEXTRA_DOI_RE.match(doi):
        return "psycextra_record"
    if doi.split("/", 1)[0] in _DATA_REPOSITORY_PREFIXES:
        return "data_repository_deposit"
    if _NON_ARTICLE_DOI_RE.search(doi):
        return "peer_review_object"
    return ""


# ── What kind of OSF object a row is ─────────────────────────────────────────
# OpenAlex names the HOST, not the object: `primary_location.source.display_name` is
# "OSF Preprints" for everything OSF serves, whether it is a preprint, a project or a
# registration, and that string becomes `journal_r` (`_row_from_snapshot()` in
# search/snapshot_scan.py). Measured over the 1,674 OSF rows in the exported CSVs, it
# mislabels 395 `10.17605` DOIs — the projects/registrations registrant — as preprints,
# while leaving 219 DOIs minted by genuine preprint servers with no journal at all.
# OpenAlex also contradicts itself on that registrant: 395 rows say "OSF Preprints" and
# 313 say "Open Science Framework" for identically-minted `10.17605` DOIs. Who MINTED
# the DOI is therefore a better signal than what OpenAlex calls the source, and it
# needs no network call — which matters because the OSF API is unreliable.

# The registrant OSF mints project and registration DOIs under. Telling those two
# apart is Stage 2's job (the `osf-registration-*` specs); from the DOI alone they are
# one thing, so this does not pretend to separate them.
OSF_REGISTRATION_PREFIX = "10.17605"

# Every registrant under which the pool holds a DOI encoding an `osf.io` guid: the
# registrations above plus the branded preprint servers OSF hosts (PsyArXiv 10.31234,
# SocArXiv 10.31235, EarthArXiv 10.31223, …). Measured over the pool.
OSF_OWN_PREFIXES = frozenset({
    "10.1149", "10.17605", "10.31219", "10.31220", "10.31221", "10.31222", "10.31223",
    "10.31224", "10.31225", "10.31226", "10.31227", "10.31228", "10.31229", "10.31230",
    "10.31231", "10.31232", "10.31233", "10.31234", "10.31235", "10.31236", "10.31237",
    "10.31730", "10.32942", "10.33767", "10.34055", "10.35542", "10.35543", "10.37044",
})

# Derived, so the two cannot drift apart: everything OSF mints that is not the
# registration registrant is one of its preprint servers.
OSF_PREPRINT_PREFIXES = OSF_OWN_PREFIXES - {OSF_REGISTRATION_PREFIX}

# A preprint differs in URL SHAPE, not in kind: OSF serves preprints at
# `osf.io/preprints/<server>/<guid>` and everything else at a bare `osf.io/<guid>`
# (the same distinction `osf_registration_guid()` in shared/pdf_sources.py parses).
# That shape is what decides the 726 OSF-labelled rows carrying no DOI at all.
_OSF_URL_RE = re.compile(r"(?:^|/|\.)osf\.io/", re.IGNORECASE)
_OSF_PREPRINT_URL_RE = re.compile(r"osf\.io/preprints/", re.IGNORECASE)

OSF_PREPRINT = "preprint"
OSF_PROJECT_OR_REGISTRATION = "project_or_registration"


def osf_type(doi: str, url: str = "") -> str:
    """Which kind of OSF object a row is, or "" when it is not on OSF.

    `preprint` is what FLoRA wants; `project_or_registration` tells a validator to go
    looking for the preprint or published paper rather than code the project as a
    study. The DOI is asked first, because the registrant is a fact about who minted
    it; the URL only decides rows that carry no DOI.

    Deliberately NOT a claim about whether the work is a replication — a project may
    well hold one. It replaces a wrong label with an honest one, nothing more.
    """
    doi = clean_doi(doi)
    prefix = doi.split("/", 1)[0] if doi else ""
    if prefix == OSF_REGISTRATION_PREFIX:
        return OSF_PROJECT_OR_REGISTRATION
    if prefix in OSF_PREPRINT_PREFIXES:
        return OSF_PREPRINT
    target = (url or "").strip()
    if not _OSF_URL_RE.search(target):
        return ""
    return (OSF_PREPRINT if _OSF_PREPRINT_URL_RE.search(target)
            else OSF_PROJECT_OR_REGISTRATION)


# "Replication Data for: X", "Replication data set for X", and the Dataverse volume
# form "Vol. 16(2): Replication Data for: X". Anchored at the start and requiring the
# noun phrase whole, so "Replication Data Analysis in Psychology" — a paper about
# replication data — does not match.
_DEPOSIT_TITLE_RE = re.compile(
    r"^(?:vol\.\s*\d+\(\d+\):\s*)?replication data\s+(?:for|set)\b", re.IGNORECASE)


def non_article_title(title: str) -> str:
    """Reason string if *title* names a data deposit rather than a study, else "".

    Second, independent check behind non_article_doi(): the prefix list catches the
    repositories we enumerated, this catches instances on the ones we did not (issue
    #141). Title evidence is weaker than DOI evidence, which is why the pattern is
    anchored and narrow — a title merely mentioning replication data is a paper.

    Deliberately does NOT match reproduction packages that are themselves the research
    output — CODECHECK certificates, artifact-evaluation records, reproducibility
    reports — which the reproduction-of arm (#137) admits on purpose.
    """
    title = str(title or "").strip()
    if not title:
        return ""
    return "data_repository_deposit" if _DEPOSIT_TITLE_RE.match(title) else ""


_NON_ARTICLE_TYPES = {
    "dataset": "dataset_record",
    "database": "dataset_record",
    "software": "software_record",
    "peer-review": "peer_review_object",
    "supplementary-materials": "supplementary_material",
    "component": "supplementary_material",
    "paratext": "paratext_record",
    "libguides": "library_guide",
    "grant": "grant_record",
    "standard": "standard_document",
}


def non_article_type(work_type: str) -> str:
    """Reason string if the OpenAlex/CrossRef work *type* is a non-study object, else "".

    Companion to non_article_doi(): that one pattern-matches DOI strings, this one uses
    the work type the registry reports. A hand-check of 50 provisionally-linked rows
    found 23 were not studies — Dataverse/Zenodo/Mendeley deposits and eLife
    "Author response" objects — none of which the DOI patterns catch, but all of which
    carry a giveaway type (dataset, peer-review, ...).

    EXCLUDE-ONLY, never require. Only types affirmatively known not to be studies are
    rejected; an empty, missing or unrecognised type returns "". Do NOT rewrite this as
    an allow-list of acceptable types: `type` coverage is incomplete and inconsistent
    across CrossRef, OpenAlex and content negotiation, so requiring a known-good type
    would silently drop real replications.

    "other" is deliberately NOT excluded — it is the registries' catch-all for anything
    unclassified, including ordinary articles. "data-paper" and "software-paper" are
    papers *about* data/software and are likewise kept, which is why matching is exact
    rather than by substring.
    """
    t = str(work_type or "").strip().lower().replace("_", "-")
    return _NON_ARTICLE_TYPES.get(t, "")


def cache_key(text: str) -> str:
    """
    Return a stable, filesystem-safe cache key for *text*.

    Uses MD5 (not cryptographic — just for deduplication).
    """
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def pdf_serve_url(doi_r: str, result: dict) -> str:
    """URL path to serve the cached PDF for *doi_r*, or "" if none is cached."""
    from shared.config import PDF_CACHE_DIR

    if result.get("pdf_path"):
        return f"/pdf/{Path(result['pdf_path']).name}"
    expected = PDF_CACHE_DIR / f"{cache_key(doi_r)}.pdf"
    return f"/pdf/{expected.name}" if expected.exists() else ""


# ── Titles that are really citation strings ──────────────────────────────────
# A reference parsed out of a PDF without GROBID structure arrives as the raw
# citation line — "[2] L.J.T. Balter, et al., Low-grade inflammation decreases
# emotion recognition …, Brain Behav. Immun. 73 (2018) 216–221." — and a numbering
# marker or an author list in front of the title names no paper that a title search,
# a validator or doi_verify's Jaccard comparison can match.
#
# Two rules below, and they do different jobs. clean_citation_title() strips what is
# demonstrably not part of the title, and only when the evidence for that is a
# following author list — "[12] Angry Men" and "3. Methods for …" are titles.
# usable_title() judges what is left. It gates CONFIDENCE, title searches and
# verification — never a record's existence: a reference dropped from the @key
# namespace is invisible to the target prompt and is not counted as a shortfall,
# which is worse than one carrying an awkward title.

# Shorter than this, punctuation aside, is boilerplate ("n/a", "unknown") — not a
# title we can search on. Legitimate short titles ("Nudge") fail it too, which is
# why it may only cost a row its confidence.
MIN_USABLE_TITLE = 10

# "[2] ", "(2) ", "3. " — entry numbering in Vancouver/numeric reference lists.
_REF_MARKER_RE = re.compile(r"^\s*(?:[\[(]\d{1,3}[\])][.,)]?|\d{1,3}\.)\s+")

_INITIALS = r"(?:[A-Z]\.[-\s]*){1,4}"
# One author in a numeric-style list, up to its separator: "L.J.T. Balter, " or
# "Moieni M.R., ". Requires the separator, so a title's first words are never
# mistaken for a name.
_AUTHOR_CHUNK_RE = re.compile(
    rf"^(?:{_INITIALS}(?:[A-Z][\w'’\-]+\s+)?[A-Z][\w'’\-]+"
    rf"|[A-Z][\w'’\-]+,?\s+{_INITIALS})\s*(?:[,;&]|\band\b)\s*")
# "et al., " continues an author list; on its own it is no evidence of one, so it
# is only ever stripped after a real name has been.
_ET_AL_RE = re.compile(r"^et\.?\s*al\.?\s*(?:[,;&]|\band\b)\s*", re.IGNORECASE)
# A citation cut mid-list, so it opens on the conjunction before its last author:
# "and D. Tzovaras, Emotion recognition …". Only stripped when a name follows —
# "And Then There Were None" is a title.
_LEADING_CONJUNCTION_RE = re.compile(r"^(?:and|&)\s+", re.IGNORECASE)

# A citation cut off mid-author-list: "… , M.R", "… , J.".
_TRUNCATED_AUTHORS_RE = re.compile(r",\s*(?:[A-Z]\.){1,3}[A-Z]?\s*$")
# Nothing but initials — what is left when a truncated citation is cleaned ("M.R").
_INITIALS_ONLY_RE = re.compile(r"^(?:[A-Z]\.[-\s]*){1,4}[A-Z]?\.?$")

# Unicode-aware: a Cyrillic or CJK title is a title, and stripping it to nothing
# would make every non-Latin original unusable.
_TITLE_NORM_RE = re.compile(r"[\W_]+", re.UNICODE)


def _strip_reference_marker(text: str) -> str:
    """Drop a leading "[2] "/"3. " ONLY when an author list follows it — that is the
    only evidence that the number is bibliography numbering rather than the title
    ("[12] Angry Men", "3. Methods for Estimating Prevalence")."""
    match = _REF_MARKER_RE.match(text)
    if not match:
        return text
    rest = text[match.end():].lstrip()
    return rest if _AUTHOR_CHUNK_RE.match(rest) else text


def clean_citation_title(title: str) -> str:
    """Strip a reference-entry marker and any leading author list off *title*.

    Returns the title portion of a raw citation string; a title that is already a
    title comes back unchanged. Never strips everything away — a string that is
    nothing but authors is returned as-is for `usable_title()` to judge.
    """
    text = _strip_reference_marker(str(title or "").strip())
    conjunction = _LEADING_CONJUNCTION_RE.match(text)
    if conjunction and _AUTHOR_CHUNK_RE.match(text[conjunction.end():]):
        # "and D. Tzovaras, “Is Popularity …”" — a citation cut before its last
        # author. `citation_fragment` still calls the raw string a fragment, which is
        # what demotes the row's confidence; stripping it here is what leaves a title
        # to search on.
        text = text[conjunction.end():]
    stripped_a_name = False
    while True:
        match = _AUTHOR_CHUNK_RE.match(text) or (
            _ET_AL_RE.match(text) if stripped_a_name else None)
        if not match or not text[match.end():].strip():
            return text
        stripped_a_name = True
        text = text[match.end():].strip()


def citation_fragment(title: str) -> bool:
    """True when *title* is a piece of a citation rather than a title: it leads with
    an author list, or it was cut off mid-initials ("M. Moieni, M.R").

    Shape only — length says nothing here, because "Nudge" and "Grit" are titles.
    """
    text = _strip_reference_marker(str(title or "").strip())
    if not text:
        return True
    conjunction = _LEADING_CONJUNCTION_RE.match(text)
    if conjunction and _AUTHOR_CHUNK_RE.match(text[conjunction.end():]):
        return True
    return bool(_AUTHOR_CHUNK_RE.match(text) or _TRUNCATED_AUTHORS_RE.search(text)
                or _INITIALS_ONLY_RE.match(text))


def author_surname(author: str) -> str:
    """Best-effort surname from 'Surname', 'Surname, First' or 'First Surname'.

    The comma case is what makes this more than `.split()[-1]`: registries and
    reference lists write the inverted form, so the last token of "Balter, L." is the
    INITIAL. Written into `authors_o` that way, it is a name no author list carries
    and `author_matches()` can never pass.
    """
    author = str(author or "").strip()
    if not author:
        return ""
    if "," in author:
        return author.split(",")[0].strip()
    parts = author.split()
    return parts[-1] if parts else ""


# A glued article id — PLOS ONE's "e77661", eLife's "e00090" — sits in the citation
# tail, and a title index has no such token, so it ANDs a word into the query that no
# record can satisfy.
_ARTICLE_ID_RE = re.compile(r"\b[eE]\d{4,}\b")
_QUOTED_TITLE_RE = re.compile(r"[\"“]([^\"”]{15,})[\"”]")
# ". Journal Name 84(3), 600-621" — the citation tail a reference line carries after
# the title.
_JOURNAL_TAIL_RE = re.compile(r"\.\s+[A-Z][^.]{0,60}\s+\d{1,4}\s*\(")
_VOLUME_PAGES_RE = re.compile(r",?\s*\d{1,4}\s*\(\d+\)\s*,?\s*[\d–\-]+\s*$")


def clean_search_query(text: str) -> str:
    """*text* as a title query a registry can match — the citation chrome removed.

    What is searched for is however the replication wrote its target, which is
    routinely a whole reference line. Every word of chrome in it is a word the title
    index must also match, so the journal tail, the URL, the DOI string and the
    article id each cost the query the paper it names. Returns "" only for an empty
    input; a string this cannot improve comes back as it went in.
    """
    text = str(text or "").replace("\t", " ").replace("’", "'")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\bdoi:\s*\S+", " ", text, flags=re.I)
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)   # "cog- nition" -> "cognition"
    quoted = _QUOTED_TITLE_RE.search(text)
    if quoted:
        text = quoted.group(1)
    text = _JOURNAL_TAIL_RE.split(text)[0]
    text = _VOLUME_PAGES_RE.sub("", text)
    text = _ARTICLE_ID_RE.sub(" ", text)
    text = re.sub(r"^\s*references\b[:.]?\s*", "", text, flags=re.I)
    # Entry numbering is stripped only on the same evidence `clean_citation_title`
    # asks for — a following author list — because "12 Angry Men" is a title.
    text = _strip_reference_marker(text)
    return re.sub(r"\s+", " ", text).strip(" .,;“”\"'")


def usable_title(title: str) -> bool:
    """True when *title* can stand for a paper in a title search or a title
    comparison: long enough to search on, and not a citation fragment.

    False is a statement about what can be DONE with the string, not about whether
    the record it belongs to is real — callers demote confidence and skip title
    lookups on a False, they never discard the record.
    """
    text = _strip_reference_marker(str(title or "").strip())
    if len(_TITLE_NORM_RE.sub(" ", text).strip()) < MIN_USABLE_TITLE:
        return False
    return not citation_fragment(text)


_ABBREV_RE = re.compile(
    r"\b(?:et al|e\.g|i\.e|vs|Dr|Mr|Mrs|Ms|Prof|Fig|No|Vol|pp|cf)\."
    r"|(?<!\w)\b[A-Z]\.",
    re.IGNORECASE,
)


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """
    Return (start, end) character offsets into *text* for each sentence.

    Splits on whitespace following sentence-ending punctuation, while protecting
    common abbreviations (et al., e.g., Dr., single-letter initials like "J.") from
    being treated as sentence boundaries. Offsets index into the original *text*
    unchanged (the abbreviation mask is applied to a same-length working copy only),
    so callers can directly compare citation/phrase match offsets against these spans.
    """
    if not text:
        return []
    masked = _ABBREV_RE.sub(lambda m: "\x00" * len(m.group(0)), text)
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?])\s+", masked):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def reconstruct_abstract(inverted_index: "dict | None") -> "str | None":
    """Reconstruct plain abstract text from an OpenAlex inverted index.

    OpenAlex represents abstracts as a mapping of ``{word: [positions]}``. This
    reverses the mapping into position order and joins the tokens. Returns
    ``None`` when no abstract data is provided.

    Shared because both ends of the abstract supply chain decode the same shape:
    the snapshot scanner reads it out of the raw snapshot rows, and the abstract
    backfill reads it out of the API. One decoder means the two can never drift.
    """
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[k] for k in sorted(positions)) if positions else None
