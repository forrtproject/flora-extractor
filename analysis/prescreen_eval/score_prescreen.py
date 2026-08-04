"""Score a pre-screen configuration: what it discards, and what it costs in lost papers.

Usage:  python3 score_prescreen.py <tag> <modelA> [modelB]
                                   [--split=dev|test|all] [--bypass=NAME] [--losses]

The gate under test: discard only when EVERY model answers "no". A missing answer, an
api_error or an unparseable reply is not a "no", so the row proceeds to the validated
screen. Bypasses run before the models and send the row straight to the validated screen:

  none    — models alone
  regex   — shared/prescreen.py's hard replication signals
  stage2  — Stage 2 already called it replication/reproduction at high confidence, or
            the row came from a curated source
  short   — abstract under 200 characters: too little text for a 3B-class model
  all     — every bypass above (the shipped configuration)

Rows Stage 2 marked `false_positive` never reach Stage 3, so counting them as negatives
the pre-screen "saved" would inflate the benefit; they are excluded throughout.

Prompt and model selection happens on the `dev` half and is reported on `test` — the
1,486 cases are the same rows that motivated the design, so a variant chosen on all of
them carries a winner's curse. The split is a deterministic hash of the case id.
"""
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.prescreen import hard_signal  # noqa: E402

HERE = Path(__file__).resolve().parent

BUCKETS = [("goldpos_flora", "GOLD+ FLoRA entries", True),
           ("goldpos_repro", "GOLD+ curated reproductions", True),
           ("goldneg_curated", "GOLD- curated negatives", False),
           ("goldneg_screen", "GOLD- screen-confirmed", False)]

# What shared/prescreen.prescreen_bypass() actually does — "all" must mean the shipped
# configuration, or the scorer measures something nobody can run.
BYPASSES = ("regex", "short", "curated")
# Measured, not shipped: 98% of the rows that reach Stage 3 carry Stage 2's
# high-confidence replication verdict, including every screen-confirmed negative, so
# bypassing on it disables the tier. Available as a diagnostic, excluded from "all".
DIAGNOSTIC_BYPASSES = ("stage2",)


def split_of(case_id: str) -> str:
    return "dev" if int(hashlib.md5(case_id.encode()).hexdigest(), 16) % 2 else "test"


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson interval — the normal approximation is useless at k=0."""
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def bypasses_for(case: dict) -> set[str]:
    """Which bypasses fire for this case."""
    out = set()
    if hard_signal(case.get("title", ""), case.get("abstract", "")):
        out.add("regex")
    if len((case.get("abstract") or "").strip()) < 200:
        out.add("short")
    if case.get("filter_evidence", "").startswith("curated_source:"):
        out.add("curated")
    if (case.get("filter_status", "") in {"replication", "reproduction"}
            and case.get("filter_confidence", "") == "high"):
        out.add("stage2")
    return out


def load_results(tag: str, model: str, bucket: str) -> dict[str, dict]:
    path = HERE / f"pre_{tag}_{model.replace('/', '_')}_live_{bucket}.json"
    if not path.exists():
        return {}
    return {r["id"]: r for r in json.loads(path.read_text())}


def says_no(rec: "dict | None") -> bool:
    """Only an explicit, parsed 'no' counts. Everything else proceeds."""
    return bool(rec) and not rec.get("error") and rec.get("verdict") == "no"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a.split("=")[0].lstrip("-"): a.split("=", 1)[1]
            for a in sys.argv[1:] if "=" in a}
    if not args:
        print(__doc__)
        sys.exit(1)
    tag, models = args[0], args[1:]
    split = opts.get("split", "all")
    bypass = opts.get("bypass", "none")
    active = (set(BYPASSES) if bypass == "all"
              else {bypass} & set(BYPASSES + DIAGNOSTIC_BYPASSES))
    show_losses = "--losses" in sys.argv

    print(f"\n=== prompt {tag} · {', '.join(models)} · split={split} · bypass={bypass} ===\n")
    cols = "".join(f"{m.split('/')[-1][:13]:>15}" for m in models)
    header = f"{'bucket':<28}{'n':>5}{cols}{'AND':>13}"
    print(header)
    print("-" * len(header))

    losses: list[tuple[str, str, str, set]] = []
    totals = {"neg_n": 0, "neg_discard": 0, "pos_n": 0, "pos_discard": 0}
    for bucket, label, is_positive in BUCKETS:
        cases = {c["id"]: c for c in
                 json.loads((HERE / f"cases_live_{bucket}.json").read_text())}
        per_model = [load_results(tag, m, bucket) for m in models]
        ids = [i for i in cases
               if any(i in pm for pm in per_model)
               and cases[i].get("filter_status") != "false_positive"
               and (split == "all" or split_of(i) == split)]
        if not ids:
            continue

        singles = [sum(says_no(pm.get(i)) for i in ids) for pm in per_model]
        discarded = [i for i in ids
                     if all(says_no(pm.get(i)) for pm in per_model)
                     and not (bypasses_for(cases[i]) & active)]

        n = len(ids)
        cells = "".join(f"{s:>9} {100*s/n:>4.0f}%" for s in singles)
        print(f"{label:<28}{n:>5}{cells}{len(discarded):>8} {100*len(discarded)/n:>3.0f}%")
        key = "pos" if is_positive else "neg"
        totals[f"{key}_n"] += n
        totals[f"{key}_discard"] += len(discarded)
        if is_positive:
            for i in discarded:
                losses.append((bucket, i, cases[i].get("doi", ""), bypasses_for(cases[i])))
        errs = sum(1 for pm in per_model for i in ids
                   if pm.get(i, {}).get("error") or pm.get(i, {}).get("schema_error"))
        if errs:
            print(f"{'  (api/schema failures)':<28}{errs:>5}")
        # A row one voter never scored can never be an AND-discard, so an unfinished
        # run reads as a clean result. Say so rather than let it pass for a measurement.
        thin = [m for m, pm in zip(models, per_model)
                if len([i for i in ids if i in pm]) < len(ids)]
        if thin:
            covered = [len([i for i in ids if i in pm]) for pm in per_model]
            print(f"{'  INCOMPLETE — AND is a floor':<28}{'':>5}  coverage: "
                  + ", ".join(f"{m.split('/')[-1][:14]}={c}/{len(ids)}"
                              for m, c in zip(models, covered)))

    lo, hi = wilson(totals["pos_discard"], totals["pos_n"])
    print(f"\nnegatives discarded : {totals['neg_discard']:>4}/{totals['neg_n']:<5}"
          f" {100*totals['neg_discard']/max(1,totals['neg_n']):>5.1f}%   (the saving)")
    print(f"positives discarded : {totals['pos_discard']:>4}/{totals['pos_n']:<5}"
          f" {100*totals['pos_discard']/max(1,totals['pos_n']):>5.1f}%   (the loss)")
    print(f"  miss rate 95% CI  : {100*lo:.2f}% – {100*hi:.2f}%")

    tok = sum((r.get("usage") or {}).get("in", 0) + (r.get("usage") or {}).get("out", 0)
              for b, _, _ in BUCKETS for m in models
              for r in load_results(tag, m, b).values())
    print(f"tokens (all splits) : {tok:,}")

    if show_losses and losses:
        print("\n--- gold positives this configuration would discard ---")
        for bucket, cid, doi, byp in losses:
            missed = ",".join(sorted(byp - active)) or "-"
            print(f"  {bucket:<16}{cid:<8}{doi:<42}other bypasses that saw it: {missed}")


if __name__ == "__main__":
    main()
