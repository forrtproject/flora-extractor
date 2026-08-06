"""Postgres sizing for the engine state tables (issue #146 §8 decision 1).

The question the maintainer has to answer before the first big campaign is
free-tier vs paid Postgres, and the honest form of it is: *how many bytes does one
claimed row and one verdict actually cost?* This module answers it by MEASURING
rather than guessing — it builds N synthetic rows of exactly the shape
`db/migrations/0001_engine_baseline.sql` defines, sums their real encoded widths
(varlena text lengths, jsonb payload lengths, fixed-width columns), adds Postgres'
per-tuple and per-index overheads, and projects the total for the 5.1M-row pool.

Two modes:

* default — the analytic model above, no database needed, fully deterministic.
* `DATABASE_URL` set — the same synthetic rows are inserted into TEMP tables in a
  real database and `pg_total_relation_size()` is read back. Needs `psycopg`; if
  it is not installed the model is used instead, silently, and `method` says so.

Run:

    python -m filter.engine.sizing --rows 5146160 --verdict-rate 0.1
"""

import argparse
import json
import os
from typing import Any, Optional

# ── Postgres physical overheads (constants, not guesses about our data) ───────
# A heap tuple carries a 23-byte HeapTupleHeader, padded to the 8-byte MAXALIGN,
# plus a 4-byte item pointer in the page. Pages are 8 KB with a 24-byte header,
# and the default fillfactor leaves heap pages full but btree pages at 90%.
HEAP_TUPLE_HEADER = 24
ITEM_POINTER = 4
BTREE_TUPLE_HEADER = 8          # IndexTupleData
BTREE_FILLFACTOR = 0.9
PAGE_OVERHEAD = 8192 / (8192 - 24)

FREE_TIER_MB = 500              # Supabase free tier database size

# One claim per this many items — sets how many engine_claims and engine_audit
# rows a campaign writes. Both tables are per-BATCH, so they round to nothing;
# they are counted anyway so "total" means total.
ITEMS_PER_CLAIM = 1000
# The claim, and the release_claim that ends it. An UPPER BOUND: a claim whose run
# is killed writes only the first and expires instead of being released, so the
# projection runs slightly high. Left at 2 deliberately — the true rate is however
# often a run dies, which nothing measures, and over-estimating the smallest term
# in the total is the safe direction for a free-tier-or-not decision.
AUDIT_ROWS_PER_CLAIM = 2

SYNTHETIC_N = 1000              # synthetic rows measured per table

_PILES = ("screen_expensive", "screen_cheap", "needs_human", "discard")
_VERDICTS = ("replication", "reproduction", "none", "unclear")
_CONFIDENCES = ("high", "medium", "low")
# Representative model-id STRINGS, for their byte width only — this measures how wide
# a stored row is, and never selects a model. Voter ids live in shared/config.py.
_MODELS = ("gemini-3.5-flash-lite", "gpt-5.4-mini", "openai/gpt-5.4-mini")
# A screen quote is a sentence from the abstract; this is a representative one.
_QUOTE = ("We conducted a direct replication of Study 2 of Smith and Jones (2009) "
          "with a larger sample drawn from the same population, preregistered on OSF.")


def _varlena(text: str) -> int:
    """On-disk width of a text/jsonb value: 1-byte header under 127 bytes, else 4."""
    n = len(text.encode("utf-8"))
    return (1 + n) if n < 127 else (4 + n)


def _align8(n: int) -> int:
    return (n + 7) // 8 * 8


def _heap_row_bytes(column_widths: list[int]) -> int:
    return HEAP_TUPLE_HEADER + _align8(sum(column_widths)) + ITEM_POINTER


def _index_row_bytes(key_widths: list[int]) -> int:
    return int((BTREE_TUPLE_HEADER + _align8(sum(key_widths)) + ITEM_POINTER)
               / BTREE_FILLFACTOR)


# ── Synthetic rows: the empirical part ───────────────────────────────────────
# Deterministic by construction (index-derived, no RNG) so two runs of `estimate`
# on the same arguments return byte-identical numbers.

def _claim_item_rows(n: int = SYNTHETIC_N) -> list[dict]:
    return [{"claim_id": "0" * 32, "work_id": 2000000000 + i,
             "pile": _PILES[i % len(_PILES)]} for i in range(n)]


def _verdict_rows(n: int = SYNTHETIC_N) -> list[dict]:
    rows = []
    for i in range(n):
        rows.append({
            "id": "0" * 32, "claim_id": "0" * 32,
            "work_id": 2000000000 + i,
            "tier": "screen_expensive" if i % 2 else "screen_cheap",
            "model": _MODELS[i % len(_MODELS)],
            "prompt_hash": f"{i:064x}",
            "verdict": _VERDICTS[i % len(_VERDICTS)],
            "confidence": _CONFIDENCES[i % len(_CONFIDENCES)],
            # Quotes vary; a fixed one would measure a single length, not a spread.
            "quote": _QUOTE[: 60 + (i % 120)],
            "response_hash": f"{i:064x}",
            "response_state": "uploaded",
            "cost": {"input_tokens": 1200 + i, "output_tokens": 90,
                     "usd": round(0.00012 + i * 1e-9, 9)},
        })
    return rows


