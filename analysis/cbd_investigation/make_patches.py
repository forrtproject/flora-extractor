"""Write fix_prompts.patch and fix_link_original.patch against HEAD (not applied).

prompts: the V1b rule edits (variants.EDITS_B), written against the «slot» source.
link_original: (1) every document reaches the full-text call whole (full_body), and
(2) a settled full-text reading of the carried original replaces the carried cbd.
"""
import difflib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent.parent))
import variants as V  # noqa: E402

def src(text: str) -> str:
    for label, slot in (("cannot_be_determined", "«cannot_be_determined»"),
                        ("uninformative", "«uninformative»")):
        text = text.replace(f'"{label}"', f'"{slot}"')
    return text

def head(path: str) -> str:
    return subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True,
                          check=True, cwd=HERE.parent.parent).stdout

def patch(path: str, edits, out: str) -> None:
    old = head(path)
    new = old
    for a, b in edits:
        assert new.count(a) == 1, (path, a[:70])
        new = new.replace(a, b)
    diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                f"a/{path}", f"b/{path}", n=3)
    (HERE / out).write_text("".join(diff))
    print("wrote", out)

patch("shared/prompts.py", [(src(a), src(b)) for a, b in V.EDITS_B], "fix_prompts.patch")

FULL_OLD = """    full_body = (str(best.get("raw_text") or "").strip() if seen_certain >= 2 else "")"""
FULL_NEW = """    # Every document is sent whole since 2026-09-23 (analysis/cbd_investigation): the
    # intro/closing slices dropped the RESULTS, where 6 of the 10 adjudicated
    # cannot_be_determined rows with a document stated the outcome, and 2,820 of 5,215
    # cached full-text prompts had no discussion heading at all ("tail"). Replayed on
    # gpt-6-luna it took sampled cbd rows with a document from 14/23 to 6/23 and moved
    # 3 of 30 settled controls — as many as a plain re-run of the old prompt moved.
    full_body = str(best.get("raw_text") or "").strip()"""

CARRY_OLD = """    if carried:
        # Nothing below the carried resolution accepted a link of its own — the call
        # failed, or it read the closing sections and named no target it was sure of.
        # Neither contradicts the accepted link, so it stands, with the outcome it has.
        log.info("[%s] descent: the full-text call accepted no link — keeping the "
                 "carried %s resolution", doi_r, carried.get("resolution_method"))
        return _exit_resolved({**carried,"""
CARRY_NEW = """    if carried:
        # Nothing below the carried resolution accepted a link of its own — the call
        # failed, or it read the closing sections and named no target it was sure of.
        # Neither contradicts the accepted link, so it stands. But the descent was made
        # to settle its OUTCOME, and the call usually coded one for the same original
        # while failing to match it to a record (the PDF's reference list is not the
        # list the carried rung read). Dropping that reading shipped cbd over a settled
        # full-text verdict on 22 llm_references rows of extracted.csv, 41 in all
        # (analysis/cbd_investigation).
        same = _same_original_as_carried(llm.get("targets") or [], carried)
        if (same and not _settled(carried)
                and outcome_is_settled(same.get("outcome_block") or {}, record_type)):
            log.info("[%s] descent: the full-text call coded the carried original — "
                     "taking its outcome", doi_r)
            carried = {**carried, "outcome_block": same["outcome_block"]}
        log.info("[%s] descent: the full-text call accepted no link — keeping the "
                 "carried %s resolution", doi_r, carried.get("resolution_method"))
        return _exit_resolved({**carried,"""

HELPER_ANCHOR = """def _as_target(resolution: dict) -> dict:"""
HELPER_NEW = '''def _same_original_as_carried(targets: list[dict], carried: dict) -> "dict | None":
    """The one target of a later answer that names the carried resolution's original.

    By the mapped record's DOI when it has one; otherwise — the usual case, because the
    later call's key did not match a record — by the carried original's first-author
    surname AND year both appearing in how the paper named the target. None when no
    target, or more than one, fits: a wrong transfer is worse than a cbd.
    """
    doi = clean_doi(str(carried.get("resolved_doi_o") or ""))
    surname = _norm(carried.get("resolved_author_o")).split(" ")[-1]
    year = str(carried.get("resolved_year_o") or "")[:4]
    fits = []
    for t in targets:
        t_doi = clean_doi(str((t.get("record") or {}).get("doi") or ""))
        if t_doi:
            if doi and t_doi == doi:
                fits.append(t)
            continue
        named = _norm(t.get("target_as_named"))
        if surname and year and surname in named and year in named:
            fits.append(t)
    return fits[0] if len(fits) == 1 else None


''' + HELPER_ANCHOR

patch("extract/link_original.py",
      [(FULL_OLD, FULL_NEW), (CARRY_OLD, CARRY_NEW), (HELPER_ANCHOR, HELPER_NEW)],
      "fix_link_original.patch")
