"""Count what a stricter `clean_doi()` would change, before changing it (handover step 4).

The candidate change: collapse a doubled slash after the prefix (`10.1037//x` →
`10.1037/x`) and URL-decode (`%3c` → `<`). `clean_doi()` is the identity function for
every DOI comparison in the repo, so this counts, per artifact, the values that would
change, the pair_ids that would move (pair_id = md5 of the stored doi_r|doi_o), and the
skip-list matches that would be GAINED (a row that today misses FLoRA / the validated
list only because of the spelling).

    .venv/bin/python -m analysis.pick_pilot.clean_doi_impact
"""
from __future__ import annotations

import csv
import glob
import hashlib
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import shared.config  # noqa: F401  (loads .env before the Supabase client reads it)
from shared.utils import clean_doi

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
_DOUBLE = re.compile(r"^(10\.\d+)/{2,}")


def new_clean(doi: str) -> str:
    d = clean_doi(doi)
    d = _DOUBLE.sub(r"\1/", d)
    return unquote(d).lower() if "%" in d else d


def kinds(doi: str) -> set[str]:
    d = clean_doi(doi)
    out = set()
    if _DOUBLE.match(d):
        out.add("double_slash")
    if re.search(r"%[0-9a-f]{2}", d):
        out.add("url_encoded")
    return out


def rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def column_report(name: str, data: list[dict], cols: list[str]) -> list[str]:
    lines = []
    for col in cols:
        vals = [r.get(col) or "" for r in data]
        cells = [c for v in vals for c in re.split(r"[;|,]\s*(?=10\.|https?://)", v) if clean_doi(c).startswith("10.")]
        changed = [c for c in cells if clean_doi(c) and new_clean(c) != clean_doi(c)]
        k = Counter(x for c in changed for x in kinds(c))
        lines.append(f"| {name} | `{col}` | {len(cells):,} | {len(changed):,} | "
                     f"{k.get('double_slash', 0)} | {k.get('url_encoded', 0)} |")
    return lines


def pair_moves(data: list[dict]) -> tuple[int, int]:
    """(pair_ids equal to md5(doi_r|doi_o) today, of those how many would move)."""
    same = moved = 0
    for r in data:
        dr, do = r.get("doi_r") or "", r.get("doi_o") or ""
        if not (dr and do):
            continue
        if hashlib.md5(f"{dr}|{do}".encode()).hexdigest() != r.get("pair_id"):
            continue
        same += 1
        if new_clean(dr) != dr or new_clean(do) != do:
            moved += 1
    return same, moved


def main() -> None:
    from shared.flora_skip import default_flora_skip_dois, load_validated_skip

    out = ["| Artifact | Column | DOI cells | would change | `//` | `%xx` |",
           "| --- | --- | ---: | ---: | ---: | ---: |"]
    extracted = rows(DATA / "extracted.csv")
    out += column_report("extracted.csv", extracted, ["doi_r", "doi_o"])
    set_asides = [Path(p) for p in sorted(glob.glob(str(DATA / "*.csv")))
                  if Path(p).name in {"api_error.csv", "keyed_link_disputed.csv",
                                      "no_evidence.csv", "no_original_found.csv",
                                      "not_a_replication.csv", "prospective_registration.csv",
                                      "search_link_unconfirmed.csv", "target_pending.csv",
                                      "unidentified_original.csv"}]
    aside_rows = [r for p in set_asides for r in rows(p)]
    out += column_report("set-aside CSVs (9)", aside_rows, ["doi_r", "doi_o"])
    out += column_report("flora.csv", rows(DATA / "flora.csv"),
                         ["doi_r", "doi_o", "alt_identifier_r"])
    out += column_report("FLoRA entry sheet", rows(DATA / "FLoRA entry sheet - replication list.csv"),
                         ["doi_r", "doi_o", "alt_identifier_r"])
    out += column_report("validated_skip.csv", rows(DATA / "validated_skip.csv"), ["doi"])

    sb_lines = []
    try:
        from shared.supabase_client import _get
        for table in ("unvalidated", "validated"):
            data = _get(table, {"select": "record_id,doi_r,doi_o,pair_id"}) \
                if table == "unvalidated" else _get(table, {"select": "*"})
            out += column_report(f"Supabase `{table}`", data, ["doi_r", "doi_o"])
            s, m = pair_moves(data)
            sb_lines.append(f"- Supabase `{table}`: {len(data):,} rows; pair_id = "
                            f"md5(doi_r|doi_o) on {s:,}; would move on re-export: {m}")
    except Exception as exc:  # read-only probe; report rather than crash
        sb_lines.append(f"- Supabase read failed: {exc!r}")

    s, m = pair_moves(extracted)
    s2, m2 = pair_moves(aside_rows)

    flora_now = default_flora_skip_dois()
    flora_new = {new_clean(d) for d in flora_now}
    _, val_now = load_validated_skip()
    val_new = {new_clean(d) for d in val_now}
    gained = Counter()
    for r in extracted + aside_rows:
        d = clean_doi(r.get("doi_r") or "")
        if not d:
            continue
        if d not in flora_now and new_clean(d) in flora_new:
            gained["flora"] += 1
        if d not in val_now and new_clean(d) in val_new:
            gained["validated"] += 1
    collapse = len(flora_now) - len(flora_new)

    report = "\n".join(out) + "\n\n" + "\n".join([
        f"- extracted.csv pair_ids that are md5(doi_r|doi_o) today: {s:,}; would move: {m}",
        f"- set-aside pair_ids likewise: {s2:,}; would move: {m2}",
        *sb_lines,
        f"- FLoRA skip set: {len(flora_now):,} DOIs → {len(flora_new):,} after "
        f"normalising ({collapse} collapse into an existing spelling)",
        f"- extracted + set-aside rows that would NEWLY match the FLoRA skip list: "
        f"{gained['flora']}; the validated skip list: {gained['validated']}",
    ])
    print(report)
    (Path(__file__).parent / "clean_doi_impact_counts.md").write_text(report + "\n")


if __name__ == "__main__":
    main()