def _measure_claim_items(rows: list[dict]) -> dict:
    """Bytes per engine_claim_items row: heap + PK(claim_id, work_id) + work_id index."""
    heap = idx = 0
    for r in rows:
        heap += _heap_row_bytes([16, 8, _varlena(r["pile"])])   # uuid, bigint, text
        idx += _index_row_bytes([16, 8])                        # PRIMARY KEY
        idx += _index_row_bytes([8])                            # work_id lookup
    n = len(rows)
    return {"heap_bytes_per_row": heap / n * PAGE_OVERHEAD,
            "index_bytes_per_row": idx / n * PAGE_OVERHEAD,
            "bytes_per_row": (heap + idx) / n * PAGE_OVERHEAD}


def _measure_verdicts(rows: list[dict]) -> dict:
    """Bytes per engine_verdicts row: heap + PK(id) + work_id + claim_id indexes."""
    heap = idx = 0
    for r in rows:
        widths = [
            16,                                   # id uuid
            16,                                   # claim_id uuid
            8,                                    # work_id bigint
            _varlena(r["tier"]),
            _varlena(r["model"]),
            _varlena(r["prompt_hash"]),
            _varlena(r["verdict"]),
            _varlena(r["confidence"]),
            _varlena(r["quote"]),
            _varlena(r["response_hash"]),
            _varlena(r["response_state"]),
            # jsonb is stored binary; its width tracks the JSON text closely at
            # these sizes, and it is small enough never to be TOASTed out of line.
            _varlena(json.dumps(r["cost"], separators=(",", ":"))),
            8,                                    # created_at timestamptz
            16,                                   # superseded_by uuid (NULL, still aligned)
        ]
        heap += _heap_row_bytes(widths)
        idx += _index_row_bytes([16])             # PRIMARY KEY (id)
        idx += _index_row_bytes([8])              # work_id
        idx += _index_row_bytes([16])             # claim_id
    n = len(rows)
    return {"heap_bytes_per_row": heap / n * PAGE_OVERHEAD,
            "index_bytes_per_row": idx / n * PAGE_OVERHEAD,
            "bytes_per_row": (heap + idx) / n * PAGE_OVERHEAD}


# ── Optional: measure against a real database ────────────────────────────────

_DDL = {
    "claim_items": """
        CREATE TEMP TABLE sizing_claim_items (
            claim_id uuid NOT NULL, work_id bigint NOT NULL, pile text NOT NULL,
            PRIMARY KEY (claim_id, work_id))""",
    "verdicts": """
        CREATE TEMP TABLE sizing_verdicts (
            id uuid PRIMARY KEY, claim_id uuid NOT NULL, work_id bigint NOT NULL,
            tier text NOT NULL, model text NOT NULL, prompt_hash text NOT NULL,
            verdict text NOT NULL, confidence text NOT NULL, quote text NOT NULL,
            response_hash text, response_state text NOT NULL, cost jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(), superseded_by uuid)""",
}


def _measure_in_database(dsn: str) -> Optional[dict]:
    """Real bytes/row from `pg_total_relation_size`, or None if psycopg is absent."""
    try:
        import psycopg  # type: ignore
    except ImportError:
        return None

    items = _claim_item_rows()
    verdicts = _verdict_rows()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_DDL["claim_items"])
        cur.execute("CREATE INDEX ON sizing_claim_items (work_id)")
        cur.executemany(
            "INSERT INTO sizing_claim_items (claim_id, work_id, pile) VALUES (gen_random_uuid(), %s, %s)",
            [(r["work_id"], r["pile"]) for r in items])

        cur.execute(_DDL["verdicts"])
        cur.execute("CREATE INDEX ON sizing_verdicts (work_id)")
        cur.execute("CREATE INDEX ON sizing_verdicts (claim_id)")
        cur.executemany(
            "INSERT INTO sizing_verdicts (id, claim_id, work_id, tier, model, prompt_hash,"
            " verdict, confidence, quote, response_hash, response_state, cost)"
            " VALUES (gen_random_uuid(), gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(r["work_id"], r["tier"], r["model"], r["prompt_hash"], r["verdict"],
              r["confidence"], r["quote"], r["response_hash"], r["response_state"],
              json.dumps(r["cost"])) for r in verdicts])

        cur.execute("SELECT pg_total_relation_size('sizing_claim_items'),"
                    " pg_total_relation_size('sizing_verdicts')")
        items_bytes, verdict_bytes = cur.fetchone()

    return {
        "claim_items": {"bytes_per_row": items_bytes / len(items)},
        "verdicts": {"bytes_per_row": verdict_bytes / len(verdicts)},
    }


# ── The projection ───────────────────────────────────────────────────────────

