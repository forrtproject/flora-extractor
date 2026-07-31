"""
clean_parse_cache.py — Delete all-empty parse caches from cache/parse/.

Before audit B4 the pipeline ran the six-parser stack for every non-multi row,
including rows that exited at the reference screen and never fetched a PDF, and wrote
the resulting six empty results to cache. Because every reader early-returns on an
existing cache file, those empty caches then stood in for the real ones on any later
run that DID acquire the PDF — the paper stayed pinned to abstract-only outcome
coding. The writers no longer produce them; this removes the ones already on disk.

    python -m extract.clean_parse_cache            # dry run: count and report
    python -m extract.clean_parse_cache --apply    # delete them
"""
from __future__ import annotations

import argparse
import json

from shared.config import PARSE_CACHE_DIR
from shared.pdf_parsing import parse_result_is_empty


def clean_parse_cache(apply: bool = False) -> dict:
    """Report (and with apply=True delete) every all-empty parse cache file."""
    files = sorted(PARSE_CACHE_DIR.glob("parse_*.json"))
    empty, unreadable = [], []
    for path in files:
        try:
            with path.open(encoding="utf-8") as fh:
                results = json.load(fh)
        except Exception:
            unreadable.append(path)
            continue
        if parse_result_is_empty(results):
            empty.append(path)

    deleted = 0
    if apply:
        for path in empty:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass

    print(f"parse caches scanned: {len(files)}")
    print(f"  all-empty:   {len(empty)}")
    print(f"  unreadable:  {len(unreadable)} (left alone)")
    print(f"  deleted:     {deleted}" if apply else "  (dry run — pass --apply to delete)")
    return {"scanned": len(files), "empty": len(empty),
            "unreadable": len(unreadable), "deleted": deleted}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Delete all-empty parse caches.")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete (default: dry run).")
    a = p.parse_args()
    clean_parse_cache(apply=a.apply)
