"""Score the live (old) and sandbox (new) picks of the 150-work pilot against the blinded
truth coding in truth/answers/. A pick is matched to the coder's @key through the offered
list's citation line (normalised title prefix), or by DOI for not_on_list originals."""
import json, re, unicodedata
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
_KEY = re.compile(r"^(@\S+)\s+.*?\(\d{4}[a-z]?\)\.\s*(.*)$")


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def split(v):
    return [x.strip() for x in str(v or "").split(" | ") if x.strip() and x.strip() != "nan"]


rows = []
pilot = pd.read_csv(HERE / "pilot_150.csv", dtype=str, keep_default_na=False)
for _, p in pilot.iterrows():
    a = json.load(open(HERE / "truth/answers" / f"{p.work_id}.json"))
    lines = (HERE / "truth/packets" / f"{p.work_id}.md").read_text().splitlines()
    titles = {}
    for ln in lines:
        m = _KEY.match(ln.strip())
        if m:
            titles[m.group(1)] = norm(m.group(2))
    truth_titles, truth_dois = [], []
    for o in a.get("originals") or []:
        if o.get("key") in titles:
            truth_titles.append(titles[o["key"]])
        if o.get("doi"):
            truth_dois.append(o["doi"].lower())
        if not o.get("on_list"):
            truth_titles.append(norm(o.get("citation")))

    def hits(title, doi):
        t = norm(title)[:40]
        return bool(t) and any(t in tt or tt[:40] in t for tt in truth_titles if tt) \
            or (doi and doi.lower() in truth_dois)

    def score(dois, titles_):
        ts, ds = split(titles_), split(dois)
        if not ts and not ds:
            return "none"
        n = max(len(ts), len(ds))
        ok = [hits(ts[i] if i < len(ts) else "", ds[i] if i < len(ds) else "") for i in range(n)]
        if a.get("not_a_replication"):
            return "wrong(not_a_replication)"
        return "right" if all(ok) else ("partial" if any(ok) else "wrong")

    rows.append(dict(work_id=p.work_id, truth_conf=a.get("confidence"),
                     truth_keys=" ".join(o.get("key") or "not_on_list" for o in a.get("originals") or []),
                     not_a_replication=a.get("not_a_replication"),
                     old=score(p.old_doi_o, p.old_title_o), new=score(p.new_doi_o, p.new_title_o),
                     new_method=p.new_link_method, match_change=p.match_change,
                     old_title=p.old_title_o[:80], new_title=p.new_title_o[:80]))
out = pd.DataFrame(rows)
out.to_csv(HERE / "truth_scored.csv", index=False)
print(pd.crosstab(out.old, out.new, margins=True))
print(out[out.old != out.new][["work_id", "old", "new", "new_method", "truth_keys", "old_title", "new_title"]].to_string())
