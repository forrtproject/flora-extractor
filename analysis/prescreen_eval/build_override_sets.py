"""build_override_sets.py — corpora for testing hard_signal(), the pre-screen override.

The override in `shared/prescreen.py` was hand-written after looking at the four misses
it had to catch, so its measured perfection on those 567 rows is partly circular. These
two case sets are much larger and independent of that derivation:

  override_positives.json — every FLoRA replication/reproduction paper with a title and
      an abstract, from all_replications.csv, flora.csv, flora_entry_sheet.csv and
      reproductions.csv (cp1252). NOT restricted to the Stage 3 population: the question
      is FLoRA-wide recall of the override.
  override_negatives.json — non-replications that genuinely reach Stage 3: the current
      screen's own discards, plus old-pipeline false positives that Stage 2 passes
      through (filter_status != false_positive in filtered.csv).

One streaming pass over data/filtered.csv (4.9 GB, ~6 min) does both jobs: it backfills
missing titles/abstracts for the positives and collects the live negatives.

Rows whose abstract is under PRESCREEN_MIN_ABSTRACT_CHARS are dropped from both sets:
production bypasses the pre-screen on those (`short_text`), so the override never gets
to matter for them and counting them would flatter or deflate its recall for free.

Run:  .venv/bin/python analysis/prescreen_eval/build_override_sets.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]                                     # the worktree (code)
DATA = Path("/Users/lukaswallrich/Documents/Coding/flora-extractor/data")  # main checkout
sys.path.insert(0, str(REPO))

from shared.config import PRESCREEN_MIN_ABSTRACT_CHARS  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

csv.field_size_limit(sys.maxsize)

NEG_CAP = 5_000

# Positive sources, most trusted first — a DOI keeps the bucket of the first source that
# claims it. flora.csv and the entry sheet are the human-curated FLoRA database itself;
# `already_in_flora` rows of all_replications.csv were matched to it; `llm_confirmed`
# rows are the old pipeline's own LLM verdicts and are kept separate for that reason.
POS_BUCKETS = ("flora_db", "entry_sheet", "reproductions", "allrep_in_flora", "allrep_llm")

_TITLE_FROM_REF = re.compile(r"\(\d{4}[a-z]?(?:, [^)]+)?\)\.\s*(.+?)(?:\.\s|\.$)")


def read_csv(path: Path, encoding: str = "utf-8-sig") -> list[dict]:
    with open(path, newline="", encoding=encoding, errors="replace") as fh:
        return list(csv.DictReader(fh))


def doi_of(row: dict, col: str = "doi_r") -> str:
    v = (row.get(col) or "").strip()
    return "" if v.upper() in {"", "NA", "NAN"} else clean_doi(v)


def title_from_ref(ref: str) -> str:
    """Best-effort title out of an APA reference string — many sources have no title."""
    m = _TITLE_FROM_REF.search(ref or "")
    return m.group(1).strip() if m else ""


def collect_positives() -> dict[str, dict]:
    """doi -> {bucket, title, abstract} from the four curated files, first source wins."""
    pos: dict[str, dict] = {}

    def add(d: str, bucket: str, title: str, abstract: str) -> None:
        if d and d not in pos:
            pos[d] = {"doi": d, "bucket": bucket,
                      "title": (title or "").strip(), "abstract": (abstract or "").strip()}

    for r in read_csv(DATA / "flora.csv"):
        add(doi_of(r), "flora_db", r.get("title_r") or "", "")
    for r in read_csv(DATA / "flora_entry_sheet.csv"):
        if (r.get("validation_status") or "").strip() == "validated - discarded":
            continue
        add(doi_of(r), "entry_sheet", title_from_ref(r.get("ref_r") or ""), r.get("abstract_r") or "")
    for r in read_csv(DATA / "reproductions.csv", "cp1252"):
        add(doi_of(r), "reproductions", title_from_ref(r.get("ref_r") or ""), r.get("abstract_r") or "")
    for r in read_csv(DATA / "all_replications.csv"):
        status, kind = r.get("validation_status") or "", r.get("type") or ""
        if kind not in {"replication", "reproduction"}:
            continue
        if status == "already_in_flora" or status == "validated":
            add(doi_of(r), "allrep_in_flora", title_from_ref(r.get("ref_r") or ""), r.get("abstract_r") or "")
        elif status == "llm_confirmed":
            add(doi_of(r), "allrep_llm", title_from_ref(r.get("ref_r") or ""), r.get("abstract_r") or "")
    return pos


def collect_negatives() -> tuple[dict[str, dict], dict[str, dict]]:
    """(screen discards, old-pipeline false positives) — the second needs the live check."""
    screen: dict[str, dict] = {}
    for r in read_csv(DATA / "not_a_replication.csv"):
        if (r.get("link_method") or "").strip() != "not_a_replication":
            continue
        d = doi_of(r)
        if d and d not in screen:
            screen[d] = {"doi": d, "bucket": "screen_discard",
                         "title": (r.get("title_r") or "").strip(),
                         "abstract": (r.get("abstract_r") or "").strip(),
                         "source": (r.get("source") or "").strip(),
                         "note": ((r.get("link_evidence") or r.get("filter_evidence") or "")
                                  .strip()[:300])}
    curated: dict[str, dict] = {}
    for r in read_csv(DATA / "all_replications.csv"):
        if (r.get("validation_status") or "") != "false_positive":
            continue
        d = doi_of(r)
        if d and d not in curated and d not in screen:
            curated[d] = {"doi": d, "bucket": "curated_false_positive",
                          "title": "", "abstract": (r.get("abstract_r") or "").strip(),
                          "note": "old-pipeline false_positive; "
                                  + (r.get("prep_notes") or "").strip()[:300]}
    return screen, curated


def main() -> None:
    pos = collect_positives()
    screen_neg, curated_neg = collect_negatives()
    for d in list(curated_neg):          # a paper cannot be both
        if d in pos:
            del curated_neg[d]
    for d in list(screen_neg):
        if d in pos:
            del screen_neg[d]
    print(f"positives from curated files: {len(pos):,} DOIs", flush=True)
    print(f"negatives: screen_discard={len(screen_neg):,} "
          f"curated_false_positive={len(curated_neg):,}", flush=True)

    # ── one streaming pass over filtered.csv ──────────────────────────────────
    wanted = set(pos) | set(screen_neg) | set(curated_neg)
    found: dict[str, dict] = {}
    n = 0
    with open(DATA / "filtered.csv", newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            n += 1
            if n % 500_000 == 0:
                print(f"  … {n:,} rows, {len(found):,} hits", flush=True)
            d = doi_of(row)
            if not d or d not in wanted or d in found:
                continue
            found[d] = {"title": (row.get("title_r") or "").strip(),
                        "abstract": (row.get("abstract_r") or "").strip(),
                        "filter_status": (row.get("filter_status") or "").strip(),
                        "source": (row.get("source") or "").strip()}
    print(f"scanned {n:,} rows; {len(found):,} of {len(wanted):,} wanted DOIs present")

    stats = {"filtered_rows": n, "wanted_dois": len(wanted), "found_in_filtered": len(found)}

    # ── positives ─────────────────────────────────────────────────────────────
    cases, drop_no_title, drop_no_abstract, drop_short = [], 0, 0, 0
    for d in sorted(pos):
        c, f = pos[d], found.get(d, {})
        title = c["title"] or f.get("title", "")
        abstract = c["abstract"] or f.get("abstract", "")
        if not title:
            drop_no_title += 1
            continue
        if not abstract:
            drop_no_abstract += 1
            continue
        if len(abstract) < PRESCREEN_MIN_ABSTRACT_CHARS:
            drop_short += 1
            continue
        cases.append({"id": "", "doi": d, "title": title, "abstract": abstract,
                      "bucket": c["bucket"],
                      "text_source": ("curated" if c["title"] and c["abstract"] else
                                      "filtered" if not (c["title"] or c["abstract"]) else "mixed"),
                      "in_filtered": d in found,
                      "filter_status": f.get("filter_status", ""),
                      "source": f.get("source", "")})
    for i, c in enumerate(cases, 1):
        c["id"] = f"OP{i:05d}"
    (HERE / "override_positives.json").write_text(json.dumps(cases, indent=1, ensure_ascii=False))
    stats["positives"] = {"kept": len(cases), "dropped_no_title": drop_no_title,
                          "dropped_no_abstract": drop_no_abstract,
                          "dropped_abstract_under_min": drop_short,
                          "min_abstract_chars": PRESCREEN_MIN_ABSTRACT_CHARS}
    print(f"positives: {len(cases):,} kept "
          f"(dropped {drop_no_title:,} no title, {drop_no_abstract:,} no abstract, "
          f"{drop_short:,} abstract < {PRESCREEN_MIN_ABSTRACT_CHARS} chars)")

    # ── negatives: screen discards first, then live curated false positives ───
    negs, skipped_dead, neg_short = [], 0, 0
    for d in sorted(screen_neg):
        c, f = screen_neg[d], found.get(d, {})
        title, abstract = c["title"] or f.get("title", ""), c["abstract"] or f.get("abstract", "")
        if not title or not abstract:
            continue
        if len(abstract) < PRESCREEN_MIN_ABSTRACT_CHARS:
            neg_short += 1
            continue
        negs.append(dict(c, title=title, abstract=abstract,
                         filter_status=f.get("filter_status", ""),
                         source=c.get("source") or f.get("source", "")))
    for d in sorted(curated_neg):
        if len(negs) >= NEG_CAP:
            break
        c, f = curated_neg[d], found.get(d)
        if not f or f.get("filter_status") == "false_positive":
            skipped_dead += 1          # Stage 3 never sees these rows
            continue
        title, abstract = f.get("title", ""), c["abstract"] or f.get("abstract", "")
        if not title or not abstract:
            continue
        if len(abstract) < PRESCREEN_MIN_ABSTRACT_CHARS:
            neg_short += 1
            continue
        negs.append(dict(c, title=title, abstract=abstract,
                         filter_status=f.get("filter_status", ""), source=f.get("source", "")))
    for i, c in enumerate(negs, 1):
        c["id"] = f"ON{i:05d}"
    (HERE / "override_negatives.json").write_text(json.dumps(negs, indent=1, ensure_ascii=False))
    stats["negatives"] = {"kept": len(negs), "cap": NEG_CAP,
                          "skipped_not_reaching_stage3": skipped_dead,
                          "dropped_abstract_under_min": neg_short}
    print(f"negatives: {len(negs):,} kept ({skipped_dead:,} curated skipped as "
          f"Stage-2 false_positive, {neg_short:,} short abstracts)")

    (HERE / "override_build_stats.json").write_text(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
