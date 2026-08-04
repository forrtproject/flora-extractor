"""One page for the whole live bundle: what each rule does, and how right it was.

MEASUREMENT ONLY — no LLM call, no spend, and the DuckDB store is opened
**read-only** so a `route` that is running keeps its write lock and this tool
fails with a message instead of hanging on it.

Today the same question is answered in three places — `filter.engine status` for
pile counts, `analysis/arm_evidence.py` for one candidate pattern's evidence, and
ad-hoc DuckDB for everything else. This is the whole bundle at once, one row per
spec:

1. **What it is** — pile, precedence, shadow, vocabulary, and the `measured`
   levels it claims (`filter/engine/spec.py`).
2. **What it did to this release** — rows it WON (it was the highest-precedence
   non-shadow match: `routing.rule_id`), rows it matched at all
   (`evaluations`), and its share of the release. A shadow rule wins nothing by
   construction, so it gets a *would-win* count instead: works it matched that no
   higher-precedence live rule also matched. That count ignores the no-text
   downgrade, which only the actual winner of a row can suffer.
3. **Known-FLoRA capture** — works of `data/flora.csv` the rule reaches, and
   works no other rule in the bundle reaches (the recall it uniquely carries).
4. **Known negatives** — rows of `data/not_a_replication.csv` it hits, matched on
   that file's own title/abstract columns, so a DOI- or type-based rule scores
   zero here for want of the columns rather than for want of hits.
5. **Screening outcome** — the recorded verdicts (`filter/engine/claims.py`),
   rebuilt into per-work outcomes with the same functions the tiers use, joined
   to the rule that won each work: rows screened, proceed / discard, and the
   observed precision with a Wilson 95% interval. A rule with no verdicts yet
   prints `not screened`, never `0%`.

Plus the release's pile composition and the `pending` split
(`no_filter_matched` vs `no_text`) — the bundle's coverage gap.

**Read the label-derived columns as optimistic**, exactly as `arm_evidence` says:
the cached-screen and CSV-derived columns describe what the OLD filter admitted,
and FLoRA capture is partly circular because much of `flora.csv` was found with
these very phrases. Cells resting on fewer than 15 labelled or screened rows are
marked as decoration.

Usage:
    python -m analysis.rule_report
    python -m analysis.rule_report --html redesign/rule_report.html
    python -m analysis.rule_report --json cache/rule_report.json
"""

import argparse
import html
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from analysis.arm_evidence import (
    DECORATION,
    MIN_LABELLED,
    flora_replication_dois,
    negative_rows,
    newest_release,
)
from filter.engine.backends import _clean_doi_array, eval_spec_rows
from filter.engine.export import ALIASES_FILENAME, SPEC_DIR
from filter.engine.spec import FilterSpec, bundle_hash, load_specs
from filter.engine.store import DEFAULT_STORE_PATH
from filter.engine.workids import load_aliases, resolve, work_id
from shared.config import DATA_DIR, SNAPSHOT_POOL_DIR

# The tiers whose verdicts this report can read back (filter/engine/tiers.py).
TIERS = ("screen_expensive", "screen_cheap")

Z95 = 1.959963984540054


class StoreUnavailable(RuntimeError):
    """The routing store cannot be opened read-only — usually a running `route`."""


# ---------------------------------------------------------------------------
# The store, read-only
# ---------------------------------------------------------------------------


def open_readonly(path: Path) -> duckdb.DuckDBPyConnection:
    """*path* as a read-only connection, or a clear refusal.

    Read-only matters twice: this tool must never mint tables in the store it is
    reporting on, and a `route` in progress holds an exclusive lock — the useful
    answer then is "come back when it finishes", not a hang or a stack trace.
    """
    if not Path(path).exists():
        raise StoreUnavailable(
            f"no routing store at {path} — run `python -m filter.engine route` first.")
    try:
        return duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        raise StoreUnavailable(
            f"cannot open {path} read-only: {exc}\n"
            "A `python -m filter.engine route` is probably still running and holds "
            "the write lock. Wait for it to finish (`pgrep -f 'filter.engine route'`) "
            "and re-run.") from exc


def _release(con: duckdb.DuckDBPyConnection, given: Optional[str]) -> str:
    release = given or newest_release(con)
    if release is None:
        raise StoreUnavailable("the store holds no routing release yet.")
    return release


# ---------------------------------------------------------------------------
# Routing outcome
# ---------------------------------------------------------------------------


