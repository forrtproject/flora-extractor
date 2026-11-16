"""Which evaluation cases Stage 2 discards BEFORE the screen ever sees them.

The 2,186-case set scores the screen in isolation, but in production the rule book's
discard specs run first (precedence above admission). This applies the live record-about-
a-paper specs on origin/main (2026-11-06) to each case's DOI, title, abstract and OpenAlex
type (`openalex_types.json`, fetched 2026-11-06), the way filter/engine/backends.py does:
case-insensitive regex search, DOI lower-cased. Copied, not imported, because the local
checkout predates #222/#223 and the engine matches pyarrow batches, not dicts.
shortcut: hand-copied regexes; re-copy if those five specs change.
"""
import json, re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPECS = {  # spec id -> (field, regex); a case matching any is discarded upstream
    "not-a-paper-doi": [("doi", r"/reviews/|/decisions/"), ("doi", r"\.suppl$"),
                        ("doi", r"^10\.24072/.*\.(?:rev|d)\d+$"), ("doi", r"^10\.24072/pci\.[a-z]+\.\d+(?:\.ar\d+)?$")],
    "not-a-paper-title": [("title", r"^\s*(?:(?:review for|decision letter for|peer review #|faculty opinions recommendation of"
                                    r"|correction to|author response)\b|(?:review of|recommendation of|decision (?:revise|reject|accept))\s*:)")],
    "not-a-paper-abstract": [("abstract", r"^\s*a recommendation of\s*:")],
    "osf-registration-protocol": [],  # needs the overlay's "OSF registration template:" text, absent from cases.json
    "peer-review-record": [("type", r"^peer-review$")],
}
TYPES = json.loads((HERE / "openalex_types.json").read_text())

def upstream_rule(case: dict) -> str:
    """The first live discard spec the case matches, or ''."""
    doi = (case.get("doi") or "").strip().lower()
    row = {"doi": doi, "title": case.get("title") or "", "abstract": case.get("abstract") or "", "type": TYPES.get(doi, "")}
    for spec, arms in SPECS.items():
        if any(re.search(rx, row[f], re.I) for f, rx in arms):
            return spec
    return ""

if __name__ == "__main__":
    from collections import Counter
    cases = json.loads((HERE.parent / "jev_eval_2026-10" / "cases.json").read_text(encoding="utf-8"))
    hit = {c["id"]: upstream_rule(c) for c in cases}
    print(Counter((c["truth"], c["source"], hit[c["id"]]) for c in cases if hit[c["id"]]))
    assert upstream_rule({"doi": "10.24072/pci.rr.100885.d1", "title": "x"}) == "not-a-paper-doi"
    assert upstream_rule({"doi": "10.1037/a0014475", "title": "Change over time in obedience"}) == ""
