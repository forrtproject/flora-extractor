"""Tables for REPORT.md from the cached answers of `run.py` (no API calls).

Reads every results_<model>_<effort>_<framing>.csv, and scores:
  - each run on its own (flags on judged-wrong picks, false flags on controls);
  - combinations (blind OR contrast; DeepSeek AND GLM);
  - against the picks a replay would still ship: `replay_gpt-6-luna.csv` (the model
    swap alone) and, when present, `analysis/pick_pilot/replay_newprompt.csv` (the
    step-3 prompt). The BLIND answer does not depend on the pick, so it scores any
    pick — including a new prompt's switched pick — without another call.

    .venv/bin/python -m analysis.contrastive_confirm.analyze
"""
from __future__ import annotations

import ast
import glob
import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LUNA = ROOT / "analysis/mo_observatory/adjudication/replay_gpt-6-luna.csv"
NEWPROMPT = ROOT / "analysis/pick_pilot/replay_newprompt.csv"
GROUPS = {"mo": "wrong", "ours": "right", "both": "right", "split": "split",
          "agrees_with_mo": "control"}


def runs() -> dict[str, pd.DataFrame]:
    out = {}
    for f in sorted(glob.glob(str(HERE / "results_*.csv"))):
        name = Path(f).stem.removeprefix("results_")
        d = pd.read_csv(f, dtype=str, keep_default_na=False)
        for c in ("flag", "flag_conf", "flag_names_other", "alt_is_mo"):
            d[c] = d[c] == "True"
        d["g"] = d.label.map(GROUPS)
        out[name] = d
    return out


def line(name: str, d: pd.DataFrame, flag: str = "flag") -> dict:
    g = d.groupby("g")
    return {"run": name,
            "wrong flagged /56": f"{g[flag].sum()['wrong']}",
            "…names judges' original": f"{(d[flag] & d.alt_is_mo)[d.g == 'wrong'].sum()}",
            "controls false-flagged /150": f"{g[flag].sum()['control']}",
            "right (ours/both) flagged /10": f"{g[flag].sum()['right']}",
            "split flagged /13": f"{g[flag].sum()['split']}",
            "no answer": f"{(d.error != '').sum()}"}


def keys(v: str) -> list[str]:
    try:
        return list(ast.literal_eval(v)) if v else []
    except (ValueError, SyntaxError):
        return []


def blind_sets(d: pd.DataFrame) -> list[tuple[str, list[str]]]:
    """(status, identified keys) per row of a blind run."""
    out = []
    for a in d.answer:
        st, _, ks = a.partition(":")
        out.append((st, [k for k in ks.split(",") if k] if st == "identified" else []))
    return out


def norm(k: str) -> str:
    """One spelling for a duplicate-key suffix: the step-4 fix renames `@x1999b` to
    `@x1999_2`, and the new-prompt replay writes every key in the new form."""
    m = re.fullmatch(r"(@[a-z]+\d{4})([b-z])", k)
    return f"{m[1]}_{ord(m[2]) - ord('a') + 1}" if m else k


def against_replay(name: str, d: pd.DataFrame, rp: pd.DataFrame, label: str) -> str:
    """Score a blind run against the picks a replay ships (match_certain keys)."""
    m = d.merge(rp[["doi_r", "new_keys"]], on="doi_r", how="left")
    m["picks"] = m.new_keys.fillna("").map(keys)
    bs = blind_sets(d)
    rows = []
    for (st, ident), r in zip(bs, m.itertuples()):
        if not r.picks or r.error:
            continue
        ident = {norm(k) for k in ident}
        old, mk = norm(r.old_key), norm(r.mo_key)
        for p in map(norm, r.picks):
            kind = r.g
            if r.g == "wrong":
                kind = "repeat" if p == old else "to_mo" if p == mk else "other"
            rows.append({"kind": kind, "flag": p not in ident, "alt_mo": bool(mk) and mk in ident})
    t = pd.DataFrame(rows)
    n = t.kind.value_counts()
    f = t[t.flag].kind.value_counts()
    fm = t[t.flag & t.alt_mo].kind.value_counts()
    c = lambda k: f"{f.get(k, 0)}/{n.get(k, 0)}"
    return (f"| {name} | {label} | {c('repeat')} ({fm.get('repeat', 0)}) | {c('other')} "
            f"| {c('to_mo')} | {c('control')} | {c('right')} |")


def main() -> None:
    R = runs()
    rows = [line(n, d) for n, d in R.items()]
    rows += [line(n + " (confident only)", d, "flag_conf") for n, d in R.items()]
    for model in ("deepseek-v4.1-flash_medium", "glm-5.3-flash_medium"):
        b, c = R.get(f"{model}_blind"), R.get(f"{model}_contrast")
        if b is not None and c is not None:
            u = b.copy()
            u["flag"] = b.flag | c.flag
            u["alt_is_mo"] = b.alt_is_mo | c.alt_is_mo
            u["error"] = b.error + c.error
            rows.append(line(f"{model} blind OR contrast", u))
    b1, b2 = R.get("deepseek-v4.1-flash_medium_blind"), R.get("glm-5.3-flash_medium_blind")
    if b1 is not None and b2 is not None:
        for op, f in (("AND", lambda x, y: x & y), ("OR", lambda x, y: x | y)):
            u = b1.copy()
            u["flag"] = f(b1.flag, b2.flag)
            u["alt_is_mo"] = f(b1.alt_is_mo, b2.alt_is_mo)
            rows.append(line(f"blind DeepSeek {op} GLM", u))
    t = pd.DataFrame(rows)
    print("| " + " | ".join(t.columns) + " |\n|" + "---|" * len(t.columns))
    for r in t.itertuples(index=False):
        print("| " + " | ".join(map(str, r)) + " |")

    print("\nflagged / picks the replay ships. repeat = the judged-wrong pick again (in brackets: "
          "the flag names the judges' original); other = a different pick on a wrong row that is not "
          "the judges' original; to_mo = the replay switched to the judges' original (a flag here is a "
          "false flag); control / right = correct picks kept (false flags).\n")
    print("| blind run | replay | repeat | other | to_mo | control | right (ours/both) |\n|---|---|---:|---:|---:|---:|---:|")
    replays = [(LUNA, "gpt-6-luna, old prompt")]
    if NEWPROMPT.exists():
        replays.append((NEWPROMPT, "step-3 new prompt"))
    rep1 = NEWPROMPT.with_name("replay_newprompt_rep1.csv")
    if rep1.exists():
        replays.append((rep1, "step-3 new prompt, rep1"))
    for path, lab in replays:
        rp = pd.read_csv(path, dtype=str, keep_default_na=False)
        for n, d in R.items():
            if n.endswith("_blind"):
                print(against_replay(n, d, rp, lab))
    if not NEWPROMPT.exists():
        print(f"\n({NEWPROMPT.relative_to(ROOT)} not present — new-prompt comparison pending)")


if __name__ == "__main__":
    main()
