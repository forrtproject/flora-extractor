"""Score prompt_v32 against prompt_v3 on the CORRECTED truth labels and write report_v32.md.

v3.2 keeps every v3.1 rule fix and replaces the three-level `"confidence"` field with a
binary `"confident": true|false`. Both prompts are scored against the same corrected
labels (`human_truth_v31.json`, `heldout_truth_v31.json`, FLoRA positives all yes), so
the v3 numbers here are not the ones in `report_v3.md`.

The discard gate is the same idea under both schemas: every voter answers `none` and
every voter is confident — `confidence == "high"` for v3, `confident == true` for v32.

Offline: reads the voter_v3_* and voter_v32_* files already in this directory.
"""
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent

FL = "gemini-3.5-flash-lite"
GPT = "gpt-5.4-mini"
MIN = "mistralai/ministral-14b-2512"
MODELS = [FL, GPT, MIN]
SHORT = {FL: "flash-lite", GPT: "gpt-5.4-mini", MIN: "ministral-14b"}

MAP = {"replication": "yes", "reproduction": "yes", "both": "yes",
       "none": "no", "unclear": "unclear"}

SETS = {
    "flora": ("flora_positive_cases.json", None, "flora_positive"),
    "human": ("human_cases.json", "human_truth_v31.json", "human"),
    "heldout": ("heldout_cases.json", "heldout_truth_v31.json", "heldout"),
}

PENDING = {"F140"}

PATTERNS = {
    "P1 internal two-stage discovery": ["HU21", "HU27", "HU51", "H005", "H011", "H012",
                                        "H021", "H028"],
    "P2 internal replication across studies": ["HU05", "HU08", "HU34", "HU41"],
    "P3 tool/model benchmarking": ["HU07", "HU28", "HU33", "HU39", "HU48", "HU53",
                                   "HU59", "HU60"],
    "P4 inter-lab / technical reproducibility": ["HU02", "HU09", "HU18", "HU40", "HU46",
                                                 "H001", "H008"],
    "P5 translation / adaptation of an instrument": ["HU17", "HU58", "H003", "H006",
                                                     "H014", "H026"],
    "P6 incidental agreement credited as an aim": ["HU01", "HU12", "HU35", "HU45"],
    "P7 vocabulary misfire": ["HU44", "HU54", "H027"],
}

FLIPPED = ["HU17", "HU46", "HU58", "H003", "H006", "H014", "H026"]

ALLOWED_CATS = {"clearly_declared", "self_retest", "measurement_validation",
                "context_transfer", "incidental_finding", "initial_validation",
                "tool_benchmark", "builds_on_literature", "terminology_only",
                "about_replication", "other"}

PRICE = {FL: (0.3e-6, 2.5e-6), GPT: (0.75e-6, 4.5e-6), MIN: (0.2e-6, 0.2e-6)}
PROV = {FL: "Gemini API", GPT: "OpenAI", MIN: "OpenRouter"}


def load_cases(name: str) -> dict[str, dict]:
    return {c["id"]: c for c in json.loads((HERE / name).read_text())}


def read(tag: str, model: str, stem: str) -> dict[str, dict]:
    """One result file. `conf` is the raw confidence field of whichever schema the file
    uses; `confident` is the schema-independent boolean the gate needs."""
    f = HERE / f"voter_{tag}_{model.replace('/', '_')}_{stem}.json"
    if not f.exists():
        return {}
    out = {}
    for r in json.loads(f.read_text()):
        bad = bool(r.get("schema_error")) or bool(r.get("error"))
        if "confident" in r:
            raw = r.get("confident")
            confident = raw if isinstance(raw, bool) else None
            conf_label = None if confident is None else str(confident).lower()
        else:
            raw = r.get("confidence")
            conf_label = raw
            confident = (raw == "high") if raw else None
        out[r["id"]] = {"verdict": None if bad else MAP.get(r.get("classification") or ""),
                        "conf": None if bad else conf_label,
                        "confident": None if bad else confident,
                        "cats": [str(c) for c in (r.get("categories") or [])],
                        "raw_class": r.get("classification"),
                        "schema_error": r.get("schema_error"), "error": r.get("error"),
                        "usage": r.get("usage") or {}}
    return out