def estimate(rows: int, verdicts_per_row: float, dsn: Optional[str] = None) -> dict:
    """Projected Postgres size for *rows* claimed works at *verdicts_per_row*.

    `rows` is how many works get claimed (one claim_item each); `verdicts_per_row`
    is the average number of verdict rows a claimed work produces — 1.0 means one
    tier answers once, 2.0 means the cheap tier passes it to the expensive one.
    Fractions model claiming a pool but only spending on part of it.

    Returns per-table bytes/row, projected MB, the free-tier verdict, and the row
    count at which the projection reaches 500 MB.
    """
    if rows < 0 or verdicts_per_row < 0:
        raise ValueError("rows and verdicts_per_row must be non-negative")

    dsn = dsn if dsn is not None else os.getenv("DATABASE_URL", "")
    measured = _measure_in_database(dsn) if dsn else None

    if measured:
        method = "database"
        items = measured["claim_items"]
        verdicts = measured["verdicts"]
    else:
        method = "model" if not dsn else "model (psycopg not installed)"
        items = _measure_claim_items(_claim_item_rows())
        verdicts = _measure_verdicts(_verdict_rows())

    n_claims = rows / ITEMS_PER_CLAIM if rows else 0
    # engine_claims (uuid + text release id + two timestamps + jsonb meta) and
    # engine_audit rows are per batch; measured the same way, at batch scale.
    claim_row_bytes = _heap_row_bytes([16, _varlena("0" * 64), _varlena("screen_expensive"),
                                       _varlena("active"), 8, 8, _varlena('{"batch":"wave-1"}')])
    audit_row_bytes = _heap_row_bytes([8, _varlena("engine"), _varlena("claim"),
                                       _varlena('{"claim_id":"' + "0" * 36
                                                + '","tier":"screen_expensive","n_items":1000}'),
                                       8])

    per_mb = 1024 * 1024
    items_mb = rows * items["bytes_per_row"] / per_mb
    verdicts_mb = rows * verdicts_per_row * verdicts["bytes_per_row"] / per_mb
    overhead_mb = n_claims * (claim_row_bytes + AUDIT_ROWS_PER_CLAIM * audit_row_bytes) / per_mb
    total_mb = items_mb + verdicts_mb + overhead_mb

    mb_per_row = total_mb / rows if rows else 0.0
    return {
        "method": method,
        "rows": rows,
        "verdicts_per_row": verdicts_per_row,
        "claim_items_bytes_per_row": round(items["bytes_per_row"], 1),
        "verdict_bytes_per_row": round(verdicts["bytes_per_row"], 1),
        "claim_items_mb": round(items_mb, 1),
        "verdicts_mb": round(verdicts_mb, 1),
        "claims_and_audit_mb": round(overhead_mb, 1),
        "total_mb": round(total_mb, 1),
        "free_tier_mb": FREE_TIER_MB,
        "fits_free_tier": total_mb <= FREE_TIER_MB,
        # How many claimed works fit in the free tier at this verdict rate — the
        # number that decides "campaign now, upgrade later" vs "upgrade first".
        "free_tier_row_capacity": int(FREE_TIER_MB / mb_per_row) if mb_per_row else 0,
    }


def format_report(result: dict, scenarios: Optional[list[dict]] = None) -> str:
    lines = [
        "Engine Postgres sizing (issue #146 §8 decision 1)",
        f"  method                : {result['method']}",
        f"  claimed works         : {result['rows']:,}",
        f"  verdicts per work     : {result['verdicts_per_row']}",
        "",
        f"  engine_claim_items    : {result['claim_items_bytes_per_row']:>8.1f} B/row"
        f"  →  {result['claim_items_mb']:>8.1f} MB",
        f"  engine_verdicts       : {result['verdict_bytes_per_row']:>8.1f} B/row"
        f"  →  {result['verdicts_mb']:>8.1f} MB",
        f"  claims + audit        : {'':>8}     "
        f"  →  {result['claims_and_audit_mb']:>8.1f} MB",
        f"  TOTAL                 : {'':>8}     "
        f"  →  {result['total_mb']:>8.1f} MB",
        "",
        f"  free tier             : {result['free_tier_mb']} MB — "
        + ("fits" if result["fits_free_tier"] else "EXCEEDED, upgrade needed"),
        f"  free-tier capacity    : {result['free_tier_row_capacity']:,} claimed works "
        f"at {result['verdicts_per_row']} verdicts/work",
    ]
    if scenarios:
        lines += ["", "  verdict rate    total MB   free tier"]
        for s in scenarios:
            lines.append(f"  {s['verdicts_per_row']:>10}    {s['total_mb']:>9.1f}   "
                         + ("fits" if s["fits_free_tier"] else "exceeded"))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", type=int, default=5_146_160,
                        help="claimed works (default: the survivor pool)")
    parser.add_argument("--verdict-rate", type=float, default=0.1,
                        help="average verdict rows per claimed work")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    result = estimate(args.rows, args.verdict_rate)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    scenarios: list[dict[str, Any]] = [estimate(args.rows, r) for r in (0.1, 0.5, 1.0, 2.0)]
    print(format_report(result, scenarios))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
