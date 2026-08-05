"""build_casesets.py — rebuild the four pre-screen eval buckets of issue #130.

Reads the gitignored data/ CSVs in the MAIN checkout, makes ONE streaming pass over
data/filtered.csv (4.9 GB) to attach title + abstract and to confirm each gold DOI is in
the Stage 2 pass-through population, and writes four JSON case files here.

Run from the main checkout:  python3 analysis/prescreen_eval/build_casesets.py
(paths are absolute, so it can be run from anywhere).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path("/Users/lukaswallrich/Documents/Coding/flora-extractor")
DATA = REPO / "data"
OUT = REPO / ".claude/worktrees/issue-130-prescreen/analysis/prescreen_eval"
sys.path.insert(0, str(REPO))

from shared.config import ALL_REPLICATIONS_PATH  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

csv.field_size_limit(10_000_000)

SPECIAL = {
    "grimm": ["herpesvirus", "nuclear envelope"],
    "gur": ["parieto-frontal", "p-fit", "pfit"],
    "zelenski": ["counterdispositional", "counter-dispositional"],
    "suiter": ["yale swallow"],
}

# The four papers issue #130 names as the old pre-screen's wrong discards. Pinned so the
# cap can never drop them from the gold-positive set.
PINNED = {
    "10.1128/jvi.00068-12",      # Grimm et al. — herpesvirus nuclear envelope breakdown
    "10.1093/cercor/bhaa282",    # Gur et al. — PFIT neuroimaging
    "10.1037/a0025169",          # Zelenski et al. — counterdispositional behaviour
    "10.1007/s00455-013-9488-3", # Suiter et al. — Yale Swallow Protocol
}


def read_csv(path: Path, encoding: str = "utf-8-sig") -> list[dict]:
    with open(path, newline="", encoding=encoding, errors="replace") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    allrep = read_csv(ALL_REPLICATIONS_PATH)
    # Live file plus the pre-engine archive: the papers discarded before the #146
    # handoff are gone from the input, but their screen verdicts are still the
    # negatives these case sets are built from — see negative_rows() in arm_evidence.
    nar = (read_csv(DATA / "not_a_replication.csv")
           + read_csv(DATA / "legacy_pre_engine" / "not_a_replication.csv"))

    def doi(row: dict, col: str = "doi_r") -> str:
        v = (row.get(col) or "").strip()
        return "" if v.upper() in {"", "NA", "NAN"} else clean_doi(v)

    # ── bucket sources ────────────────────────────────────────────────────────
    pos_flora: dict[str, dict] = {}
    for r in allrep:
        if r.get("validation_status") == "already_in_flora":
            d = doi(r)
            if d:
                pos_flora.setdefault(d, r)

    pos_repro: dict[str, dict] = {}
    for r in allrep:
        if r.get("type") == "reproduction" and r.get("pathway_source") != "openalex":
            d = doi(r)
            if d:
                pos_repro.setdefault(d, r)
    for extra, enc in ((DATA / "reproductions.csv", "cp1252"), (DATA / "flora.csv", "utf-8-sig")):
        for r in read_csv(extra, enc):
            if extra.name == "flora.csv" and r.get("type") != "reproduction":
                continue
            d = doi(r)
            if d:
                pos_repro.setdefault(d, r)
    # a paper can only be one thing: flora positives win
    for d in list(pos_repro):
        if d in pos_flora:
            del pos_repro[d]

    neg_curated: dict[str, dict] = {}
    for r in allrep:
        if r.get("validation_status") == "false_positive":
            d = doi(r)
            if d:
                neg_curated.setdefault(d, r)

    neg_screen_rows = [r for r in nar if r.get("link_method") == "not_a_replication"]

    # keep the buckets disjoint
    screen_dois = {doi(r) for r in neg_screen_rows} - {""}
    for d in list(neg_curated):
        if d in screen_dois or d in pos_flora or d in pos_repro:
            del neg_curated[d]

    # screen-negative DOIs join the pass only to recover a missing title_r
    wanted = set(pos_flora) | set(pos_repro) | set(neg_curated) | screen_dois
    print(f"wanted DOIs: flora={len(pos_flora)} repro={len(pos_repro)} "
          f"curated_neg={len(neg_curated)} total={len(wanted)}", flush=True)

    # ── one streaming pass over filtered.csv ──────────────────────────────────
    found: dict[str, dict] = {}
    specials: dict[str, list[dict]] = {k: [] for k in SPECIAL}
    n = 0
    with open(DATA / "filtered.csv", newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            n += 1
            if n % 500_000 == 0:
                print(f"  … {n:,} rows, {len(found):,} hits", flush=True)
            d = doi(row)
            if d and d in wanted and d not in found:
                found[d] = {
                    "title": (row.get("title_r") or "").strip(),
                    "abstract": (row.get("abstract_r") or "").strip(),
                    "filter_status": row.get("filter_status") or "",
                }
            title = (row.get("title_r") or "").lower()
            if title:
                for name, keys in SPECIAL.items():
                    if any(k in title for k in keys):
                        specials[name].append({"doi": d, "title": row.get("title_r"),
                                               "abstract": (row.get("abstract_r") or "")[:300]})
    print(f"filtered.csv rows scanned: {n:,}; gold DOIs found: {len(found):,}", flush=True)

    (OUT / "specials_raw.json").write_text(json.dumps(specials, indent=1))

    # ── assemble ──────────────────────────────────────────────────────────────
    stats: dict[str, dict] = {}

    def text(d: str, src_row: dict) -> tuple[str, str, str]:
        """title, abstract, provenance — filtered.csv first, curated CSV as fallback."""
        title = found[d]["title"]
        abstract = found[d]["abstract"]
        prov = "filtered"
        if not abstract:
            fb = (src_row.get("abstract_r") or "").strip()
            if fb and fb.upper() != "NA":
                abstract, prov = fb, "filtered_title+curated_abstract"
        return title, abstract, prov

    def build(name: str, prefix: str, src: dict[str, dict], cap: int,
              note_fn=None) -> list[dict]:
        in_filtered = [d for d in src if d in found]
        usable = {}
        for d in in_filtered:
            t, a, prov = text(d, src[d])
            if t and a:
                usable[d] = (t, a, prov)
        order = sorted(usable)
        pinned = [d for d in order if d in PINNED]
        kept = pinned + [d for d in order if d not in PINNED]
        kept = kept[:cap]
        kept.sort()
        stats[name] = {
            "source_dois": len(src),
            "in_filtered": len(in_filtered),
            "with_title_and_abstract": len(usable),
            "dropped_not_in_filtered": len(src) - len(in_filtered),
            "dropped_no_abstract_or_title": len(in_filtered) - len(usable),
            "abstract_from_curated_fallback": sum(1 for d in kept if usable[d][2] != "filtered"),
            "kept": len(kept),
            "cap": cap,
        }
        cases = []
        for i, d in enumerate(kept, 1):
            t, a, prov = usable[d]
            case = {
                "id": f"{prefix}{i:03d}",
                "doi": d,
                "title": t,
                "abstract": a,
                "bucket": name,
                "text_source": prov,
            }
            if note_fn:
                note = note_fn(src[d])
                if note:
                    case["note"] = note
            cases.append(case)
        return cases

    def curated_note(r: dict) -> str:
        note = (r.get("prep_notes") or "").strip()
        return f"old-pipeline false_positive; {note}"[:800] if note else "old-pipeline false_positive"

    files = {
        "cases_goldpos_flora.json": build("goldpos_flora", "GP", pos_flora, 833),
        "cases_goldpos_repro.json": build("goldpos_repro", "GR", pos_repro, 76),
        "cases_goldneg_curated.json": build("goldneg_curated", "NC", neg_curated, 400,
                                            curated_note),
    }

    # screen-confirmed negatives come straight from not_a_replication.csv
    seen: set[str] = set()
    rows = []
    recovered = 0
    for r in neg_screen_rows:
        d = doi(r)
        title = (r.get("title_r") or "").strip()
        abstract = (r.get("abstract_r") or "").strip()
        if not title and d in found and found[d]["title"]:
            title = found[d]["title"]
            recovered += 1
        key = d or f"title:{title.lower()}"
        if not title or not abstract or key in seen:
            continue
        seen.add(key)
        rows.append((key, d, title, abstract, (r.get("link_evidence") or "").strip(),
                     (r.get("filter_evidence") or "").strip()))
    rows.sort(key=lambda t: t[0])
    ns_cases = []
    for i, (_key, d, title, abstract, ev, fev) in enumerate(rows[:300], 1):
        note = "screen-confirmed not_a_replication"
        if ev:
            note += f"; link_evidence: {ev}"
        elif fev:
            note += f"; filter_evidence: {fev}"
        ns_cases.append({"id": f"NS{i:03d}", "doi": d, "title": title,
                         "abstract": abstract, "bucket": "goldneg_screen",
                         "note": note[:800]})
    stats["goldneg_screen"] = {
        "source_rows": len(neg_screen_rows),
        "with_title_and_abstract_dedup": len(rows),
        "titles_recovered_from_filtered": recovered,
        "dropped": len(neg_screen_rows) - len(rows),
        "kept": len(ns_cases),
        "cap": 300,
    }
    files["cases_goldneg_screen.json"] = ns_cases

    for fname, cases in files.items():
        (OUT / fname).write_text(json.dumps(cases, indent=1, ensure_ascii=False))
        print(f"{fname}: {len(cases)}")

    (OUT / "build_stats.json").write_text(json.dumps(stats, indent=1))
    print(json.dumps(stats, indent=1))

    # where did the special cases land?
    for name, keys in SPECIAL.items():
        hits = []
        for fname, cases in files.items():
            for c in cases:
                t = c["title"].lower()
                if any(k in t for k in keys):
                    hits.append((c["id"], fname, c["title"][:90]))
        print(name, hits)


if __name__ == "__main__":
    main()
