"""Blinded ground-truth packets for the llm_references pick pilot.

For each work in `sample_150.txt`: the replication paper's metadata and abstract, the
keyed reference/candidate list exactly as the linking model saw it (from the cached
`targetoutcome_*` prompt whose resolution_method is llm_references), and its full text
when the parse cache (or a cached PDF, parsed locally with pdfminer) holds it. Nothing
that reveals the pipeline's pick (doi_o, title_o, link_evidence, outcome, the model's
answer) is written.

    .venv/bin/python -m analysis.pick_pilot.truth.build_packets
"""
from __future__ import annotations

import csv
import glob
import json
import re
import unicodedata
from pathlib import Path

import pandas as pd

from shared.config import PARSE_CACHE_DIR
from shared.pdf_parsing import best_parse_result, read_parse_cache
from shared.pdf_sources import pdf_cache_path
from shared.utils import cache_key

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PACKETS = HERE / "packets"
PACKET_CAP = 60_000
_KEYLINE = re.compile(r"^@[A-Za-z0-9]+\s")


def _norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _people(s: str) -> str:
    try:
        v = json.loads(s)
        return "; ".join(f"{a.get('given', '')} {a.get('family', '')}".strip() if isinstance(a, dict) else str(a)
                         for a in v)
    except (ValueError, TypeError, AttributeError):
        return s


def offered_list(prompt: str) -> tuple[str, int]:
    """The keyed lists and their headers, from the first list header after the abstract."""
    lines = prompt.split("\n")
    start = next((i for i, l in enumerate(lines) if _KEYLINE.match(l)), None)
    if start is None:
        return "", 0
    # back up to the header that introduces the first list
    while start > 0 and lines[start - 1].strip() and not lines[start - 1].startswith(("ABSTRACT:", "TITLE:")):
        start -= 1
    out, n = [], 0
    for l in lines[start:]:
        if _KEYLINE.match(l):
            out.append(l)
            n += 1
        elif not l.strip():
            out.append("")
        elif re.match(r"^[A-Z][A-Z ,\-/()'&]+:?.*$", l) and (l.rstrip().endswith(":") or l.isupper()):
            out.append(l)  # a list header
        else:
            break  # anything else is prompt text after the lists
    while out and (not out[-1].strip() or not _KEYLINE.match(out[-1])):
        out.pop()
    return "\n".join(out), n


def _dois(d: dict) -> set[str]:
    out = {str(d.get("resolved_doi_o") or "").lower()}
    for t in d.get("targets") or []:
        if isinstance(t, dict) and t.get("match_certain"):
            out.add(str((t.get("record") or {}).get("doi") or "").lower())
    return out - {""}


