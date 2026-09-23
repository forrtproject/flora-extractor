"""Does an OpenAlex record describe the paper its DOI is registered to? — issue #210.

`similarity(oa_title, registry)` compares the OpenAlex title with every title the
registry holds for the DOI (main title, main title + subtitle, original-language
title, short title) and returns the best score, so a subtitle dropped on one side
or a translated title the registry also carries does not read as a mismatch.

The score is token OVERLAP COEFFICIENT (|A ∩ B| / min(|A|, |B|)) over normalised
content tokens, not Jaccard: a preprint titled "X" and the article titled
"X: evidence from three experiments" share every token of the shorter one, which
the overlap coefficient scores 1.0 and Jaccard penalises. Twins share nothing —
all 30 known twins score 0.0.

`year_gap` is the smallest distance between the OpenAlex year and any registry
publication date (print, online, issued; `created` — the DOI's registration — only
when the registry states nothing else): a preprint year vs an article year, or an
online-first vs print year, is then not a gap.
"""

import html
import re
import unicodedata
from typing import Optional

_TAG = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset("""a an and the of in on for to with by from at as is are was were be
    or its their our we vs versus into than that this these those via""".split())


def tokens(title: str) -> set[str]:
    text = html.unescape(_TAG.sub(" ", title or ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return {t for t in _TOKEN.findall(text) if t not in _STOP and (len(t) > 1 or not t.isascii())}


def registry_titles(meta: dict) -> list[str]:
    mains = list(meta.get("titles") or [])
    subs = list(meta.get("subtitles") or [])
    out = mains + [f"{m} {s}" for m in mains for s in subs]
    out += list(meta.get("original_titles") or []) + list(meta.get("short_titles") or [])
    return [t for t in out if t]


def overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return float("nan")
    return len(a & b) / min(len(a), len(b))


def similarity(oa_title: str, meta: dict) -> float:
    """Best overlap coefficient against any registry title; NaN when either side is empty."""
    a = tokens(oa_title)
    scores = [overlap(a, tokens(t)) for t in registry_titles(meta)]
    scores = [s for s in scores if s == s]
    return max(scores) if scores else float("nan")


def year_gap(oa_year, meta: dict) -> Optional[int]:
    dates = meta.get("years") or {}
    # `created` is when the DOI was REGISTERED — a 1985 article deposited in 2002 has
    # created=2002 — so it counts only when the registry states no publication date.
    years = [y for k, y in dates.items() if y and k != "created"] or \
        [y for y in dates.values() if y]
    try:
        y0 = int(oa_year)
    except (TypeError, ValueError):
        return None
    return min(abs(y0 - y) for y in years) if years else None


def latin_share(title: str) -> float:
    letters = [c for c in title or "" if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if "LATIN" in unicodedata.name(c, "")) / len(letters)
