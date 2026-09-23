"""Write out/reopen_cbd_only.txt: works with a cannot_be_determined row in
data/extracted.csv whose doi_r is NOT past `unvalidated` in the Supabase validation
tables (read-only). Feed it to `--only` beside `--redo-status outcome=cannot_be_determined`."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import shared.config  # noqa: F401  (loads .env before supabase_client reads SUPABASE_URL)
import pandas as pd
from shared import supabase_client as sb
from shared.utils import clean_doi

d = pd.read_csv("data/extracted.csv", dtype=str, keep_default_na=False)
cbd = d[d.outcome == "cannot_be_determined"]
st = pd.DataFrame(sb._get("unvalidated", {"select": "doi_r,validation_status"}))
st["doi_r"] = st.doi_r.map(lambda x: clean_doi(x or ""))
touched = set(st[st.validation_status != "unvalidated"].doi_r) - {""}
val = set(pd.DataFrame(sb._get("validated", {"select": "doi_r"})).doi_r.map(lambda x: clean_doi(x or ""))) - {""}
keep = cbd[~cbd.doi_r.map(clean_doi).isin(touched | val)]
ids = sorted({int(w.rsplit("W", 1)[-1]) for w in keep.openalex_id_r if w.strip()})
no_id = keep[keep.openalex_id_r.str.strip() == ""]
Path("analysis/cbd_investigation/out/reopen_cbd_only.txt").write_text(",".join(map(str, ids)))
print(f"cbd rows {len(cbd)} (works {cbd.openalex_id_r.nunique()}); in validation past unvalidated: "
      f"{len(cbd) - len(keep)} rows; reopen list {len(ids)} works; rows without a work id {len(no_id)}")
print(st.validation_status.value_counts().to_dict())
