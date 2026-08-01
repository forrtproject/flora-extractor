"""Build a blinded adjudication set from the Gemma-vs-Flash-Lite benchmark.

Selects the cases where the models actually differ (those carry all the
information about which model is right), plus agreement controls to detect a
judge that simply says "yes" to everything. Judges never see any model's verdict.
"""
import json
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# Abstracts come from the live LLM cache under cache/llm/, which is gitignored: this is
# the one script here that cannot be re-run from a fresh clone.
RES = json.load(open(HERE / "gemma_eval_google_gemma-4-31b-it.json"))


def title_abstract(cache_name: str):
    d = json.loads((ROOT / "cache/llm" / cache_name).read_text())
    p = d.get("llm_prompt", "")
    t = re.search(r"TITLE:\s*(.*?)\n\nABSTRACT:", p, re.S)
    a = re.search(r"ABSTRACT:\s*(.*?)\n\nRespond with ONLY", p, re.S)
    return (t.group(1).strip() if t else ""), (a.group(1).strip() if a else "")


V = {"yes", "no", "unclear"}
rows = [x for x in RES if x["cand"] in V and x["flash_lite"] in V and x["ref"] in V]

buckets = {
    # The crux: flash-lite's negative bias. Is it wrong, or is gpt-5-mini over-calling?
    "A_flashlite_no_ref_yes":  [x for x in rows if x["flash_lite"] == "no" and x["ref"] == "yes"],
    # Gemma's extra yeses relative to flash-lite.
    "B_gemma_yes_flashlite_no": [x for x in rows if x["cand"] == "yes" and x["flash_lite"] == "no"],
    # Any other three-way split.
    "C_other_disagreement": [x for x in rows
                             if len({x["flash_lite"], x["cand"], x["ref"]}) > 1
                             and not (x["flash_lite"] == "no" and x["ref"] == "yes")
                             and not (x["cand"] == "yes" and x["flash_lite"] == "no")],
}
random.seed(20260729)
buckets["D_control_all_no"] = random.sample(
    [x for x in rows if x["flash_lite"] == x["cand"] == x["ref"] == "no"], 8)
buckets["D_control_all_yes"] = random.sample(
    [x for x in rows if x["flash_lite"] == x["cand"] == x["ref"] == "yes"], 6)

cases, seen = [], set()
for bucket, items in buckets.items():
    for x in items:
        if x["file"] in seen:
            continue
        seen.add(x["file"])
        t, a = title_abstract(x["file"])
        if not a or a == "(not available)":
            continue
        cases.append({"id": f"C{len(cases)+1:03d}", "bucket": bucket, "file": x["file"],
                      "title": t, "abstract": a[:3000],
                      "_flash_lite": x["flash_lite"], "_gemma": x["cand"], "_ref": x["ref"]})

json.dump(cases, open(HERE / "adjudication_cases.json", "w"), indent=2)
print(f"{len(cases)} cases")
for b in buckets:
    print(f"  {b:<26} {sum(1 for c in cases if c['bucket']==b)}")

# Blinded copy for the judges: no bucket, no model verdicts.
blind = [{"id": c["id"], "title": c["title"], "abstract": c["abstract"]} for c in cases]
random.shuffle(blind)
n = 4
for i in range(n):
    chunk = blind[i::n]
    (HERE / f"adjudication_batch{i+1}.json").write_text(json.dumps(chunk, indent=2))
    print(f"  batch{i+1}: {len(chunk)} cases")
