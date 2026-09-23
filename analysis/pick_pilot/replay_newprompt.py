"""Re-score the reference-list pick against the 2026-09-23 target prompt (handover step 3).

`analysis/mo_observatory/adjudication/replay_pick.py` replays the cached prompt TEXT
verbatim, so it can only vary the model. The cached prompts hold the rendered evidence
but not the candidate/reference records they were built from, so the new prompt is made
from the old text by the two edits the builder change makes, and nothing else:

1. the task block: the pre-edit `_TARGET_TASK` (read from git HEAD's shared/prompts.py
   by default, `--base-rev`) is replaced by the current one;
2. the key suffix: `@abrahams1999b` → `@abrahams1999_2` (a letter suffix is recognised
   only where the unsuffixed key was printed earlier in the same prompt, which is how
   `assign_target_keys` issues them).

`self_check()` proves the rewrite is exact: it renders synthetic entries with a
colliding author-year under HEAD's builders and key assignment, rewrites, and asserts the
result equals what the CURRENT builders render — for both vocabularies and with and
without closing sections. A prompt that does not contain HEAD's task block verbatim
(13 of 229, bought under the gpt-5.4-mini-era block) has its whole task block swapped
instead (`rewrite_older`), and is flagged `rewrite = older_block`.

    .venv/bin/python -m analysis.pick_pilot.replay_newprompt --model gpt-6-luna
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication import replay_pick as rp
from shared import prompts as new_prompts
from shared import target_keys as new_keys
from shared.config import LINKING_EFFORT
from shared.llm_client import call_model

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
_KEYLINE = re.compile(r"^(@[a-z0-9_]+)\s+(.*)$", re.M)
_OLD_KEYLINE = re.compile(r"^(@[a-z0-9]+)\s", re.M)
_PILOT_START = datetime(2026, 9, 23, 14, 49, tzinfo=timezone.utc).timestamp()


def _load_rev(rev: str, rel: str, name: str):
    """Import *rel* as it was at *rev*, as a module of the `shared` package."""
    src = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    spec = importlib.util.spec_from_loader(f"shared.{name}", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "shared"
    sys.modules[spec.name] = mod
    exec(compile(src, f"<{rev}:{rel}>", "exec"), mod.__dict__)
    return mod


def rekey(prompt: str) -> tuple[str, dict[str, str]]:
    """Rename letter-suffixed keys to `_n`; returns (prompt, old → new)."""
    seen: set[str] = set()
    rename: dict[str, str] = {}
    for key in _OLD_KEYLINE.findall(prompt):
        base, last = key[:-1], key[-1]
        if base in seen and "b" <= last <= "z":
            rename[key] = f"{base}_{ord(last) - ord('a') + 1}"
        seen.add(key)
    for old, new in rename.items():
        prompt = re.sub(rf"(?<![\w@]){re.escape(old)}(?![a-z0-9_])", new, prompt)
    return prompt, rename


def rewrite(prompt: str, old_task: str) -> tuple[str, dict[str, str]] | None:
    if old_task not in prompt:
        return None
    return rekey(prompt.replace(old_task, new_prompts._TARGET_TASK, 1))


def rewrite_older(prompt: str) -> tuple[str, dict[str, str]] | None:
    """For a prompt bought under an EARLIER task block (the gpt-5.4-mini era, before the
    background-citation bullet): swap the whole block, located by its first line and
    the response-format header. The rest of such a prompt stays as it was sent, so
    these rows are flagged `rewrite = older_block` and counted separately."""
    head = "This paper has been classified as a replication or reproduction."
    i, j = prompt.find(head), prompt.find("\n\nRESPONSE FORMAT")
    if i < 0 or j < i:
        return None
    return rekey(prompt[:i] + new_prompts._TARGET_TASK + prompt[j:])


def self_check(old_p, old_k) -> None:
    cands = [{"doi": "10.1/a", "title": "First", "year": 1999, "authors": ["Abrahams, A."]}]
    refs = [{"doi": "10.1/b", "title": "Second", "year": 1999, "authors": ["Abrahams, A."]},
            {"doi": "10.1/c", "title": "Third", "year": 1999, "authors": ["Abrahams, A."]},
            {"doi": "10.1/d", "title": "Other", "year": 2001, "authors": ["Mauer, B."]}]
    old_entries, _ = old_k.assign_target_keys(cands, refs)
    new_entries, _ = new_keys.assign_target_keys(cands, refs)
    assert [e["key"] for e in new_entries] == ["@abrahams1999", "@abrahams1999_2",
                                               "@abrahams1999_3", "@mauer2001"]
    for name in ("build_target_outcome_prompt", "build_repro_target_outcome_prompt"):
        for kw in ({}, {"discussion": "we found it", "intro": "intro"}):
            old = getattr(old_p, name)("T", "abstract, cf. @abrahams1999b", old_entries, **kw)
            new = getattr(new_prompts, name)("T", "abstract, cf. @abrahams1999_2",
                                             new_entries, **kw)
            got = rewrite(old, old_p._TARGET_TASK)
            assert got is not None and got[0] == new, f"rewrite differs for {name} {kw}"


def ask(prompt: str, model: str, rep: int = 0) -> dict:
    h = hashlib.sha256(f"{model}|{LINKING_EFFORT}|{prompt}".encode()).hexdigest()[:24]
    p = HERE / "out" / ("replay" if rep == 0 else f"replay_rep{rep}") / f"{h}.json"
    if p.exists():
        return json.loads(p.read_text())
    result, _prov, err = call_model(prompt, model, reasoning_effort=LINKING_EFFORT)
    if not result:
        return {"_error": err}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result))
    return result


def mo_key(r) -> str:
    """replay_pick.mo_key over the rewritten prompt (its key pattern has no `_`)."""
    lines = _KEYLINE.findall(r.prompt)
    for doi in r.mo_dois or []:
        if doi and doi.lower() in r.prompt.lower():
            for k, t in lines:
                if doi.lower() in t.lower():
                    return k
    for title in r.mo_titles or []:
        nt = rp._norm(title)[:50]
        if len(nt) > 15:
            for k, t in lines:
                if nt in rp._norm(t):
                    return k
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--base-rev", default="HEAD",
                    help="the git revision whose prompts the cached answers were bought under")
    ap.add_argument("--rep", type=int, default=0,
                    help="an independent repeat: same prompts, a separate answer cache, "
                         "to measure run-to-run noise (0 = the primary run)")
    ap.add_argument("--variant", choices=("new", "old"), default="new",
                    help="old = replay the cached prompt unchanged (a noise baseline)")
    ap.add_argument("--only-set", default="", help="restrict to judged | control")
    ap.add_argument("--only-label", default="", help="restrict to one label, e.g. mo")
    a = ap.parse_args()
    old_p = _load_rev(a.base_rev, "shared/prompts.py", "_prompts_base")
    old_k = _load_rev(a.base_rev, "shared/target_keys.py", "_target_keys_base")
    assert old_p._TARGET_TASK != new_prompts._TARGET_TASK, "no task edit to replay"
    self_check(old_p, old_k)
    print("self-check: rewritten HEAD prompts equal the current builders' output")

    # Only prompts bought BEFORE the pilot: the sandbox run (started 2026-09-23 15:49
    # BST) wrote targetoutcome entries under the new prompt, and attach_prompts would
    # otherwise pick some of those up as the "old" prompt of a case.
    real_glob = rp.glob.glob
    rp.glob.glob = lambda pat: [f for f in real_glob(pat)
                                if Path(f).stat().st_mtime < _PILOT_START]
    try:
        c = rp.attach_prompts(rp.cases())
    finally:
        rp.glob.glob = real_glob
    if a.only_set:
        c = c[c.set == a.only_set].reset_index(drop=True)
    if a.only_label:
        c = c[c.label == a.only_label].reset_index(drop=True)
    exact = [rewrite(p, old_p._TARGET_TASK) for p in c.prompt]
    c["rewrite"] = ["exact" if x is not None else "older_block" for x in exact]
    rewritten = [x if x is not None else rewrite_older(p) for x, p in zip(exact, c.prompt)]
    c["rewritable"] = [x is not None for x in rewritten]
    print(f"cached prompts: {len(c)}; carry the pre-edit task block verbatim: "
          f"{(c.rewrite == 'exact').sum()}; older block swapped by markers: "
          f"{((c.rewrite == 'older_block') & c.rewritable).sum()}; not rewritable: "
          f"{(~c.rewritable).sum()}")
    c = c[c.rewritable].reset_index(drop=True)
    rewritten = [x for x in rewritten if x is not None]
    if a.variant == "new":
        c["prompt"] = [x[0] for x in rewritten]
        c["renamed_keys"] = [len(x[1]) for x in rewritten]
        c["old_key"] = [x[1].get(k, k) for x, k in zip(rewritten, c.old_key)]
    else:
        c["renamed_keys"] = 0
    c["mo_key"] = c.apply(mo_key, axis=1)
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(lambda p: ask(p, a.model, a.rep), c.prompt))
    c["error"] = [x.get("_error", "") for x in res]
    c["new_keys"] = [rp.certain_keys(x) for x in res]
    c["new_reasoning"] = [str(x.get("reasoning", ""))[:300] for x in res]
    c["result"] = c.apply(rp.classify, axis=1)
    print(f"model {a.model} @ {LINKING_EFFORT}; replayed: {len(c)}; prompts with a "
          f"renamed key: {(c.renamed_keys > 0).sum()}")
    print(f"MO's original located in the offered list: {(c.mo_key != '').sum()} / {len(c)}")
    print(pd.crosstab([c.set, c.label], c.result, margins=True).to_string())
    name = "replay_newprompt" if (a.variant, a.rep) == ("new", 0) else \
        f"replay_{a.variant}prompt_rep{a.rep}"
    c.drop(columns=["prompt"]).to_csv(HERE / f"{name}.csv", index=False,
                                      encoding="utf-8-sig")


if __name__ == "__main__":
    main()
