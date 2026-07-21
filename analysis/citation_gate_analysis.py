"""
citation_gate_analysis.py — measure the impact of hardening the Stage-2
author-year citation gate (shared.openalex_client.extract_author_year_patterns).

Context
-------
In Stage 2 the rule filter accepts a paper as replication/reproduction with
*high* confidence (bypassing the LLM review that ``needs_review`` rows get) as
soon as ONE author-year citation is detected in title+abstract.  The weakest
pattern, ``single_bare`` = ``({NAME}),?\\s+({YEAR})``, matches any capitalised
>=3-letter token followed by a 1900-2099 year with only an *optional* comma, so
non-citations like "January 2020", "Study 2019", "Table 2020", "COVID 2019"
fire the gate and skip the LLM.

This script quantifies, over the production data/filtered.csv, how many rows'
citation decision would CHANGE (accept -> needs_review) under each hardening
option, and dumps the matches each option removes so their FP/FN rate can be
eyeballed.  It writes to stdout a summary table plus per-option example files.

It reads filtered.csv by streaming ``unzip -p filtered.zip`` (the production
file ships zipped with a compression method Python's stdlib can't open) or a
plain filtered.csv if present.  READ-ONLY on production data.

Usage:
    python -m analysis.citation_gate_analysis [--max-rows N] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import calendar
import random
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# Reuse the exact regex primitives the production extractor is built from so the
# analysis measures the real patterns, not an approximation.
from shared.openalex_client import _NAME, _YEAR, _PATTERNS  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1].parent
# analysis/ lives in the worktree; production data is in the MAIN checkout.
# Resolve the main checkout's data dir robustly.
_MAIN = Path("/Users/lukaswallrich/Documents/Coding/flora-extractor")
FILTERED_ZIP = _MAIN / "data" / "filtered.zip"
FILTERED_CSV = _MAIN / "data" / "filtered.csv"

# --------------------------------------------------------------------------- #
# Hardening building blocks
# --------------------------------------------------------------------------- #

# Option (a): tokens that, as the leading "name", almost never start a real
# author-year citation.  Months, structural document words, disease acronyms,
# and capitalised function words that commonly begin a sentence before a year.
_MONTHS = {m for m in calendar.month_name if m} | {m for m in calendar.month_abbr if m}
_SEASONS = {"Winter", "Spring", "Summer", "Fall", "Autumn"}
_DOC_WORDS = {
    "Study", "Studies", "Table", "Figure", "Fig", "Experiment", "Experiments",
    "Session", "Sessions", "Wave", "Waves", "Sample", "Samples", "Model",
    "Models", "Appendix", "Chapter", "Section", "Panel", "Phase", "Trial",
    "Trials", "Cohort", "Group", "Groups", "Item", "Items", "Question",
    "Version", "Round", "Block", "Condition", "Column", "Row", "Note",
    "Equation", "Hypothesis", "Day", "Week", "Month", "Year", "Time", "Age",
    "Quarter", "Volume", "Vol", "Issue", "Number", "No", "Page", "Part",
    "Level", "Step", "Set", "Series", "Line", "Site", "Class", "Type", "Grade",
}
_ACRONYMS = {"COVID", "SARS", "MERS", "HIV", "AIDS", "EU", "US", "USA", "UK",
             "UN", "WHO", "GDP", "AI", "ML", "PCR", "DNA", "RNA"}
_FUNCTION_WORDS = {
    "Since", "During", "Between", "From", "Until", "After", "Before", "In",
    "On", "By", "At", "For", "With", "Within", "Over", "Through", "Under",
    "Around", "About", "Across", "Throughout", "Post", "Pre", "Early", "Late",
    "The", "This", "That", "These", "Those", "Their", "Our", "Its", "And",
    "But", "However", "Thus", "Here", "There", "When", "While", "Copyright",
    "Circa", "Ca", "Fiscal", "Academic", "Autumn", "Christmas", "Easter",
}
BLACKLIST = {w.lower() for w in (_MONTHS | _SEASONS | _DOC_WORDS | _ACRONYMS | _FUNCTION_WORDS)}


def _leading_token(raw: str) -> str:
    """First whitespace-delimited token of a matched citation string."""
    raw = raw.strip()
    return raw.split()[0] if raw else ""


def _preceding_word_lowercase(text: str, start: int) -> bool:
    """True if the word immediately preceding position *start* ends in a
    lowercase letter (heuristic that the name is a mid-sentence author token,
    not a sentence-initial common word or a structural label)."""
    before = text[:start]
    m = re.search(r"(\S+)\s*$", before)
    if not m:
        return False  # nothing before -> treat as sentence-initial (reject)
    word = m.group(1)
    # strip trailing punctuation to look at the last letter
    stripped = word.rstrip(".,;:()[]{}\"'")
    if not stripped:
        return False
    return stripped[-1].islower()


# --------------------------------------------------------------------------- #
# Parametrised extractor (mirrors extract_author_year_patterns dedup logic)
# --------------------------------------------------------------------------- #

_COMPILED = [(name, re.compile(pat)) for name, pat in _PATTERNS]

# single_bare with the comma made mandatory (option b): the year must be
# comma-preceded when bare; parenthesised years are handled by single_paren.
_SINGLE_BARE_COMMA = re.compile(rf"({_NAME}),\s+({_YEAR})(?!\d)")


def extract(text: str, max_year=None, *, option: str = "current") -> list[dict]:
    """Run the full pattern cascade with an optional hardening applied to the
    single_bare pattern only.  option in:
        current  - production behaviour (baseline)
        a        - blacklist leading token
        b        - require comma before the bare year
        c        - require preceding word lowercase
        ab, ac, abc - combinations
    Returns the surviving matches (same shape as the production function)."""
    if not text:
        return []
    results: list[dict] = []
    covered: list[tuple[int, int]] = []

    for pat_name, rx in _COMPILED:
        use_rx = rx
        if pat_name == "single_bare" and "b" in option:
            use_rx = _SINGLE_BARE_COMMA
        for m in use_rx.finditer(text):
            start, end = m.start(), m.end()
            if any(s < end and start < e for s, e in covered):
                continue
            groups = m.groups()
            year_str = groups[-1]
            try:
                year = int(year_str)
            except ValueError:
                continue
            if year < 1900 or year > 2099:
                continue
            if max_year is not None and year > max_year:
                continue

            if pat_name == "single_bare":
                lead = _leading_token(m.group(0)).lower().rstrip(",")
                # strip any name prefix (van, de, ...) to test the surname token
                lead_last = lead.split()[-1] if " " in lead else lead
                if "a" in option and (lead in BLACKLIST or lead_last in BLACKLIST):
                    continue
                if "c" in option and not _preceding_word_lowercase(text, start):
                    continue

            surname = re.sub(r"[\s']", "", groups[0])
            surname = surname.split()[-1] if " " in surname else surname
            results.append({
                "surname": surname.lower(), "year": year, "raw": m.group(0),
                "pattern": pat_name, "start": start, "end": end,
            })
            covered.append((start, end))
    return results


OPTIONS = ["a", "b", "c", "ab", "ac", "abc"]


def _iter_chunks(max_rows):
    cols = ["filter_status", "filter_evidence", "title_r", "abstract_r", "year_r"]
    if FILTERED_CSV.exists():
        src = pd.read_csv(FILTERED_CSV, dtype=str, encoding="utf-8-sig",
                          usecols=cols, chunksize=100_000, low_memory=False)
        for c in src:
            yield c
        return
    proc = subprocess.Popen(["unzip", "-p", str(FILTERED_ZIP), "filtered.csv"],
                            stdout=subprocess.PIPE)
    read = 0
    for c in pd.read_csv(proc.stdout, dtype=str, encoding="utf-8-sig",
                         usecols=cols, chunksize=100_000, low_memory=False):
        yield c
        read += len(c)
        if max_rows and read >= max_rows:
            proc.terminate()
            break
    try:
        proc.stdout.close()
        proc.wait(timeout=5)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rows", type=int, default=None,
                    help="Stop after scanning ~N rows (for quick iteration).")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent / "citation_gate_out"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    status_counts: Counter = Counter()
    total_rows = 0
    cite_rows = 0                    # rows where the gate fired (cite: in evidence)
    gate_accepts = 0                 # cite rows currently classified replication/reproduction
    current_nonempty = 0            # cite rows where the current extractor re-fires
    changed = {o: 0 for o in OPTIONS}          # accept -> needs_review flips
    removed_examples = {o: [] for o in OPTIONS}  # reservoir of removed raw matches
    removed_seen = {o: 0 for o in OPTIONS}
    RESERVOIR = 400

    def _year_int(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None

    for chunk in _iter_chunks(args.max_rows):
        chunk = chunk.fillna("")
        total_rows += len(chunk)
        status_counts.update(chunk["filter_status"])
        mask = chunk["filter_evidence"].str.contains("cite:", regex=False)
        sub = chunk[mask]
        for _, row in sub.iterrows():
            cite_rows += 1
            if row["filter_status"] in ("replication", "reproduction"):
                gate_accepts += 1
            text = f"{row['title_r']}\n{row['abstract_r']}".strip()
            yr = _year_int(row["year_r"])
            cur = extract(text, max_year=yr, option="current")
            if not cur:
                continue
            current_nonempty += 1
            cur_spans = {(m["start"], m["end"]) for m in cur}
            for o in OPTIONS:
                opt = extract(text, max_year=yr, option=o)
                if not opt:
                    changed[o] += 1
                    opt_spans = set()
                else:
                    opt_spans = {(m["start"], m["end"]) for m in opt}
                # collect matches present in current but removed by this option
                for m in cur:
                    if (m["start"], m["end"]) not in opt_spans:
                        removed_seen[o] += 1
                        # reservoir sampling
                        if len(removed_examples[o]) < RESERVOIR:
                            removed_examples[o].append(_ctx(text, m))
                        else:
                            j = random.randint(0, removed_seen[o] - 1)
                            if j < RESERVOIR:
                                removed_examples[o][j] = _ctx(text, m)

    # ---- report -------------------------------------------------------------
    print("=" * 78)
    print("CITATION GATE ANALYSIS")
    print("=" * 78)
    print(f"total rows scanned          : {total_rows:,}")
    print(f"status distribution         : {dict(status_counts)}")
    print(f"cite:-evidence rows (gate)  : {cite_rows:,}")
    print(f"  of which replication/repro: {gate_accepts:,}")
    print(f"  current extractor re-fires: {current_nonempty:,}  (baseline accept pool)")
    print()
    print(f"{'option':6} {'decision_changes':>17} {'% of baseline':>14} {'matches_removed':>16}")
    for o in OPTIONS:
        pct = 100.0 * changed[o] / current_nonempty if current_nonempty else 0.0
        print(f"{o:6} {changed[o]:>17,} {pct:>13.2f}% {removed_seen[o]:>16,}")
    print()
    print("Option key: a=blacklist leading token  b=require comma before bare year")
    print("            c=require preceding word lowercase   (single_bare only)")
    print()
    for o in OPTIONS:
        f = out_dir / f"removed_examples_{o}.txt"
        random.shuffle(removed_examples[o])
        f.write_text("\n".join(removed_examples[o][:200]), encoding="utf-8")
        print(f"wrote {min(200, len(removed_examples[o]))} removed examples -> {f}")


def _ctx(text: str, m: dict) -> str:
    s = max(0, m["start"] - 40)
    e = min(len(text), m["end"] + 15)
    ctx = text[s:e].replace("\n", " ")
    return f"[{m['pattern']}] …{ctx}…   (raw={m['raw']!r})"


if __name__ == "__main__":
    main()