def _register_specs(con: duckdb.DuckDBPyConnection, specs: list[FilterSpec]) -> None:
    """A temp table of spec metadata, so the would-win query is one statement.

    Temp tables live in the temp catalog, not in the read-only database file.
    """
    con.execute("CREATE TEMP TABLE specmeta "
                "(spec_id TEXT, precedence INT, shadow BOOLEAN)")
    con.executemany("INSERT INTO specmeta VALUES (?, ?, ?)",
                    [(s.id, s.precedence, s.shadow) for s in specs])


def routing_outcome(con: duckdb.DuckDBPyConnection, release: str) -> dict:
    """Pile counts, pending reasons, and per-rule won / no-text counts."""
    piles = dict(con.execute(
        "SELECT pile, count(*) FROM routing WHERE release_id = ? GROUP BY 1 "
        "ORDER BY 1", [release]).fetchall())
    pending = dict(con.execute(
        "SELECT nullif(pending_reason, ''), count(*) FROM routing "
        "WHERE release_id = ? AND pile = 'pending' GROUP BY 1 ORDER BY 1",
        [release]).fetchall())
    won = dict(con.execute(
        "SELECT rule_id, count(*) FROM routing WHERE release_id = ? AND rule_id <> '' "
        "GROUP BY 1", [release]).fetchall())
    no_text = dict(con.execute(
        "SELECT rule_id, count(*) FROM routing WHERE release_id = ? AND rule_id <> '' "
        "AND pending_reason = 'no_text' GROUP BY 1", [release]).fetchall())
    return {"piles": piles, "pending": pending, "won": won, "no_text": no_text}


def matched_counts(con: duckdb.DuckDBPyConnection, release: str) -> dict[str, int]:
    """Works each spec matched, shadow included — distinct by alias-resolved work."""
    return dict(con.execute(
        "SELECT spec_id, count(DISTINCT work_id) FROM evaluations "
        "WHERE release_id = ? GROUP BY 1", [release]).fetchall())


def would_win(con: duckdb.DuckDBPyConnection, release: str) -> dict[str, int]:
    """Per shadow spec: works it matched that no higher-precedence live rule did.

    The counterfactual a shadow rule is carried for — "if this were live, what
    would it claim". Ties break on spec id ascending, the same order
    `load_specs()` sorts by and therefore the same order `route_batch()` resolves
    a tie in. The no-text downgrade is not modelled: it applies to whichever rule
    actually won a row, and this rule did not.
    """
    rows = con.execute("""
        SELECT e.spec_id, count(DISTINCT e.work_id)
        FROM evaluations e JOIN specmeta s ON s.spec_id = e.spec_id
        WHERE e.release_id = ? AND s.shadow AND NOT EXISTS (
            SELECT 1 FROM evaluations o JOIN specmeta t ON t.spec_id = o.spec_id
            WHERE o.release_id = e.release_id AND o.work_id = e.work_id
              AND NOT t.shadow
              AND (t.precedence > s.precedence
                   OR (t.precedence = s.precedence AND t.spec_id < s.spec_id)))
        GROUP BY 1""", [release]).fetchall()
    return dict(rows)


def rule_winner(con: duckdb.DuckDBPyConnection, release: str,
                work_ids: list[int]) -> dict[int, str]:
    """`{work_id: winning rule_id}` for *work_ids* — the join key for verdicts."""
    if not work_ids:
        return {}
    con.execute("CREATE TEMP TABLE wanted (work_id BIGINT)")
    con.executemany("INSERT INTO wanted VALUES (?)", [(int(w),) for w in work_ids])
    rows = con.execute(
        "SELECT r.work_id, r.rule_id, r.pile FROM routing r JOIN wanted w "
        "ON w.work_id = r.work_id WHERE r.release_id = ?", [release]).fetchall()
    con.execute("DROP TABLE wanted")
    return {int(wid): rule for wid, rule, _pile in rows}


def spec_hits(con: duckdb.DuckDBPyConnection, release: str,
              work_ids: list[int]) -> dict[int, set[str]]:
    """`{work_id: {spec ids that matched it}}` for *work_ids* — FLoRA capture."""
    if not work_ids:
        return {}
    con.execute("CREATE TEMP TABLE wanted_e (work_id BIGINT)")
    con.executemany("INSERT INTO wanted_e VALUES (?)", [(int(w),) for w in work_ids])
    rows = con.execute(
        "SELECT e.work_id, e.spec_id FROM evaluations e JOIN wanted_e w "
        "ON w.work_id = e.work_id WHERE e.release_id = ?", [release]).fetchall()
    con.execute("DROP TABLE wanted_e")
    hits: dict[int, set[str]] = {}
    for wid, spec_id in rows:
        hits.setdefault(int(wid), set()).add(spec_id)
    return hits


