"""eval_override.py — what each pattern in hard_signal() actually buys.

    .venv/bin/python analysis/prescreen_eval/eval_override.py            # the tables
    .venv/bin/python analysis/prescreen_eval/eval_override.py --misses=40  # derivation-half
                                                                          # misses, to mine

Positives are FLoRA replications/reproductions (recall: a miss here is a paper the cheap
voters may discard for good). Negatives are non-replications that reach Stage 3 (cost: a
hit here is one needless validated-screen call, ~$0.0018 — not a lost paper).

Proposed additions are derived on the `dev` half of the *missed* positives and reported
on the `test` half only, using score_prescreen.py's split rule (md5 of the case id), so
the reported gain is out of sample.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from shared.prescreen import _SIGNAL_PATTERNS, hard_signal  # noqa: E402

SCREEN_CALL_USD = 0.0018
STAGE3_ROWS = 49_800          # rows/pass that reach Stage 3 (README.md, filtered.csv 2026-08-02)

# Candidate additions, derived by reading the dev half of the missed positives only.
# Tier A: cheap — a large share of the misses for almost no extra negative fires.
# Tier B: "replication of <a study-ish object>" — real gain, real cost.
# Tier C: bare "replication of" — the largest gain and by far the largest cost, because
#         "replication of DNA / HIV / the virus" is the same string.
PROPOSED: tuple[tuple[str, str, str], ...] = (
    ("A", "study_replicates",
     r"\b(?:stud(?:y|ies)|paper|article|experiments?|analys[ei]s|work|note|report|research"
     r"|investigation|manuscript)\s+(?:\w+\s+){0,2}replicat(?:e|es|ed|ing)\b"),
    ("A", "results_replicate",
     r"\b(?:results?|findings?|effects?|data)\s+(?:\w+\s+){0,1}replicat(?:e|es|ed)\b"),
    ("A", "replicate_and_extend",
     r"\breplicat(?:e|es|ed|ing|ion|ions)\s+and\s+(?:extend|expand|generali[sz]|elaborat|explor)\w*"
     r"|\b(?:extend|expand)\w*\s+and\s+replicat\w+"),
    ("A", "replicate_previous",
     r"\breplicat(?:e|es|ed|ing)\s+(?:the\s+|their\s+|these\s+|a\s+|an\s+)?"
     r"(?:previous|prior|earlier|original|published|key|main|core|central)\b"),
    ("A", "replicate_findings",
     r"\breplicat(?:e|es|ed|ing)\s+(?:the\s+|their\s+|these\s+|our\s+|its\s+|his\s+|her\s+|a\s+|an\s+)?"
     r"(?:findings?|results?|effects?|analys[ei]s|procedures?|estimates?|experiments?|stud(?:y|ies))\b"),
    ("A", "replicating_any", r"\breplicating\b"),
    ("A", "adj_replication",
     r"\b(?:narrow|wide|long|partial|approximate|full|successful|unsuccessful|failed"
     r"|methodological|experimental|attempted|scientific|near|quasi|constructive"
     r"|cross-?cultural|cross-?national|online|field|lab(?:oratory)?)[\s-]+replicat(?:ion|ions)\b"),
    ("A", "aim_replication", r"\b(?:aim|goal|purpose|objective)[^.]{0,40}\breplicat"),
    ("A", "failures_to_replicate",
     r"\b(?:failure|failures|attempt|attempts|attempting|efforts?)\s+to\s+replicat"),
    ("A", "not_replicate",
     r"\b(?:did|does|do|was|were|could|can|would|is|are)\s+not\s+(?:be\s+)?replicat(?:e|ed)\b"),
    ("A", "replication_cohort",
     r"\breplication\s+(?:analys[ei]s|sample|cohort|data\s?sets?|set|stage|phase|series"
     r"|trial|attempt|effort|package|material)"),
    ("A", "x_and_replication",
     r"\b(?:identification|discovery|fine-?mapping|detection|association|validation"
     r"|extension|confirmation)\s+and\s+replication\b"),
    ("A", "reproduce_their_results",
     r"\breproduc(?:e|es|ed|ing|tion)\s+(?:the|their|these|his|her|its|our|previously|original)\s+"
     r"(?:published\s+)?(?:results?|findings?|analys[ei]s|estimates?|numbers?|tables?|figures?"
     r"|work|stud(?:y|ies)|experiments?)\b"),
    ("A", "replicated_in_sample",
     r"\breplicated\s+in\s+(?:a|an|the|two|three|our|independent|separate)\b"),
    ("A", "repl_of_authoryear",
     r"\breplications?\s+of\s+[^.;:]{0,60}?(?:\(?(?:19|20)\d{2}\)?|et\s+al)"),
    ("A", "repl_of_prior",
     r"\breplications?\s+of\s+(?:the\s+|a\s+|an\s+|their\s+|this\s+|that\s+|these\s+|our\s+)?"
     r"(?:previous|prior|earlier|original|published|previously|older|existing|classic|seminal|key)"),
    ("B", "repl_of_object",
     r"\breplications?\s+of\s+(?:(?:the|a|an|their|this|these|our|two|three|\d+|several|other"
     r"|previously)\s+)?(?:\w+[\s-]){0,2}(?:stud(?:y|ies)|experiments?|findings?|results?|effects?"
     r"|analys[ei]s|papers?|articles?|research|trials?|surveys?|estimates?|associations?|loci)\b"),
    ("C", "repl_of_any", r"\breplications?\s+of\b"),
)


def split_of(case_id: str) -> str:
    return "dev" if int(hashlib.md5(case_id.encode()).hexdigest(), 16) % 2 else "test"


def load(name: str) -> list[dict]:
    return json.loads((HERE / name).read_text())


def text(c: dict) -> str:
    return f"{c.get('title') or ''}\n{c.get('abstract') or ''}"


def hits(pattern: str, cases: list[dict]) -> set[str]:
    rx = re.compile(pattern, re.IGNORECASE)
    return {c["id"] for c in cases if rx.search(text(c))}


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:5.1f}%" if d else "    - "


def per_pattern(pos: list[dict], neg: list[dict]) -> None:
    """Recall, recall unique to this pattern, and the two negative hit rates.

    $/pass prices the screen_discard hit rate — those are the rows the current Stage 3
    screen itself calls negative, so they are the closest thing here to the negatives a
    real pass meets. The curated_false_positive column is the pessimistic bound.
    """
    screen = [c for c in neg if c["bucket"] == "screen_discard"]
    curated = [c for c in neg if c["bucket"] == "curated_false_positive"]
    pos_hits = [hits(p, pos) for p in _SIGNAL_PATTERNS]
    print(f"\n=== per-pattern · {len(pos):,} positives · {len(screen)} screen_discard "
          f"· {len(curated):,} curated_false_positive ===\n")
    print(f"{'#':>3} {'pos hit':>10} {'unique':>7} {'neg screen':>11} {'neg curated':>11} "
          f"{'$/pass':>7}  pattern")
    for i, p in enumerate(_SIGNAL_PATTERNS):
        others = set().union(*(h for j, h in enumerate(pos_hits) if j != i))
        s, c = hits(p, screen), hits(p, curated)
        cost = len(s) / len(screen) * STAGE3_ROWS * SCREEN_CALL_USD
        print(f"{i:>3} {len(pos_hits[i]):>4} {pct(len(pos_hits[i]), len(pos))} "
              f"{len(pos_hits[i] - others):>7} {len(s):>4} {pct(len(s), len(screen))} "
              f"{len(c):>4} {pct(len(c), len(curated))} {cost:>6.0f}  {p[:60]}")


def overall(pos: list[dict], neg: list[dict]) -> list[dict]:
    """Print combined override performance; return the positives it misses."""
    missed = [c for c in pos if not hard_signal(c["title"], c["abstract"])]
    neg_hit = [c for c in neg if hard_signal(c["title"], c["abstract"])]
    print(f"\n=== combined override ===\n")
    print(f"positives           : {len(pos):,}")
    print(f"  override fires    : {len(pos) - len(missed):,} ({pct(len(pos) - len(missed), len(pos))})")
    print(f"  override misses   : {len(missed):,}")
    print(f"negatives           : {len(neg):,}")
    print(f"  override fires    : {len(neg_hit):,} ({pct(len(neg_hit), len(neg))})"
          f"  = ${len(neg_hit) / len(neg) * STAGE3_ROWS * SCREEN_CALL_USD:,.0f}/corpus pass")
    for cases, field, label in ((pos, "bucket", "positive recall by bucket"),
                                (pos, "filter_status", "positive recall by Stage 2 verdict"),
                                (neg, "bucket", "negative hit rate by bucket")):
        print(f"\n{label}:")
        for v in sorted({c.get(field, "") for c in cases}):
            sub = [c for c in cases if c.get(field, "") == v]
            fired = sum(1 for c in sub if hard_signal(c["title"], c["abstract"]))
            print(f"  {v or '(blank)':<24} {fired:>5}/{len(sub):<5} {pct(fired, len(sub))}")
    return missed


def proposals(pos: list[dict], missed: list[dict], neg: list[dict]) -> None:
    """Each candidate's gain, reported on the held-out half of the missed positives.

    The cost columns count only negatives the shipped override does NOT already fire on,
    because a row it already bypasses cannot be charged twice.
    """
    dev = [c for c in missed if split_of(c["id"]) == "dev"]
    test = [c for c in missed if split_of(c["id"]) == "test"]
    free = [c for c in neg if not hard_signal(c["title"], c["abstract"])]
    screen = [c for c in free if c["bucket"] == "screen_discard"]
    curated = [c for c in free if c["bucket"] == "curated_false_positive"]
    print(f"\n=== proposed additions · derived on dev ({len(dev):,} missed), "
          f"reported on test ({len(test):,} missed) ===")
    print(f"negatives the shipped override does not already catch: {len(free):,} "
          f"({len(screen)} screen_discard, {len(curated)} curated_false_positive)\n")
    print(f"{'tier name':<28} {'dev':>11} {'HELD-OUT':>11} {'neg screen':>11} "
          f"{'neg curated':>11} {'$/pass':>7}")
    for tier, name, pat in PROPOSED:
        d, t = hits(pat, dev), hits(pat, test)
        s, c = hits(pat, screen), hits(pat, curated)
        cost = len(s) / len(screen) * STAGE3_ROWS * SCREEN_CALL_USD
        print(f"{tier} {name:<26} {len(d):>4} {pct(len(d), len(dev))} {len(t):>4} "
              f"{pct(len(t), len(test))} {len(s):>4} {pct(len(s), len(screen))} "
              f"{len(c):>4} {pct(len(c), len(curated))} {cost:>6.0f}")

    pos_test = [c for c in pos if split_of(c["id"]) == "test"]
    for tiers in ("A", "AB", "ABC"):
        pats = [p for tier, _, p in PROPOSED if tier in tiers]
        combo = "|".join(f"(?:{p})" for p in pats)
        d, t = hits(combo, dev), hits(combo, test)
        s, c = hits(combo, screen), hits(combo, curated)
        cost = len(s) / len(screen) * STAGE3_ROWS * SCREEN_CALL_USD
        recall = sum(1 for x in pos_test
                     if hard_signal(x["title"], x["abstract"]) or re.search(combo, text(x), re.I))
        print(f"\ntier {tiers:<3} union            {len(d):>4} {pct(len(d), len(dev))} {len(t):>4} "
              f"{pct(len(t), len(test))} {len(s):>4} {pct(len(s), len(screen))} "
              f"{len(c):>4} {pct(len(c), len(curated))} {cost:>6.0f}")
        print(f"  held-out override recall on ALL positives: "
              f"{recall:,}/{len(pos_test):,} = {pct(recall, len(pos_test))} "
              f"(shipped: {pct(len(pos_test) - len(test), len(pos_test))})")


def residuals(pos: list[dict], missed: list[dict]) -> None:
    """The two avenues the handover asked about, and what is left after tier A."""
    other_lang = (r"replica[cç]i[oó]n|replicazione|replikation|réplic\w*|riproduzion\w*"
                  r"|reprodu[cç][ãa]o|reproducción|wiederholung|复现|重复实验|再現")
    family = r"replicat|reproduc|re-?analy"
    dev = [c for c in missed if split_of(c["id"]) == "dev"]
    tier_a = "|".join(f"(?:{p})" for tier, _, p in PROPOSED if tier == "A")
    print("\n=== residual ===\n")
    print(f"non-English replication vocabulary: {len(hits(other_lang, pos))} of {len(pos):,} "
          f"positives, {len(hits(other_lang, dev))} of {len(dev):,} dev misses")
    print(f"no replication-family word at all : {len(pos) - len(hits(family, pos))} of "
          f"{len(pos):,} positives, {len(dev) - len(hits(family, dev))} of {len(dev):,} dev misses")
    print(f"dev misses left after tier A      : {len(dev) - len(hits(tier_a, dev))} of {len(dev):,}")


def dump_misses(missed: list[dict], k: int) -> None:
    dev = [c for c in missed if split_of(c["id"]) == "dev"]
    print(f"\n=== {min(k, len(dev))} of {len(dev):,} dev-half misses ===\n")
    for c in dev[:k]:
        print(f"--- {c['id']} [{c['bucket']}] {c['doi']}")
        print(f"    T: {c['title'][:200]}")
        print(f"    A: {' '.join(c['abstract'].split())[:600]}")


def main() -> None:
    args = sys.argv[1:]
    pos, neg = load("override_positives.json"), load("override_negatives.json")
    per_pattern(pos, neg)
    missed = overall(pos, neg)
    proposals(pos, missed, neg)
    residuals(pos, missed)
    for a in args:
        if a.startswith("--misses"):
            dump_misses(missed, int(a.split("=")[1]) if "=" in a else 20)


if __name__ == "__main__":
    main()
