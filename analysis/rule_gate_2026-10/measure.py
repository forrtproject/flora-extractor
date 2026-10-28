"""Per-rule evidence and deferred-work counts for the rule-dependent admission gate.

Offline over the frozen inputs `pull.py` writes to `out/`; the gate assignment is
read from the spec bundle (`admission`), so this reports the bundle as committed.

    .venv/bin/python -m analysis.rule_gate_2026-10.measure

A work's gate is the MOST LENIENT one among the live screen-pile rules it matched
(`filter/engine/handoff.py:admission_gates`). Endings are each work's best file
across `extracted.csv` and the set-asides; the validator columns count works whose
validator marked the type incorrect with `not_validation` (any of the two slots).
"""

import json
from pathlib import Path

import pandas as pd

from filter.engine.spec import LENIENT, SCREEN_PILES, load_specs
from shared.llm_client import screen_strict

OUT = Path(__file__).parent / "out"
ORDER = ["extracted", "not_a_replication", "no_original_found",
         "prospective_registration", "no_evidence", "unidentified_original",
         "keyed_link_disputed", "search_link_unconfirmed", "target_pending",
         "api_error"]


def _pct(x: float) -> str:
    return "–" if pd.isna(x) else f"{100 * x:.0f}%"


def _nar(frame: pd.DataFrame) -> dict:
    """Validated works in *frame* and how many a validator called not a replication."""
    judged = frame[frame.judged.notna()]
    return {"n": len(judged), "not_a_replication": int(judged.nar_any.sum())}


def main() -> None:
    specs = {s.id: s for s in load_specs(Path("filter/spec")) if s.pile in SCREEN_PILES}
    routing = pd.read_parquet(OUT / "routing.parquet")
    routing["matched"] = routing.matched_rules.map(json.loads)
    screen = pd.read_parquet(OUT / "screen.parquet")
    screen["strict"] = screen.votes.map(lambda v: screen_strict(json.loads(v)))
    endings = pd.read_parquet(OUT / "endings.parquet")
    endings["rank"] = endings.file.map(ORDER.index)
    ending = endings.sort_values("rank").drop_duplicates("work_id")[["work_id", "file"]]
    validated = pd.read_parquet(OUT / "validated.parquet")

    def gate(rules: list[str]) -> str:
        return ("lenient" if any(specs.get(r) and specs[r].admission == LENIENT
                                 for r in rules) else "strict")

    df = (routing.merge(screen, on="work_id", how="left")
          .merge(ending, on="work_id", how="left")
          .merge(validated, on="work_id", how="left"))
    df["gate"] = df.matched.map(gate)
    df["gate_winner"] = df.rule_id.map(lambda r: gate([r]))
    proceed = df.outcome == "proceed"
    df["deferred"] = proceed & (df.gate == "strict") & ~df.strict.fillna(False)
    df["deferred_winner"] = (proceed & (df.gate_winner == "strict")
                             & ~df.strict.fillna(False))

    lines = ["| rule | gate | routed (winner) | proceed | strict among proceeds | "
             "Stage 3 shipped / not_a_rep / no_orig / prospective | validated proceeds: n, "
             "not a replication when strict passes vs fails | deferred |",
             "|---|---|--:|--:|--:|---|---|--:|"]
    for rule in sorted(set(df.rule_id)):
        win = df[df.rule_id == rule]
        p = win[win.outcome == "proceed"]
        ended = p[p.file.notna()]
        val = win[win.judged.notna() & (win.outcome == "proceed")]
        vs, vl = val[val.strict == True], val[val.strict == False]  # noqa: E712
        lines.append(
            f"| `{rule}` | {specs[rule].admission} | {len(win):,} | {len(p):,} | "
            f"{_pct(p.strict.mean())} | {_pct((ended.file == 'extracted').mean())} / "
            f"{_pct((ended.file == 'not_a_replication').mean())} / "
            f"{_pct((ended.file == 'no_original_found').mean())} / "
            f"{_pct((ended.file == 'prospective_registration').mean())} | "
            f"{len(val)}: {_pct(vs.nar_any.mean())} (n={len(vs)}) vs "
            f"{_pct(vl.nar_any.mean())} (n={len(vl)}) | "
            f"{int(win.deferred.sum()):,} |")
    d = df[df.deferred]
    summary = {
        "release_id": json.loads((OUT / "manifest.json").read_text())["release_id"],
        "screen_pile_works": len(df), "proceeds": int(proceed.sum()),
        "deferred": int(df.deferred.sum()),
        "deferred_if_winner_gate": int(df.deferred_winner.sum()),
        "deferred_already_ended": d.file.value_counts().to_dict(),
        "deferred_never_extracted": int(d.file.isna().sum()),
        "validated_deferred_n": int(d.judged.notna().sum()),
        "validated_deferred_not_a_rep": int(d.nar_any.fillna(False).sum()),
        "validated_proceed_strict_pass": _nar(df[proceed & df.strict.fillna(False)]),
        "validated_proceed_strict_fail": _nar(df[proceed & ~df.strict.fillna(False)]),
        "validated_admitted_n": int(df[proceed & ~df.deferred].judged.notna().sum()),
        "validated_admitted_not_a_rep": int(
            df[proceed & ~df.deferred].nar_any.fillna(False).sum()),
    }
    text = "\n".join(lines) + "\n\n```json\n" + json.dumps(summary, indent=1) + "\n```\n"
    (OUT / "table.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