# ---------------------------------------------------------------------------
# FLoRA work ids
# ---------------------------------------------------------------------------


def _scan_ids(path: Path, flora: pa.Array, aliases: dict[int, int]) -> list[int]:
    table = pq.read_table(path, columns=["id", "doi"])
    if not table.num_rows:
        return []
    doi = _clean_doi_array(table.column("doi").combine_chunks())
    keep = pc.fill_null(pc.is_in(doi, value_set=flora), False)
    if not pc.any(keep).as_py():
        return []
    ids = table.filter(keep).column("id").to_pylist()
    return [resolve(work_id(value), aliases) for value in ids]


def flora_work_ids(pool_dir: Path, dois: set[str], aliases: dict[int, int],
                   workers: int) -> set[int]:
    """Alias-resolved work ids of the FLoRA DOIs present in the pool.

    Two columns, no regex: `arm_evidence.scan_pool()` fuses this with pattern
    matching because it needs both at once, and here the pattern side is already
    stored in `evaluations`. Same shape otherwise — column projection and a
    thread pool over the partitions.
    """
    if not dois:
        return set()
    value_set = pa.array(sorted(dois), type=pa.string())
    files = sorted(Path(pool_dir).glob("*.parquet"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda p: _scan_ids(p, value_set, aliases), files)
    return {wid for chunk in results for wid in chunk}


# ---------------------------------------------------------------------------
# Screening verdicts
# ---------------------------------------------------------------------------


def screen_decisions(client, release: str) -> dict[str, dict[int, str]]:
    """`{tier: {work_id: outcome}}` from the recorded verdicts.

    The outcome is recomputed from the votes by `tiers._cheap_decision()` /
    `tiers._expensive_decision()` — the same functions the tiers and the handoff
    use — rather than read from a run report, because the verdict rows are the
    permanent evidence and a report file is a convenience. Both modes are read:
    a validation-mode run's votes are as informative about a RULE as a live one's,
    and it is the pile effect, not the evidence, that validation mode withholds.
    """
    from filter.engine.tiers import _cheap_decision, _expensive_decision

    out: dict[str, dict[int, str]] = {}
    for tier in TIERS:
        claim_ids = {c["id"] for c in client.claims(release_id=release, tier=tier)}
        if not claim_ids:
            out[tier] = {}
            continue
        by_work: dict[int, list[dict]] = {}
        for row in client.verdicts(tier, claim_ids):
            by_work.setdefault(int(row["work_id"]), []).append(row)
        decide = _cheap_decision if tier == "screen_cheap" else _expensive_decision
        out[tier] = {wid: decide(votes)["outcome"] for wid, votes in by_work.items()}
    return out


def wilson(successes: int, n: int, z: float = Z95) -> Optional[tuple[float, float]]:
    """Wilson score interval for *successes*/*n*, or None when n is 0.

    Wilson rather than normal-approximation because the counts here are small and
    often at a boundary, where the normal interval leaves the unit interval.
    """
    if n <= 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _measured(spec: FilterSpec) -> list[str]:
    out = []
    for entry in spec.measured:
        label = str(entry.get("level"))
        detail = []
        if entry.get("precision") is not None:
            detail.append(f"p={entry['precision']}")
        if entry.get("n") is not None:
            detail.append(f"n={entry['n']}")
        out.append(f"{label}({', '.join(detail)})" if detail else label)
    return out


def _screen_row(rule_id: str, decisions: dict[str, dict[int, str]],
                winner: dict[int, str]) -> dict:
    """Screened / proceed / discard / incomplete and the precision, for one rule."""
    proceed = discard = incomplete = 0
    tiers: dict[str, int] = {}
    for tier, per_work in decisions.items():
        for wid, outcome in per_work.items():
            if winner.get(wid) != rule_id:
                continue
            tiers[tier] = tiers.get(tier, 0) + 1
            if outcome == "proceed":
                proceed += 1
            elif outcome == "discard":
                discard += 1
            else:
                incomplete += 1
    decided = proceed + discard
    interval = wilson(proceed, decided)
    return {
        "screened": proceed + discard + incomplete,
        "proceed": proceed,
        "discard": discard,
        "incomplete": incomplete,
        "precision": (proceed / decided) if decided else None,
        "ci": list(interval) if interval else None,
        "by_tier": tiers,
    }


def build(*, spec_dir: Path, pool_dir: Path, store: Path, release: Optional[str],
          flora_path: Path, negatives_path: Path, aliases_path: Path,
          workers: int) -> dict:
    """Every measure for every spec in the bundle, as `render()` prints it."""
    started = time.time()
    specs = load_specs(spec_dir)
    aliases = load_aliases(aliases_path)

    con = open_readonly(store)
    try:
        release_id = _release(con, release)
        _register_specs(con, specs)
        routing = routing_outcome(con, release_id)
        matched = matched_counts(con, release_id)
        shadow_would = would_win(con, release_id)

        flora_dois = (flora_replication_dois(flora_path)
                      if Path(flora_path).exists() else set())
        pool_started = time.time()
        flora_ids = flora_work_ids(pool_dir, flora_dois, aliases, workers)
        pool_seconds = time.time() - pool_started
        flora_hits = spec_hits(con, release_id, sorted(flora_ids))
        flora_release = rule_winner(con, release_id, sorted(flora_ids))

        client, screen_note = _claims_client()
        decisions, screen_note = _decisions(client, release_id, screen_note)
        screened_ids = sorted({wid for per in decisions.values() for wid in per})
        winner = rule_winner(con, release_id, screened_ids)
    finally:
        con.close()

    negatives = (negative_rows(negatives_path) if Path(negatives_path).exists()
                 else [])

    flora_reached = {spec.id: 0 for spec in specs}
    flora_unique = {spec.id: 0 for spec in specs}
    for hits in flora_hits.values():
        for spec_id in hits:
            if spec_id in flora_reached:
                flora_reached[spec_id] += 1
        if len(hits) == 1:
            only = next(iter(hits))
            if only in flora_unique:
                flora_unique[only] += 1

    total = sum(routing["piles"].values())
    any_screened = any(decisions.get(tier) for tier in TIERS)

    rules = []
    for spec in specs:
        negative_hits = (int(sum(eval_spec_rows(spec, negatives))) if negatives else 0)
        won = 0 if spec.shadow else routing["won"].get(spec.id, 0)
        rules.append({
            "id": spec.id,
            "description": spec.description,
            "pile": spec.pile,
            "precedence": spec.precedence,
            "shadow": spec.shadow,
            "vocabulary": spec.vocabulary,
            "measured": _measured(spec),
            "won": won,
            "won_share": (won / total) if total else None,
            "won_no_text": routing["no_text"].get(spec.id, 0) if not spec.shadow else 0,
            "matched": matched.get(spec.id, 0),
            "would_win": shadow_would.get(spec.id, 0) if spec.shadow else None,
            "flora_reached": flora_reached[spec.id],
            "flora_unique": flora_unique[spec.id],
            "negatives": negative_hits,
            "screen": (_screen_row(spec.id, decisions, winner) if any_screened
                       else None),
        })

    return {
        "release": release_id,
        "store": str(store),
        "spec_dir": str(spec_dir),
        "bundle_hash": bundle_hash(spec_dir),
        "specs": len(specs),
        "total_works": total,
        "piles": routing["piles"],
        "pending": routing["pending"],
        "flora": {
            "dois": len(flora_dois),
            "in_pool": len(flora_ids),
            "in_release": len(flora_release),
            "reached_by_any": len(flora_hits),
            "path": str(flora_path),
        },
        "negatives": {"rows": len(negatives), "path": str(negatives_path)},
        "screen": {
            "available": any_screened,
            "note": screen_note,
            "decided": {tier: len(per) for tier, per in decisions.items()},
        },
        "rules": rules,
        "elapsed": {"total": time.time() - started, "pool_scan": pool_seconds},
    }


def _claims_client():
    """The claims client, or `(None, why not)` — an unconfigured Supabase is a
    normal state, and the report has to render before any tier has been run."""
    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
    try:
        return ClaimsClient(), ""
    except ClaimsNotConfigured:
        return None, "SUPABASE_URL unset — no state authority to read verdicts from"


def _decisions(client, release: str, note: str) -> tuple[dict, str]:
    """The recorded verdicts, or an empty read and the reason it was empty.

    A state authority that is configured but does not answer — the migrations not
    run, the network down — must produce "no verdicts, and here is why" rather
    than a traceback or, worse, a silent zero that reads like a screened rule
    whose rows all failed.
    """
    from filter.engine.claims import ClaimsError
    if client is None:
        return {}, note
    try:
        return screen_decisions(client, release), note
    except ClaimsError as exc:
        return {}, f"state authority unreadable: {exc}"


# ---------------------------------------------------------------------------
# Terminal render
# ---------------------------------------------------------------------------


def _precision_cell(screen: Optional[dict]) -> str:
    if screen is None:
        return "no verdicts"
    decided = screen["proceed"] + screen["discard"]
    if not screen["screened"]:
        return "not screened"
    if not decided:
        return f"{screen['incomplete']} incomplete"
    mark = DECORATION if decided < MIN_LABELLED else ""
    low, high = screen["ci"]
    return (f"{100 * screen['precision']:.0f}% (n={decided}) "
            f"[{100 * low:.0f}–{100 * high:.0f}]{mark}")


def _count(value: int) -> str:
    return f"{value:,}{DECORATION if 0 < value < MIN_LABELLED else ''}"


def render(report: dict) -> str:
    """The stdout report: the release, the piles, the per-rule table, the caveat."""
    lines: list[str] = []
    lines.append(f"applied rules — release {report['release'][:12]} · "
                 f"bundle {report['bundle_hash'][:12]} · {report['specs']} spec(s)")
    lines.append(f"store {report['store']} (read-only) · specs {report['spec_dir']}")
    total = report["total_works"]
    piles = " · ".join(f"{k} {v:,} ({100 * v / total:.1f}%)"
                       for k, v in sorted(report["piles"].items())) or "(none)"
    lines.append(f"release: {total:,} work(s) — {piles}")
    pending = report["pending"]
    if pending:
        lines.append("pending (the bundle's coverage gap): " + " · ".join(
            f"{k or '(unset)'} {v:,} ({100 * v / total:.1f}% of the release)"
            for k, v in sorted(pending.items())))
    flora = report["flora"]
    lines.append(f"FLoRA: {flora['dois']:,} replication DOIs · {flora['in_pool']:,} in "
                 f"the pool · {flora['in_release']:,} routed · "
                 f"{flora['reached_by_any']:,} reached by some rule")
    lines.append(f"known negatives: {report['negatives']['rows']:,} rows of "
                 f"{report['negatives']['path']} (matched on title/abstract only)")
    screen = report["screen"]
    if screen["available"]:
        lines.append("screening verdicts: " + " · ".join(
            f"{tier} {n:,} work(s)" for tier, n in sorted(screen["decided"].items())))
    else:
        lines.append("screening verdicts: NONE recorded for this release" +
                     (f" ({screen['note']})" if screen["note"] else "") +
                     " — the screening columns say 'no verdicts', not 0%.")
    lines.append("")

    header = ("rule", "pile", "prec", "sh", "won", "% rel", "matched", "would win",
              "FLoRA", "fl.uniq", "neg", "screened", "proceed (Wilson 95%)")
    table = [header]
    for rule in report["rules"]:
        screen_row = rule["screen"]
        table.append((
            rule["id"],
            rule["pile"],
            str(rule["precedence"]),
            "yes" if rule["shadow"] else "",
            "—" if rule["shadow"] else f"{rule['won']:,}",
            "—" if rule["shadow"] or rule["won_share"] is None
            else f"{100 * rule['won_share']:.2f}%",
            f"{rule['matched']:,}",
            f"{rule['would_win']:,}" if rule["would_win"] is not None else "—",
            _count(rule["flora_reached"]),
            _count(rule["flora_unique"]),
            _count(rule["negatives"]),
            "—" if screen_row is None else f"{screen_row['screened']:,}",
            _precision_cell(screen_row),
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for index, row in enumerate(table):
        lines.append("  ".join(
            cell.ljust(widths[j]) if j <= 1 else cell.rjust(widths[j])
            for j, cell in enumerate(row)))
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    lines.append("")
    for rule in report["rules"]:
        measured = ", ".join(rule["measured"]) or "no measured entry"
        vocabulary = f" · {rule['vocabulary']}" if rule["vocabulary"] else ""
        lines.append(f"  {rule['id']}{vocabulary} — {measured}")
        if rule["won_no_text"]:
            lines.append(f"      {rule['won_no_text']:,} of its wins were downgraded "
                         "to pending/no_text (no abstract to screen)")
    lines.append("")
    lines.append(f"{DECORATION} fewer than {MIN_LABELLED} labelled or screened rows "
                 "behind the cell — decoration, not evidence.")
    lines.append(
        "won = the rule was the highest-precedence non-shadow match; matched = it "
        "matched at all, winner or not. A shadow rule wins nothing, so it reports "
        "'would win': works no higher-precedence live rule also matched (the "
        "no-text downgrade is not modelled). fl.uniq counts FLoRA works no OTHER "
        "rule in the bundle reaches, draft rules included — a live rule shadowed "
        "by a broad draft therefore carries little uniquely.")
    lines.append(
        "BIAS: the screening verdicts and both CSVs describe what the OLD filter "
        "admitted, so every precision here is optimistic; FLoRA capture is partly "
        "circular, because much of flora.csv was found with these very phrases. "
        "This ranks rules and gates cheap decisions — it does not replace a "
        "human-labelled precision estimate for anything that discards.")
    elapsed = report["elapsed"]
    lines.append(f"elapsed: {elapsed['total']:.1f}s "
                 f"(pool scan {elapsed['pool_scan']:.1f}s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML render
# ---------------------------------------------------------------------------

_CSS = """
:root { --ink:#1c2333; --muted:#5b6478; --accent:#1a5fb4; --rule:#e3e6ee; --bg:#fdfdfc;
        --panel:#f4f6fa; --warn:#9a6a00; --bad:#b3261e; --good:#3a7d44; --code-bg:#f0f2f7; }
@media (prefers-color-scheme: dark) {
  :root { --ink:#e6e9f2; --muted:#9aa3b8; --accent:#7cb0f0; --rule:#2c3346; --bg:#12151d;
          --panel:#1b2030; --warn:#d9a441; --bad:#f0857d; --good:#79c98a; --code-bg:#1e2433; }
  /* The tag colours are light in dark mode, so their label has to stop being white. */
  .tag { color:#10131a; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:16px/1.65 "Avenir Next","Segoe UI",system-ui,sans-serif; }
main { max-width:1100px; margin:0 auto; padding:3rem 1.5rem 6rem; }
h1 { font-size:1.9rem; line-height:1.25; margin:0 0 .3rem; }
.subtitle { color:var(--muted); margin:0 0 2.2rem; font-size:1.02rem; }
h2 { font-size:1.35rem; margin:2.6rem 0 .8rem; padding-top:1.2rem;
     border-top:2px solid var(--rule); }
p { margin:.6rem 0; }
code { background:var(--code-bg); padding:.1em .35em; border-radius:4px; font-size:.88em;
       font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace; }
table { border-collapse:collapse; width:100%; margin:1rem 0; font-size:.88rem; }
th,td { border:1px solid var(--rule); padding:.4rem .55rem; text-align:left;
        vertical-align:top; white-space:nowrap; }
td.wrap { white-space:normal; }
th { background:var(--panel); font-weight:600; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.tablewrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
.box { background:var(--panel); border-left:4px solid var(--accent);
       padding:1rem 1.2rem; border-radius:0 8px 8px 0; margin:1.5rem 0; }
.box.warn { border-left-color:var(--warn); }
.tag { display:inline-block; font-size:.72rem; font-weight:700; padding:.12em .55em;
       border-radius:999px; color:#fff; background:var(--muted); }
.t-live { background:var(--good); } .t-draft { background:var(--warn); }
.t-discard { background:var(--bad); }
.small { color:var(--muted); font-size:.9rem; }
.none { color:var(--muted); font-style:italic; }
summary { cursor:pointer; color:var(--accent); font-size:.85em; margin-top:.3rem; }
details p, details { color:var(--muted); }
ul { margin:.6rem 0; padding-left:1.4rem; }
li { margin:.35rem 0; }
"""


def _esc(value) -> str:
    return html.escape(str(value))


def _description(text: str) -> str:
    """A spec description as a lead sentence plus a `<details>` for the rest.

    The shipped descriptions are essays — the rationale for a rule belongs in the
    spec — and pasting one whole into a table cell makes the table unreadable.
    `<details>` keeps the full text on the page without a line of JavaScript.
    """
    text = text.strip()
    cut = text.find(". ")
    if 0 <= cut <= 240:
        lead, rest = text[:cut + 1], text[cut + 1:]
    elif len(text) <= 240:
        lead, rest = text, ""
    else:
        cut = text.rfind(" ", 0, 240)          # never break a word
        lead, rest = text[:cut] + " …", text[cut:]
    if not rest.strip():
        return _esc(lead)
    return (f"{_esc(lead)} <details><summary>rationale</summary>"
            f"{_esc(rest.strip())}</details>")


def _html_screen(screen: Optional[dict]) -> str:
    if screen is None or not screen["screened"]:
        text = "no verdicts" if screen is None else "not screened"
        return f'<span class="none">{text}</span>'
    return _esc(_precision_cell(screen))


def render_html(report: dict) -> str:
    """A self-contained page, no external assets, readable light and dark."""
    total = report["total_works"]
    flora = report["flora"]
    screen = report["screen"]
    parts: list[str] = [
        "<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>FLoRA filter engine — applied rules "
        f"({_esc(report['release'][:12])})</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>", "<main>",
        "<h1>Applied rules — what each Stage 2 rule did to the corpus</h1>",
        f'<p class="subtitle">Routing release <code>{_esc(report["release"][:12])}</code> · '
        f'bundle <code>{_esc(report["bundle_hash"][:12])}</code> · '
        f'{report["specs"]} spec(s) · {total:,} routed work(s).<br>'
        f'Generated by <code>python -m analysis.rule_report</code> from '
        f'<code>{_esc(report["store"])}</code> (read-only) and '
        f'<code>{_esc(report["spec_dir"])}</code>.</p>',
    ]

    parts.append("<h2>Pile composition</h2>")
    parts.append('<div class="tablewrap"><table>')
    parts.append("<tr><th>Pile</th><th>Works</th><th>Share of the release</th></tr>")
    for pile, count in sorted(report["piles"].items()):
        parts.append(f'<tr><td><code>{_esc(pile)}</code></td>'
                     f'<td class="num">{count:,}</td>'
                     f'<td class="num">{100 * count / total:.2f}%</td></tr>')
    parts.append("</table></div>")
    if report["pending"]:
        parts.append("<p><strong>The coverage gap.</strong> "
                     "<code>pending</code> splits into:</p><ul>")
        for reason, count in sorted(report["pending"].items()):
            parts.append(f"<li><code>{_esc(reason or '(unset)')}</code> — "
                         f"{count:,} work(s), {100 * count / total:.2f}% of the "
                         "release</li>")
        parts.append("</ul>")

    parts.append("<h2>Per rule</h2>")
    parts.append('<div class="tablewrap"><table>')
    parts.append(
        "<tr><th>Rule</th><th>Pile</th><th>Prec</th><th>State</th><th>Won</th>"
        "<th>% of release</th><th>Matched</th><th>Would win</th><th>FLoRA</th>"
        "<th>FLoRA only</th><th>Known neg.</th><th>Screened</th>"
        "<th>Proceed (Wilson 95%)</th></tr>")
    for rule in report["rules"]:
        state = ('<span class="tag t-draft">draft</span>' if rule["shadow"]
                 else ('<span class="tag t-discard">live discard</span>'
                       if rule["pile"] == "discard"
                       else '<span class="tag t-live">live</span>'))
        won = "—" if rule["shadow"] else format(rule["won"], ",")
        share = ("—" if rule["shadow"] or rule["won_share"] is None
                 else f"{100 * rule['won_share']:.2f}%")
        would = ("—" if rule["would_win"] is None
                 else format(rule["would_win"], ","))
        screened = ("—" if rule["screen"] is None
                    else format(rule["screen"]["screened"], ","))
        parts.append(
            f'<tr><td><code>{_esc(rule["id"])}</code></td>'
            f'<td><code>{_esc(rule["pile"])}</code></td>'
            f'<td class="num">{rule["precedence"]}</td>'
            f'<td>{state}</td>'
            f'<td class="num">{won}</td>'
            f'<td class="num">{share}</td>'
            f'<td class="num">{rule["matched"]:,}</td>'
            f'<td class="num">{would}</td>'
            f'<td class="num">{_esc(_count(rule["flora_reached"]))}</td>'
            f'<td class="num">{_esc(_count(rule["flora_unique"]))}</td>'
            f'<td class="num">{_esc(_count(rule["negatives"]))}</td>'
            f'<td class="num">{screened}</td>'
            f'<td>{_html_screen(rule["screen"])}</td></tr>')
    parts.append("</table></div>")

    parts.append('<div class="tablewrap"><table>')
    parts.append("<tr><th>Rule</th><th>Vocabulary</th><th>Measured</th>"
                 "<th>Description</th></tr>")
    for rule in report["rules"]:
        measured = ", ".join(rule["measured"]) or \
            '<span class="none">no measured entry</span>'
        parts.append(
            f'<tr><td><code>{_esc(rule["id"])}</code></td>'
            f'<td>{_esc(rule["vocabulary"] or "—")}</td>'
            f'<td>{measured}</td>'
            f'<td class="wrap">{_description(rule["description"])}</td></tr>')
    parts.append("</table></div>")

    parts.append("<h2>Labels, and what they are worth</h2>")
    if not screen["available"]:
        why = f'<em>{_esc(screen["note"])}.</em> ' if screen["note"] else ""
        parts.append(
            f'<div class="box warn"><p><strong>No screening verdicts exist for this '
            f'release.</strong> {why}Every screening cell above '
            f'reads <em>no verdicts</em>; none of them is a measured zero. As soon as '
            f'<code>python -m filter.engine screen --run</code> has recorded its first '
            f'tier, those cells fill in.</p></div>')
    else:
        counts = " · ".join(f"<code>{_esc(tier)}</code> {n:,} work(s)"
                            for tier, n in sorted(screen["decided"].items()))
        parts.append(
            '<div class="box"><p><strong>Screening verdicts read back from the state '
            f'authority</strong>: {counts}. Outcomes are recomputed from the stored '
            "votes with the same gate the tiers use. A rule with no verdicts of its "
            "own still reads <em>not screened</em>.</p></div>")
    parts.append(
        f"<p><strong>FLoRA.</strong> {flora['dois']:,} known replication DOIs; "
        f"{flora['in_pool']:,} are in the survivor pool, {flora['in_release']:,} were "
        f"routed in this release, and {flora['reached_by_any']:,} were matched by at "
        "least one rule. <em>FLoRA only</em> counts the works no other rule in the "
        "bundle reaches — the recall that rule uniquely carries. Draft rules count "
        "as other rules, so a live rule whose matches a broad draft also covers "
        "carries little uniquely.</p>")
    parts.append(
        f"<p><strong>Known negatives.</strong> {report['negatives']['rows']:,} rows of "
        f"<code>{_esc(report['negatives']['path'])}</code>, matched on that file's own "
        "title and abstract columns only — a rule keyed on DOI or work type scores "
        "zero here for want of the columns, not for want of hits.</p>")
    parts.append(
        f'<p class="small">{DECORATION} marks a cell resting on fewer than '
        f"{MIN_LABELLED} labelled or screened rows: decoration, not evidence. "
        "<em>Won</em> means the rule was the highest-precedence non-shadow match; "
        "<em>matched</em> counts every row it matched, winner or not; a draft "
        "(shadow) rule wins nothing by construction and reports <em>would win</em> "
        "instead — works no higher-precedence live rule also matched, without "
        "modelling the no-text downgrade.</p>")
    parts.append(
        '<div class="box warn"><p><strong>Bias, standing.</strong> The screening '
        "verdicts and both CSVs describe what the OLD filter admitted, so every "
        "precision here is optimistic; FLoRA capture is partly circular, because much "
        "of <code>flora.csv</code> was found with these very phrases. This page ranks "
        "rules and gates cheap decisions. It does not replace a human-labelled "
        "precision estimate for anything that <strong>discards</strong>.</p></div>")
    parts.append(f'<p class="small">Built in {report["elapsed"]["total"]:.1f}s '
                 f'(pool scan {report["elapsed"]["pool_scan"]:.1f}s).</p>')
    parts.extend(["</main>", "</body>", "</html>"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analysis.rule_report",
        description="One page for the whole filter bundle: what each rule did to the "
                    "corpus, and — once a tier has screened — how right it was.")
    parser.add_argument("--html", dest="html_path", type=Path,
                        help="also write a self-contained HTML page here")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="also write the raw numbers here")
    parser.add_argument("--release", default=None,
                        help="routing release to report (default: the newest)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--spec-dir", type=Path, default=SPEC_DIR)
    parser.add_argument("--flora", type=Path, default=DATA_DIR / "flora.csv")
    parser.add_argument("--negatives", type=Path,
                        default=DATA_DIR / "not_a_replication.csv")
    parser.add_argument("--aliases", type=Path, default=None,
                        help="alias file (default: <spec-dir>/aliases.json)")
    args = parser.parse_args(argv)

    try:
        report = build(spec_dir=args.spec_dir, pool_dir=args.pool, store=args.store,
                       release=args.release, flora_path=args.flora,
                       negatives_path=args.negatives,
                       aliases_path=args.aliases or (args.spec_dir / ALIASES_FILENAME),
                       workers=args.workers)
    except StoreUnavailable as exc:
        raise SystemExit(str(exc))

    print(render(report))
    if args.json_path:
        args.json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"wrote {args.json_path}")
    if args.html_path:
        args.html_path.write_text(render_html(report), encoding="utf-8")
        print(f"wrote {args.html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
