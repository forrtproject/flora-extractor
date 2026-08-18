"""
routes/dashboard.py — Read-only monitoring dashboard.

The page shell and the set-aside CSVs. The three bands fetch their own numbers from
api_flow / api_analysis / api_concerns; the Supabase views moved to api_validation.

Routes:
  GET  /dashboard                        → dashboard page
  GET  /api/dashboard/set-stats          → one set-aside pile's size
  GET  /api/dashboard/set-rows           → a page of one set-aside pile
  GET  /api/dashboard/set-download       → one set-aside CSV as attachment
  GET  /api/dashboard/download           → stream a raw pipeline CSV as attachment
"""
import datetime
import shutil

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR
from shared.schema import SET_ASIDE_DESTINATIONS

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard")



# ── Set-aside CSVs ────────────────────────────────────────────────────────────
# Rows the pipeline deliberately kept OUT of extracted.csv, each in its own file.
# WHICH files exist is not restated here: the set-aside tabs are built from
# shared.schema.SET_ASIDE_DESTINATIONS, the one place a destination is named, so a
# destination sanity_check writes cannot be missing a tab and a tab cannot point at a
# file nothing writes. This map holds only the display copy, keyed by filename; a
# destination with no entry still gets a tab, under a title derived from its filename.
_SET_ASIDE_COPY: dict[str, dict] = {
    "keyed_link_disputed.csv": {
        "title": "Keyed Link Disputed",
        "why": "An LLM accepted this original from the keyed candidate/reference list, and a "
               "second, independent check — shown only the study's title/abstract, the quoted "
               "evidence and the matched record, with no knowledge of the first answer — "
               "confidently said it is not the target the paper named (issue #186). The link, "
               "the outcome and BOTH readings are kept so a human can adjudicate.",
        "action": "Read the two readings side by side. A confident disagreement is the one "
                  "signal that catches the right list with the wrong entry picked from it, "
                  "which DOI verification passes as 'verified'.",
    },
    "unidentified_original.csv": {
        "title": "Unidentified Original",
        "why": "The paper names an original and the link was KEPT — but the matched record "
               "carries neither a DOI nor an OpenAlex id, typically because it came from a "
               "garbled parsed reference. Without an identifier the pair cannot be keyed, so "
               "it cannot enter validation however real the link is.",
        "action": "Recoverable by better reference parsing or acquisition (#188); re-run with "
                  "--redo-status unidentified_original once that improves.",
    },
    "not_a_replication.csv": {
        "title": "Not a Replication",
        "why": "The full-text outcome pass answered record_type_check=neither — the text does not "
               "describe a real attempt to replicate or reproduce the named original. These are "
               "Stage 2 false positives that survived the phrase gate.",
        "action": "Spot-check for classifier over-rejection; genuine misses should be promoted back.",
    },
    "prescreen_discard.csv": {
        "title": "Pre-screen Discards",
        "why": "The optional cheap pre-screen (two very small models, both answering that the "
               "paper is clearly out of scope) ended these rows before the validated front-door "
               "screen ever voted. It is a weaker instrument than that screen and its discards "
               "are terminal, so they are kept separate rather than mixed into Not a Replication.",
        "action": "Sample these regularly while the cheap tier is running — nothing else in the "
                  "pipeline ever looks at them again.",
    },
    "provisional_title_search.csv": {
        "title": "Provisional Title Search",
        "why": "The original was matched against the whole literature by title search "
               "(link_method = llm_title_search) rather than picked from a candidate list. At ~50% "
               "measured precision the DOI is usually a real paper — just not this paper's target — "
               "so the link is provisional and never imported.",
        "action": "Confirm or reject each link by hand; confirmed ones can be promoted back.",
    },
    "search_link_unconfirmed.csv": {
        "title": "Search Link Unconfirmed",
        "why": "The original was found by a search rung (llm_title_search or "
               "llm_author_year_search) and the cold confirmation call graded the link "
               "likely_target, unlikely_target or clearly_not_target rather than "
               "clearly_target. The link, the outcome and the grade are all on the row; "
               "only the import is withheld.",
        "action": "Read the search_confirm grade and reasoning in link_evidence; confirmed "
                  "links can be shipped with `python -m extract.export --release <id> "
                  "--include-unconfirmed-search`.",
    },
    "api_error.csv": {
        "title": "API Error",
        "why": "The row carries no verdict at all: a provider or registry call failed after its "
               "retries. Transient, not settled — the work stays in the extract tier's worklist "
               "and the next run retries it.",
        "action": "Nothing, unless they persist: then check provider status and quotas.",
    },
    "no_original_found.csv": {
        "title": "No Original Found",
        "why": "The LLM ran with full context and concluded no identifiable original study exists — "
               "usually a Stage 2 false positive or a self-replication. Settled: the tier will not "
               "pay to reproduce the verdict without --redo.",
        "action": "Sample for genuine originals the model failed to name.",
    },
    "screen_disagreement.csv": {
        "title": "Screen Disagreement",
        "why": "Historical: the two front-door voters split, back when a split ended the row. The "
               "gate now proceeds on a split, so nothing new lands here.",
        "action": "Re-screening the works in Stage 2 under a changed voter pair or prompt puts "
                  "them back in the extract tier's worklist; the next export re-files them.",
    },
    "unresolved_doi_mismatch.csv": {
        "title": "Unresolved DOI Mismatch",
        "why": "doi_o pointed at a paper whose title/year did not match the resolved original, and "
               "re-resolution from title+author found no confident replacement. A wrong DOI is worse "
               "than a flagged one, so these are held back rather than guessed.",
        "action": "Resolve the original by hand, or confirm no original exists.",
    },
    "unregistered_original_doi.csv": {
        "title": "Unregistered Original DOI",
        "why": "doi_o looked like a registered DOI (plausible publisher prefix) but resolves "
               "nowhere — doi.org 404, absent from CrossRef and OpenAlex. Either the original "
               "was cited with a DOI that was never registered, or the record is wrong.",
        "action": "Discard, or re-resolve the true original by hand if the replication is genuine.",
    },
    "unresolved_self_links.csv": {
        "title": "Unresolved Self-Links",
        "why": "doi_o resolved to the replication paper itself. Replication titles often echo the "
               "original's, so title search can return the replication — these could not be "
               "disentangled automatically.",
        "action": "Identify the true original manually.",
    },
    "target_pending.csv": {
        "title": "Target Pending",
        "why": "The paper is a genuine replication but no candidate original was retrievable at "
               "extraction time — link_method = target_pending.",
        "action": "Retry once the reference data improves.",
    },
}

