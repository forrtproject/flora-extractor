"""What the full-text rung could have sent vs what it did send, per row with a document.

For each row: the best parse's raw text, where the references start, whether a
discussion heading was found (outcome_text provenance), whether a Results heading
exists, and — given quotes — where in the text each quote sits.
"""
import re
import sys
from shared.config import PARSE_CACHE_DIR
from shared.pdf_parsing import (read_parse_cache, best_parse_result, outcome_text,
                                _references_start)
from shared.prompts import TARGET_DISCUSSION_CHARS

RESULTS_HEADER = re.compile(r"(?im)^[^\S\n]{0,8}(?:\d+[.\s]{1,3})?(?:\w+\s+){0,2}results?\b[^\n]{0,60}$")


def n(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def probe(cache_id: str, quotes=()) -> dict:
    res = read_parse_cache(cache_id, PARSE_CACHE_DIR)
    if not res:
        return {"parse": "miss"}
    best = best_parse_result(res) or {}
    raw = str(best.get("raw_text") or "")
    if not raw:
        return {"parse": "no_raw", "method": best.get("source")}
    refs = _references_start(raw)
    disc, prov = outcome_text(raw, max_chars=TARGET_DISCUSSION_CHARS)
    out = {"parse": "ok", "method": best.get("source"), "raw_len": len(raw),
           "refs_at": round(refs / len(raw), 2), "prov": prov,
           "has_results_hdr": bool(RESULTS_HEADER.search(raw)),
           "intro_len": len(best.get("intro") or "")}
    rn, dn = n(raw), n(disc)
    for i, q in enumerate(quotes):
        q = n(q.split("...")[0])[:60]
        if not q:
            continue
        pos = rn.find(q)
        out[f"q{i}_in_raw"] = round(pos / len(rn), 2) if pos >= 0 else None
        out[f"q{i}_in_sent"] = q in dn
    return out


if __name__ == "__main__":
    print(probe(sys.argv[1], sys.argv[2:]))