def load_prompts(rows: pd.DataFrame) -> dict[str, dict]:
    """work_id -> the chosen cached llm_references entry for that paper."""
    titles = {r.oa_work_id_r: _norm(r.title_r)[:60] for r in rows.drop_duplicates("oa_work_id_r").itertuples()}
    picks = rows.groupby("oa_work_id_r").doi_o.apply(lambda s: {str(x).lower() for x in s if str(x)}).to_dict()
    found: dict[str, list] = {}
    for f in glob.glob(str(ROOT / "cache/llm/targetoutcome_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if "llm_references" not in (d.get("resolution_method"), d.get("target_stage")) or not d.get("llm_prompt"):
            continue
        m = re.search(r"^TITLE: (.*)$", d["llm_prompt"], re.M)
        pt = _norm(m.group(1) if m else "")
        for w, t in titles.items():
            if t and pt.startswith(t):
                found.setdefault(w, []).append((f, d))
    chosen = {}
    for w, lst in found.items():
        # the entry behind the shipped row: matching resolved doi first, then newest
        lst.sort(key=lambda fd: (bool(_dois(fd[1]) & picks.get(w, set())),
                                 Path(fd[0]).stat().st_mtime), reverse=True)
        chosen[w] = lst[0][1] | {"_n_entries": len(lst)}
    return chosen


def fulltext(ids: list[str]) -> tuple[str, str]:
    for cid in [i for i in ids if i]:
        parsed = None
        try:
            parsed = read_parse_cache(cid)
        except ValueError:
            pass
        if not parsed:  # legacy / other-version parse files for the same identity
            for f in sorted(glob.glob(str(PARSE_CACHE_DIR / f"parse_{cache_key(cid)}*.json")), reverse=True):
                try:
                    parsed = json.load(open(f, encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if isinstance(parsed, dict) and parsed:
                    break
        if isinstance(parsed, dict) and parsed:
            try:
                best = best_parse_result(parsed)
            except Exception:
                best = None
            txt = ((best or {}).get("raw_text") or "").strip()
            if len(txt) > 2000:
                return txt, f"parse cache ({(best or {}).get('source') or (best or {}).get('method') or '?'})"
        p = pdf_cache_path(cid)
        if p.exists() and p.stat().st_size > 1000:
            try:
                from pdfminer.high_level import extract_text
                txt = (extract_text(str(p)) or "").strip()
            except Exception:
                txt = ""
            if len(txt) > 2000:
                return txt, "cached PDF (pdfminer)"
    return "", ""


def main() -> None:
    wids = [w.strip() for w in (HERE.parent / "sample_150.txt").read_text().replace("\n", ",").split(",") if w.strip()]
    ex = pd.read_csv(ROOT / "data/extracted.csv", dtype=str, keep_default_na=False)
    rows = ex[ex.oa_work_id_r.isin(wids)]
    prompts = load_prompts(rows)
    PACKETS.mkdir(parents=True, exist_ok=True)
    manifest = []
    for w in wids:
        r = rows[rows.oa_work_id_r == w]
        if r.empty:
            manifest.append({"work_id": w, "doi_r": "", "has_fulltext": False, "n_offered": 0,
                             "packet": "", "note": "not in extracted.csv"})
            continue
        rep = r.iloc[0]
        d = prompts.get(w, {})
        lst, n = offered_list(d.get("llm_prompt", ""))
        doi = rep.doi_r
        head = [f"# {w}", "",
                "## The replication paper", "",
                f"- OpenAlex work: https://openalex.org/{w}",
                f"- DOI: {doi} (https://doi.org/{doi})" if doi else "- DOI: (none on record)",
                f"- URL: {rep.url_r}" if rep.url_r else "",
                f"- Title: {rep.title_r}", f"- Authors: {_people(rep.authors_r)}",
                f"- Year: {rep.year_r}  ·  Journal: {rep.journal_r}", "",
                "### Abstract", "", rep.abstract_r or "(no abstract on record)", "",
                "## Offered list (the keyed candidates and references, as shown to the linking model)", ""]
        head += ([lst] if lst else ["(no offered list found — name the original(s) by full citation and mark them not_on_list)"])
        head += ["", "## Full text of the replication paper", ""]
        body = "\n".join(l for l in head if l is not None)
        ft, src = fulltext([doi, w, rep.url_r])
        if ft:
            room = max(5_000, PACKET_CAP - len(body) - 300)
            cut = len(ft) > room
            body += (f"(machine-extracted from {src}; {'first ' + format(room, ',') + ' of ' + format(len(ft), ',') + ' characters — intro and methods come first; consult the DOI for the rest' if cut else 'complete'})\n\n"
                     + ft[:room])
        else:
            body += "(not available in this packet — consult the paper via its DOI or URL)"
        path = PACKETS / f"{w}.md"
        path.write_text(body, encoding="utf-8")
        manifest.append({"work_id": w, "doi_r": doi, "has_fulltext": bool(ft), "n_offered": n,
                         "packet": str(path.relative_to(ROOT)),
                         "note": "" if d else "no cached llm_references prompt"})
    m = pd.DataFrame(manifest)
    m.to_csv(HERE / "manifest.csv", index=False, encoding="utf-8-sig")
    print(f"works {len(wids)}; packets {(m.packet != '').sum()}; fulltext {m.has_fulltext.sum()}; "
          f"offered list {(m.n_offered > 0).sum()}; notes {m.note.value_counts().to_dict()}")
    print("multiple cached prompts:", sum(1 for d in prompts.values() if d['_n_entries'] > 1))
    for i in range(10):
        (HERE / f"batch_{i + 1:02d}.txt").write_text("\n".join(wids[i * 15:(i + 1) * 15]) + "\n")


if __name__ == "__main__":
    main()