# Other CSVs that get a tab of the same shape but are NOT set-aside destinations —
# nothing in sanity_check writes them, so they are named here and nowhere else.
_OTHER_SETS: dict[str, dict] = {
    "cannot_be_determined": {
        "title": "Cannot Be Determined",
        "file": "cannot_be_determined.csv",
        "why": "The original was linked but the text did not support any outcome verdict — usually a "
               "missing abstract, a paywalled full text, or a genuinely ambiguous result statement. "
               "These rows STAY in extracted.csv; this file is a view of them, not a set-aside "
               "the pipeline files rows into.",
        "action": "Recover the full text, then re-run the work: extract.tier --redo.",
    },
    "pre_validation_audit": {
        "title": "Pre-Validation Audit",
        "file": "pre_validation_audit.csv",
        "why": "Per-row audit findings raised before rows are pushed to Supabase. One row per "
               "finding (check / severity / detail), so a record may appear several times.",
        "action": "Clear high-severity findings before the next push to validation.",
    },
    "doi_audit_report": {
        "title": "DOI Audit Report",
        "file": "doi_audit_report.csv",
        "why": "Output of extract.audit_dois — every doi_o whose registry metadata disagreed with "
               "the extracted original, with the proposed correction.",
        "action": "Apply with `python -m extract.audit_dois --apply`.",
    },
}