def g_strict(recs: list[Optional[dict]]) -> bool:
    """The discard gate: every voter answers `none` and every voter is confident."""
    if any(r is None or r["verdict"] is None for r in recs):
        return False
    return all(r["verdict"] == "no" and r["confident"] is True for r in recs)


def pct(n: int, d: int) -> str:
    return f"{n}/{d} = {n / d:.1%}" if d else "n/a"


def main() -> None:
    cases = {s: load_cases(cf) for s, (cf, _, _) in SETS.items()}
    truth = {}
    for s, (_, tf, _) in SETS.items():
        truth[s] = ({i: "yes" for i in cases[s]} if tf is None
                    else json.loads((HERE / tf).read_text())["truth"])

    data = {(t, m, s): read(t, m, SETS[s][2])
            for t in ("v3", "v32") for m in MODELS for s in SETS}
    proxy_cases = load_cases("coding_v3_cases.json")
    proxy = {t: read(t, FL, "coding_v3") for t in ("v3", "v32")}

    GROUPS = [("flash-lite+gpt-5.4-mini", (FL, GPT)),
              ("flash-lite+ministral-14b", (FL, MIN)),
              ("trio (all three)", (FL, GPT, MIN))]

    L: list[str] = ["# prompt_v32 vs prompt_v3 — evaluation on the corrected truth labels",
                    ""]
    L.append("Generated by `score_v32.py`. `prompt_v32.txt` is `prompt_v31.txt` (the P1-P4, "
             "P6, P7 rule fixes plus the instrument-boundary ruling) with one schema change: "
             "the three-level `\"confidence\": high|medium|low` field is replaced by a binary "
             "`\"confident\": true|false` carrying an explicit definition.")
    L.append("")
    L.append("**Both prompts are scored against the corrected labels** from the "
             "instrument-boundary ruling (`truth_flips_v31.md`): `human_truth_v31.json` "
             "(60 cases: 47 `no`, 13 `yes`), `heldout_truth_v31.json` (30: 20 `no`, 10 "
             "`yes`) and the 300 FLoRA entries in `flora_positive_cases.json` (all `yes`). "
             "The v3 numbers therefore differ from `report_v3.md`, which used the pre-ruling "
             "labels.")
    L.append("")
    L.append("Verdict mapping: `replication`/`reproduction`/`both` -> yes, `none` -> no, "
             "`unclear` -> unclear. A row with a schema or API error counts as no answer, so "
             "it can never contribute to a discard.")
    L.append("")
    L.append("Discard gate for the primary tables: **G-strict** — every voter in the group "
             "answers `none` *and* is confident (`confidence == high` under v3, "
             "`confident == true` under v32). `F140` (an adjudication-pending FLoRA entry) is "
             "reported apart from the settled misses. Gate alternatives, including ones that "
             "exploit the new binary field on the qualifying side, are swept in "
             "`gate_sweep_v32.md`.")
    L.append("")
    L.append("All of these cases are derivation data (see `README.md`), so every number is "
             "in-sample.")
    L.append("")
    L.append("**Provenance note.** `voter_v3_gemini-3.5-flash-lite_human.json` was "
             "overwritten during this session's tooling check and re-run against "
             "`prompt_v3.txt` with the same model. The re-run reproduces the original's "
             "verdicts exactly — the per-model row below (13/13 sensitivity, 31/47 "
             "specificity, 1/47 unclear) and the v3 headline discard counts (43/67, 36/67, "
             "34/67) are identical to `report_v31.md`, which was computed from the original "
             "file. Two of the 60 confidences moved `high` -> `medium`, which does not "
             "change any gate outcome reported here.")
    L.append("")

    # ---------------------------------------------------------------- headline
    hard_neg = [(s, i) for s in ("human", "heldout")
                for i in cases[s] if truth[s].get(i) == "no"]
    L += ["## Headline — v3 vs v32 under G-strict", "",
          f"Hard-negative pool: {len(hard_neg)} corrected true negatives "
          f"({sum(1 for s, _ in hard_neg if s == 'human')} human + "
          f"{sum(1 for s, _ in hard_neg if s == 'heldout')} held-out).", "",
          "| prompt | group | settled misses | pending (F140) | hard-neg discard |",
          "| --- | --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        for gname, ms in GROUPS:
            miss, pend = [], []
            for s in SETS:
                for i in cases[s]:
                    if truth[s].get(i) != "yes":
                        continue
                    if g_strict([data[(t, m, s)].get(i) for m in ms]):
                        (pend if i in PENDING else miss).append(f"{s}:{i}")
            hn = sum(1 for s, i in hard_neg
                     if g_strict([data[(t, m, s)].get(i) for m in ms]))
            L.append(f"| {t} | {gname} | {len(miss)} | {len(pend)} | "
                     f"{hn}/{len(hard_neg)} = {hn / len(hard_neg):.1%} |")
    L.append("")

    # ---------------------------------------------------------------- misses per set
    L += ["## 1. Miss rate per case set", "",
          "A miss is a corrected truth-`yes` case the group discards.", "",
          "| prompt | group | set | positives | settled misses | miss rate | pending | "
          "group could not answer |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    miss_detail: list[tuple[str, ...]] = []
    for t in ("v3", "v32"):
        for gname, ms in GROUPS:
            for s in SETS:
                pos = [i for i in cases[s] if truth[s].get(i) == "yes"
                       and all(i in data[(t, m, s)] for m in ms)]
                if not pos:
                    continue
                miss = pend = na = 0
                for i in pos:
                    recs = [data[(t, m, s)].get(i) for m in ms]
                    if any(r is None or r["verdict"] is None for r in recs):
                        na += 1
                        continue
                    if g_strict(recs):
                        if i in PENDING:
                            pend += 1
                        else:
                            miss += 1
                        miss_detail.append((t, gname, s, i,
                                            "PENDING" if i in PENDING else "settled",
                                            cases[s][i].get("title", "")[:110]
                                            .replace("\n", " ").replace("|", "/")))
                L.append(f"| {t} | {gname} | {s} | {len(pos)} | {miss} "
                         f"| {miss / len(pos):.1%} | {pend} | {na} |")
    L.append("")
    L += ["### Every discarded positive (case id + title)", ""]
    if miss_detail:
        L += ["| prompt | group | set | id | status | title |",
              "| --- | --- | --- | --- | --- | --- |"]
        for row in miss_detail:
            L.append("| " + " | ".join(row) + " |")
    else:
        L.append("None — no corrected true positive was discarded under either prompt by "
                 "any of the three groups.")
    L.append("")

    # ---------------------------------------------------------------- discard rate
    L += ["## 2. Discard rate on the corrected hard-negative pool", "",
          "| prompt | group | set | true negatives | discarded | rate |",
          "| --- | --- | --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        for gname, ms in GROUPS:
            for s in ("human", "heldout"):
                neg = [i for i in cases[s] if truth[s].get(i) == "no"]
                d = sum(1 for i in neg if g_strict([data[(t, m, s)].get(i) for m in ms]))
                L.append(f"| {t} | {gname} | {s} | {len(neg)} | {d} | {d / len(neg):.1%} |")
    L.append("")
    L.append("### Flash-lite-only analogue on the 150-case proxy pool")
    L.append("")
    L.append("`coding_v3_cases.json` is 150 past production discards, run through flash-lite "
             "only, so the single-model analogue of G-strict is `none` + confident. It is "
             "comparable across prompts but not to the two-voter column above.")
    L.append("")
    L += ["| prompt | n | none + confident | rate |", "| --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        d = proxy[t]
        if not d:
            continue
        n = sum(1 for i in proxy_cases
                if d.get(i) and d[i]["verdict"] == "no" and d[i]["confident"] is True)
        L.append(f"| {t} | {len(proxy_cases)} | {n} | {n / len(proxy_cases):.1%} |")
    L.append("")

    # ------------------------------------------------- 3. confidence-field diagnostic
    L += ["## 3. Confidence-field behaviour — the key diagnostic", "",
          "In v3 the three-level scale was effectively a constant: flash-lite answered "
          "`high` on 388 of 390 responses and ministral on 385 of 390, so the gate's "
          "high-vs-not distinction carried almost no information. The question for v32 is "
          "whether the binary field discriminates.", "",
          "### v32 — `confident` crossed with verdict, all sets pooled "
          "(flora 300 + human 60 + heldout 30; flash-lite also 150 proxy)", "",
          "| model | verdict | confident=true | confident=false | false-rate |",
          "| --- | --- | --- | --- | --- |"]
    v32_false: dict[str, tuple[int, int]] = {}
    for m in MODELS:
        recs = [r for s in SETS for r in data[("v32", m, s)].values()]
        if m == FL:
            recs += list(proxy["v32"].values())
        if not recs:
            continue
        tot_t = tot_f = 0
        for label, want in (("none", "no"), ("qualifying", "yes"), ("unclear", "unclear")):
            nt = sum(1 for r in recs if r["verdict"] == want and r["confident"] is True)
            nf = sum(1 for r in recs if r["verdict"] == want and r["confident"] is False)
            tot_t += nt
            tot_f += nf
            L.append(f"| {SHORT[m]} | {label} | {nt} | {nf} "
                     f"| {(nf / (nt + nf)) if nt + nf else 0:.1%} |")
        L.append(f"| **{SHORT[m]}** | **all** | **{tot_t}** | **{tot_f}** "
                 f"| **{(tot_f / (tot_t + tot_f)) if tot_t + tot_f else 0:.1%}** |")
        v32_false[SHORT[m]] = (tot_f, tot_t + tot_f)
    L.append("")
    L += ["### v3 — `confidence` crossed with verdict, same pooling", "",
          "| model | verdict | high | medium | low | not-high rate |",
          "| --- | --- | --- | --- | --- | --- |"]
    v3_nothigh: dict[str, tuple[int, int]] = {}
    for m in MODELS:
        recs = [r for s in SETS for r in data[("v3", m, s)].values()]
        if m == FL:
            recs += list(proxy["v3"].values())
        if not recs:
            continue
        th = tn = tmed = tlow = 0
        for label, want in (("none", "no"), ("qualifying", "yes"), ("unclear", "unclear")):
            sub = [r for r in recs if r["verdict"] == want]
            c = Counter(r["conf"] for r in sub)
            hi = c.get("high", 0)
            th += hi
            tn += len(sub) - hi
            tmed += c.get("medium", 0)
            tlow += c.get("low", 0)
            L.append(f"| {SHORT[m]} | {label} | {hi} | {c.get('medium', 0)} "
                     f"| {c.get('low', 0)} "
                     f"| {((len(sub) - hi) / len(sub)) if sub else 0:.1%} |")
        L.append(f"| **{SHORT[m]}** | **all** | **{th}** | **{tmed}** | **{tlow}** "
                 f"| **{(tn / (th + tn)) if th + tn else 0:.1%}** |")
        v3_nothigh[SHORT[m]] = (tn, th + tn)
    L.append("")
    L += ["### Does the binary field discriminate?", "",
          "| model | v3 not-high rate | v32 confident=false rate |",
          "| --- | --- | --- |"]
    for m in MODELS:
        k = SHORT[m]
        if k not in v32_false or k not in v3_nothigh:
            continue
        a, an = v3_nothigh[k]
        b, bn = v32_false[k]
        L.append(f"| {k} | {a}/{an} = {a / an:.1%} | {b}/{bn} = {b / bn:.1%} |")
    L.append("")
    L.append("### Consequence for the gate")
    L.append("")
    L.append("If the confidence field were a constant, dropping it from the gate would "
             "change nothing. The comparison below is the same discard rule with and "
             "without the confidence clause (`both none, both confident` vs `both none`):")
    L.append("")
    L += ["| prompt | pair | both none + both confident | both none (any confidence) | "
          "cases the clause blocks |", "| --- | --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        for gname, ms in GROUPS[:2]:
            strict = anyc = 0
            for s, i in hard_neg:
                recs = [data[(t, m, s)].get(i) for m in ms]
                if any(r is None or r["verdict"] is None for r in recs):
                    continue
                if all(r["verdict"] == "no" for r in recs):
                    anyc += 1
                    if all(r["confident"] is True for r in recs):
                        strict += 1
            L.append(f"| {t} | {gname} | {strict}/{len(hard_neg)} | {anyc}/{len(hard_neg)} "
                     f"| {anyc - strict} |")
    L.append("")

    # ---------------------------------------------------------------- 4. sens/spec
    L += ["## 4. Per-model sensitivity and specificity (corrected labels)", "",
          "Sensitivity = share of truth-`yes` cases the model calls qualifying; "
          "specificity = share of truth-`no` cases it calls `none`. `unclear` counts as "
          "neither, so the two do not sum to 1.", "",
          "| prompt | model | set | sensitivity | specificity | unclear (pos) | unclear (neg) |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        for m in MODELS:
            for s in SETS:
                d = data[(t, m, s)]
                if not d:
                    continue
                pos = [i for i in cases[s]
                       if truth[s].get(i) == "yes" and d.get(i, {}).get("verdict")]
                neg = [i for i in cases[s]
                       if truth[s].get(i) == "no" and d.get(i, {}).get("verdict")]
                L.append(f"| {t} | {SHORT[m]} | {s} "
                         f"| {pct(sum(1 for i in pos if d[i]['verdict'] == 'yes'), len(pos))} "
                         f"| {pct(sum(1 for i in neg if d[i]['verdict'] == 'no'), len(neg))} "
                         f"| {pct(sum(1 for i in pos if d[i]['verdict'] == 'unclear'), len(pos))} "
                         f"| {pct(sum(1 for i in neg if d[i]['verdict'] == 'unclear'), len(neg))} |")
    L.append("")

    L += ["### Schema conformance", "",
          "| prompt | model | set | n | parsed | schema errors | api errors |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        for m in MODELS:
            for s in list(SETS) + ["proxy"]:
                if s == "proxy" and m != FL:
                    continue
                d = proxy[t] if s == "proxy" else data[(t, m, s)]
                if not d:
                    continue
                n = len(d)
                good = sum(1 for r in d.values() if r["verdict"] is not None)
                se = sum(1 for r in d.values() if r["schema_error"])
                ae = sum(1 for r in d.values() if r["error"])
                L.append(f"| {t} | {SHORT[m]} | {s} | {n} | {good} ({good / n:.1%}) "
                         f"| {se} | {ae} |")
    L.append("")
    se_kinds: Counter = Counter()
    for m in MODELS:
        for s in SETS:
            for r in data[("v32", m, s)].values():
                if r["schema_error"]:
                    se_kinds[f"{SHORT[m]}:{r['schema_error']}"] += 1
    for r in proxy["v32"].values():
        if r["schema_error"]:
            se_kinds[f"flash-lite:{r['schema_error']}"] += 1
    L.append("v32 schema errors by kind: "
             + (", ".join(f"`{k}` ({n})" for k, n in se_kinds.most_common())
                if se_kinds else "none — every response carried a boolean `confident`."))
    L.append("")

    L += ["### Classification mix per model and set", "",
          "| prompt | model | set | classifications | confidence field |",
          "| --- | --- | --- | --- | --- |"]
    for t in ("v3", "v32"):
        for m in MODELS:
            for s in SETS:
                d = data[(t, m, s)]
                if not d:
                    continue
                L.append(f"| {t} | {SHORT[m]} | {s} "
                         f"| {dict(Counter(r['raw_class'] for r in d.values()))} "
                         f"| {dict(Counter(r['conf'] for r in d.values()))} |")
    L.append("")

    # ---------------------------------------------------------------- 5. flipped 7
    L += ["## 5. The 7 flipped cases — does v32 call them qualifying?", "",
          "These are the cases the instrument-boundary ruling moved from `no` to `yes` "
          "(`truth_flips_v31.md`). The v3.1/v3.2 rule changes exist to make the models call "
          "them qualifying. Cell format: `classification`@`confidence field`.", "",
          "| id | set | " + " | ".join(f"{SHORT[m]} v3 | {SHORT[m]} v32" for m in MODELS)
          + " | G-strict discard v3 -> v32 (FL+GPT / FL+MIN) |",
          "| --- | --- | " + " | ".join("---" for _ in range(2 * len(MODELS) + 1)) + " |"]
    flip_fix: Counter = Counter()
    for i in FLIPPED:
        s = "human" if i.startswith("HU") else "heldout"
        cells = []
        for m in MODELS:
            for t in ("v3", "v32"):
                r = data[(t, m, s)].get(i)
                cells.append(f"{r['raw_class']}@{r['conf']}" if r and r["raw_class"]
                             else "—")
                if t == "v32" and r and r["verdict"] == "yes":
                    flip_fix[SHORT[m]] += 1
        gates = []
        for ms in ((FL, GPT), (FL, MIN)):
            g3 = g_strict([data[("v3", m, s)].get(i) for m in ms])
            g32 = g_strict([data[("v32", m, s)].get(i) for m in ms])
            gates.append(f"{'DISC' if g3 else 'kept'}->{'DISC' if g32 else 'kept'}")
        L.append(f"| {i} | {s} | " + " | ".join(cells) + " | " + " / ".join(gates) + " |")
    L.append("")
    L.append("Per model, how many of the 7 v32 calls are qualifying: "
             + ", ".join(f"{k} {flip_fix.get(k, 0)}/7" for k in SHORT.values()) + ".")
    L.append("")

    # ---------------------------------------------------------------- 6. leak taxonomy
    L += ["## 6. Leak taxonomy re-check (P1-P7)", "",
          "For each pattern from `leak_analysis.md`, how many of its cases each model gets "
          "**right under the corrected labels** — `none` for a case still labelled `no`, "
          "qualifying for one the ruling flipped to `yes`.", "",
          "| pattern | n | corrected no / yes | "
          + " | ".join(f"{SHORT[m]} v3 | {SHORT[m]} v32" for m in MODELS) + " |",
          "| --- | --- | --- | " + " | ".join("---" for _ in range(2 * len(MODELS))) + " |"]
    for pname, ids in PATTERNS.items():
        n_no = n_yes = 0
        counts: Counter = Counter()
        for i in ids:
            s = "human" if i.startswith("HU") else "heldout"
            tv = truth[s].get(i)
            if tv == "yes":
                n_yes += 1
            else:
                n_no += 1
            for m in MODELS:
                for t in ("v3", "v32"):
                    r = data[(t, m, s)].get(i)
                    if r and r["verdict"] == ("yes" if tv == "yes" else "no"):
                        counts[(t, m)] += 1
        L.append(f"| {pname} | {len(ids)} | {n_no} / {n_yes} | "
                 + " | ".join(f"{counts.get((t, m), 0)}/{len(ids)}"
                              for m in MODELS for t in ("v3", "v32")) + " |")
    L.append("")
    L.append("Restricted to the cases still labelled `no` (the false-discard-leak question "
             "proper — how often the model now says `none`):")
    L.append("")
    L += ["| pattern | still-`no` cases | "
          + " | ".join(f"{SHORT[m]} v3 | {SHORT[m]} v32" for m in MODELS) + " |",
          "| --- | --- | " + " | ".join("---" for _ in range(2 * len(MODELS))) + " |"]
    for pname, ids in PATTERNS.items():
        keep = [i for i in ids
                if truth["human" if i.startswith("HU") else "heldout"].get(i) == "no"]
        if not keep:
            L.append(f"| {pname} | 0 | " + " | ".join("n/a" for _ in range(6)) + " |")
            continue
        c: Counter = Counter()
        for i in keep:
            s = "human" if i.startswith("HU") else "heldout"
            for m in MODELS:
                for t in ("v3", "v32"):
                    r = data[(t, m, s)].get(i)
                    if r and r["verdict"] == "no":
                        c[(t, m)] += 1
        L.append(f"| {pname} | {len(keep)} | "
                 + " | ".join(f"{c.get((t, m), 0)}/{len(keep)}"
                              for m in MODELS for t in ("v3", "v32")) + " |")
    L.append("")
    L.append("And how often the still-`no` cases of each pattern are actually **discarded** "
             "by the two pairs under G-strict (the number that matters for throughput):")
    L.append("")
    L += ["| pattern | still-`no` cases | v3 FL+GPT | v32 FL+GPT | v3 FL+MIN | v32 FL+MIN |",
          "| --- | --- | --- | --- | --- | --- |"]
    for pname, ids in PATTERNS.items():
        keep = [i for i in ids
                if truth["human" if i.startswith("HU") else "heldout"].get(i) == "no"]
        if not keep:
            L.append(f"| {pname} | 0 | n/a | n/a | n/a | n/a |")
            continue
        vals = []
        for ms in ((FL, GPT), (FL, MIN)):
            for t in ("v3", "v32"):
                n = sum(1 for i in keep
                        if g_strict([data[(t, m, "human" if i.startswith("HU")
                                           else "heldout")].get(i) for m in ms]))
                vals.append((t, ms, n))
        L.append(f"| {pname} | {len(keep)} | "
                 + " | ".join(f"{n}/{len(keep)}" for _, _, n in
                              [vals[0], vals[1], vals[2], vals[3]]) + " |")
    L.append("")

    # ---------------------------------------------------------------- 7. tokens
    L += ["## 7. Token usage and estimated spend", "",
          "Counts read from the API responses of the v32 runs made here; the v3 columns are "
          "the same measurement from the earlier runs. Both include the 150-case proxy pool "
          "for flash-lite.", "",
          "| provider | model | v3 in | v3 out | v32 in | v32 out | v32 est. cost |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    tot = 0.0
    t32_in = t32_out = 0
    for m in MODELS:
        u = {}
        for t in ("v3", "v32"):
            ti = to = 0
            for s in SETS:
                for r in data[(t, m, s)].values():
                    ti += r["usage"].get("in", 0)
                    to += r["usage"].get("out", 0)
            if m == FL:
                for r in proxy[t].values():
                    ti += r["usage"].get("in", 0)
                    to += r["usage"].get("out", 0)
            u[t] = (ti, to)
        cost = u["v32"][0] * PRICE[m][0] + u["v32"][1] * PRICE[m][1]
        tot += cost
        t32_in += u["v32"][0]
        t32_out += u["v32"][1]
        L.append(f"| {PROV[m]} | {SHORT[m]} | {u['v3'][0]:,} | {u['v3'][1]:,} "
                 f"| {u['v32'][0]:,} | {u['v32'][1]:,} | ${cost:.2f} |")
    L.append(f"| **total** | | | | **{t32_in:,}** | **{t32_out:,}** | **${tot:.2f}** |")
    L.append("")
    L.append("Prices are the OpenRouter catalogue rates read on 2026-08-01 (flash-lite "
             "$0.30/$2.50, gpt-5.4-mini $0.75/$4.50, ministral-14b $0.20/$0.20 per million "
             "prompt/completion tokens); the Gemini and OpenAI calls went to those providers "
             "directly, so their cost is an estimate.")
    L.append("")

    (HERE / "report_v32.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