def _set_aside_tabs() -> dict[str, dict]:
    """One tab per set-aside destination, in the order shared/schema.py declares them."""
    tabs: dict[str, dict] = {}
    for fname in SET_ASIDE_DESTINATIONS.values():   # several buckets share a file
        key = fname[:-4] if fname.endswith(".csv") else fname
        if key in tabs:
            continue
        copy = _SET_ASIDE_COPY.get(fname, {
            "title": key.replace("_", " ").title(),
            "why": "A set-aside destination sanity_check writes; see extract/sanity_check.py "
                   "for the rule that files a row here.",
            "action": "Review by hand.",
        })
        tabs[key] = {"file": fname, **copy}
    return tabs


# Rows the pipeline deliberately kept OUT of extracted.csv, plus the other per-file
# views. The dashboard builds its tab, stats and detail table generically from this.
SET_FILES: dict[str, dict] = {**_set_aside_tabs(), **_OTHER_SETS}

_SET_PAGE_SIZE = 50

# The one column that characterises each set — rendered as the "what type are they"
# breakdown beside the row count. Everything else about a set is visible in its table.
_SET_PRIMARY_COL: dict[str, str] = {
    "pre_validation_audit": "severity",
    "doi_audit_report":     "status",
    "reproductions":        "outcome",
}
_SET_PRIMARY_DEFAULT = "type"


def _read_set(key: str) -> "pd.DataFrame | None":
    spec = SET_FILES.get(key)
    if spec is None:
        return None
    path = DATA_DIR / spec["file"]
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, encoding=spec.get("encoding", "utf-8-sig"),
                         dtype=str, on_bad_lines="skip").fillna("")
    except Exception:
        return None
    # Hand-maintained CSVs carry hundreds of trailing commas,
    # which pandas turns into empty "Unnamed: N" columns. Named columns are kept
    # even when empty — a blank field is information; a phantom column is not.
    keep = [c for c in df.columns
            if not str(c).startswith("Unnamed:") or df[c].astype(bool).any()]
    return df[keep]


@dashboard_bp.route("/api/dashboard/set-stats")
def api_set_stats():
    """Row count plus the one breakdown that characterises this set."""
    key = request.args.get("set", "")
    if key not in SET_FILES:
        return jsonify({"error": "unknown set"}), 400

    df = _read_set(key)
    if df is None:
        return jsonify({"total": None, "missing": True, "file": SET_FILES[key]["file"]})

    col = _SET_PRIMARY_COL.get(key, _SET_PRIMARY_DEFAULT)
    primary: dict[str, int] = {}
    if col in df.columns:
        primary = {str(k): int(v) for k, v in df[col].value_counts().items() if str(k).strip()}

    return jsonify({
        "total": len(df),
        "file": SET_FILES[key]["file"],
        "columns": list(df.columns),
        "primary_col": col if primary else None,
        "primary": primary,
    })


@dashboard_bp.route("/api/dashboard/set-rows")
def api_set_rows():
    """Paginated rows for one set CSV, with a free-text search across all columns."""
    key = request.args.get("set", "")
    if key not in SET_FILES:
        return jsonify({"error": "unknown set"}), 400

    page   = max(1, int(request.args.get("page", 1)))
    search = request.args.get("search", "").strip().lower()

    df = _read_set(key)
    if df is None:
        return jsonify({"rows": [], "total": 0, "pages": 1, "page": 1, "columns": []})

    if search:
        mask = df.apply(lambda col: col.astype(str).str.lower().str.contains(
            search, regex=False, na=False)).any(axis=1)
        df = df[mask]

    total = len(df)
    pages = max(1, (total + _SET_PAGE_SIZE - 1) // _SET_PAGE_SIZE)
    page  = min(page, pages)
    start = (page - 1) * _SET_PAGE_SIZE
    return jsonify({
        "rows": df.iloc[start:start + _SET_PAGE_SIZE].to_dict("records"),
        "columns": list(df.columns),
        "total": total, "pages": pages, "page": page,
    })


@dashboard_bp.route("/api/dashboard/set-download")
def api_set_download():
    """Stream a set CSV as a download attachment."""
    key = request.args.get("set", "")
    if key not in SET_FILES:
        return jsonify({"error": "unknown set"}), 400
    src = DATA_DIR / SET_FILES[key]["file"]
    if not src.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(src), as_attachment=True,
                     download_name=SET_FILES[key]["file"], mimetype="text/csv")


_STAGE_FILES = {
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}

# Stage 2 has no file of its own, so it is generated on request (below).
_CSV_STAGES = tuple(_STAGE_FILES)


def _generate_filtered_csv():
    """Write the newest release's screened rows to a temp file and send it.

    The same rows `python -m filter.engine export-csv` writes, through the same
    function — one definition of what a Stage 2 record contains. It streams the
    whole survivor pool, so this request takes minutes; that is the price of a
    figure nobody has to trust a stale file for.
    """
    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
    from filter.engine.export import ALIASES_FILENAME, SPEC_DIR
    from filter.engine.handoff import decisions, write_handoff
    from filter.engine.release import read_release
    from filter.engine.spec import load_specs
    from filter.engine.store import (DEFAULT_STORE_PATH, StoreUnavailable,
                                     open_store, resolve_release)
    from filter.engine.workids import load_aliases
    from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR

    store = DEFAULT_STORE_PATH
    try:
        con = open_store(store, read_only=True)
    except StoreUnavailable as exc:
        return jsonify({"error": str(exc)}), 404
    try:
        try:
            release_id = resolve_release(con, "latest", cache_dir=store.parent)
        except SystemExit as exc:
            return jsonify({"error": str(exc)}), 404
        try:
            record = read_release(release_id, cache_dir=store.parent)
        except (FileNotFoundError, OSError) as exc:
            return jsonify({"error": f"no release record for {release_id[:12]}: {exc}"}), 404
        try:
            drop, screen = decisions(ClaimsClient())
        except ClaimsNotConfigured as exc:
            return jsonify({"error": f"{exc} — without it no screen verdict can be "
                                     "read, so there is nothing to export."}), 503
        except Exception as exc:  # noqa: BLE001 — network boundary; a partial
            # verdict read would export a file short of rows the engine paid for.
            return jsonify({"error": f"could not read the screen verdicts: {exc}"}), 502

        download_dir = DATA_DIR / "dashboard" / "download"
        download_dir.mkdir(parents=True, exist_ok=True)
        filename = f"filtered_{release_id[:12]}_{datetime.date.today().isoformat()}.csv"
        out_path = download_dir / filename
        overlay_dir = OVERLAY_DIR if OVERLAY_DIR.is_dir() else None
        write_handoff(con, SNAPSHOT_POOL_DIR, out_path, release_id,
                      drop=drop, screen=screen, decided=set(screen),
                      specs=load_specs(SPEC_DIR), spec_dir=SPEC_DIR,
                      aliases=load_aliases(SPEC_DIR / ALIASES_FILENAME),
                      expect_bundle_hash=record.get("bundle_hash"),
                      expect_alias_release=record.get("alias_release"),
                      overlay_dir=overlay_dir,
                      expect_overlay_hash=record.get("overlay_hash"))
    finally:
        con.close()
    return send_file(str(out_path), as_attachment=True,
                     download_name=filename, mimetype="text/csv")


@dashboard_bp.route("/api/dashboard/download")
def api_dashboard_download():
    """Stream a pipeline CSV as a download attachment.

    Query params:
      stage — filtered | extracted | extracted-test

    `filtered` is generated from the routing store on request; the other two are
    files Stage 3 wrote.
    """
    stage = request.args.get("stage", "extracted").strip()
    if stage == "filtered":
        return _generate_filtered_csv()
    if stage not in _STAGE_FILES:
        return jsonify({"error": "invalid stage"}), 400

    src = _STAGE_FILES[stage]
    if not src.exists():
        return jsonify({"error": f"{stage} CSV not found"}), 404

    download_dir = DATA_DIR / "dashboard" / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    date_str  = datetime.date.today().isoformat()
    filename  = f"{stage}_{date_str}.csv"
    dest_path = download_dir / filename

    shutil.copy2(src, dest_path)
    return send_file(str(dest_path), as_attachment=True,
                     download_name=filename, mimetype="text/csv")


# ── Supabase endpoints ────────────────────────────────────────────────────────
