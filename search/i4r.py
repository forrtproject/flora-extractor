"""Incremental I4R series ingestion into the survivor pool, never production CSVs.

EconPapers exposes the complete RePEc:zbw:i4rdps series with numbered pagination
and citation metadata. Refresh the catalogue each run; cache successful details.
The committed pool shard is the checkpoint, so a failed run can safely resume.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from filelock import FileLock
from lxml import etree, html

from search.snapshot_scan import _POOL_SCHEMA
from shared.config import CACHE_DIR, I4R_WORKERS, RESEARCHER_EMAIL, SNAPSHOT_POOL_DIR
from shared.rate_limit import throttle
from shared.utils import clean_doi

SERIES = "https://econpapers.repec.org/paper/zbwi4rdps/"
FLORA_URL = (
    "https://raw.githubusercontent.com/forrtproject/fred-data/main/output/flora.csv"
)


def _durable_replace(tmp: Path, destination: Path) -> None:
    """Publish a complete file, syncing its bytes and renamed directory entry."""
    with tmp.open("rb") as stream:
        os.fsync(stream.fileno())
    tmp.replace(destination)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def fetch(
    url: str,
    cache: Path,
    refresh: bool = False,
    validate: Callable[[str], object] | None = None,
) -> str:
    """Cache before parsing; evict invalid bodies so transient errors can recover."""
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + ".txt")
    cache.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        if path.exists() and not refresh:
            body = path.read_text()
            try:
                if validate:
                    validate(body)
                return body
            except ValueError:
                path.unlink()
        throttle("i4r_crossref" if "api.crossref.org" in url else "i4r_econpapers", 0.5)
        response = requests.get(
            url,
            timeout=60,
            headers={
                "User-Agent": f"FLoRAExtractor I4R harvester (mailto:{RESEARCHER_EMAIL})"
            },
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(response.text)
        _durable_replace(tmp, path)
        try:
            if validate:
                validate(response.text)
        except ValueError:
            path.unlink()
            raise
        return response.text


def _crossref_items(text: str) -> list[dict]:
    """Reject challenge pages and malformed JSON instead of caching false misses."""
    try:
        items = json.loads(text)["message"]["items"]
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("DOI", ""), str)
            or not isinstance(item.get("title", []), list)
            or any(not isinstance(title, str) for title in item.get("title", []))
            for item in items
        ):
            raise ValueError("Malformed Crossref items")
        return items
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed Crossref response") from exc


def _flora_rows(text: str) -> list[dict]:
    """Require identifier columns so an HTTP-200 error page cannot erase overlap."""
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
        if not {"url_r", "doi_r"}.issubset(reader.fieldnames or []):
            raise ValueError("FLoRA CSV must contain url_r and doi_r columns")
        rows = list(reader)
        if any(None in row for row in rows):
            raise ValueError("FLoRA CSV row has more fields than its header")
        return rows
    except csv.Error as exc:
        raise ValueError("Malformed FLoRA CSV") from exc


def catalogue(cache: Path) -> dict[int, str]:
    """Traverse pagination rather than guessing a maximum DP number."""
    todo, visited, papers = [SERIES], set(), {}
    while todo:
        url = todo.pop()
        if url in visited:
            continue
        visited.add(url)
        page = html.fromstring(fetch(url, cache, refresh=True))
        found = 0
        for href in page.xpath("//a/@href"):
            if re.fullmatch(r"\d+\.htm", href):
                papers[int(href[:-4])] = urljoin(SERIES, href)
                found += 1
            elif re.fullmatch(r"default\d*\.htm", href):
                target = urljoin(SERIES, href)
                if target not in visited:
                    todo.append(target)
        if not found:
            raise ValueError(
                f"No I4R papers found on {url}; refusing an empty/partial harvest"
            )
    return dict(sorted(papers.items()))


def parse_detail(number: int, text: str) -> dict:
    """Require citation metadata for this exact DP before using a detail page."""
    try:
        page = html.fromstring(text)
    except (etree.ParserError, etree.XMLSyntaxError) as exc:
        raise ValueError(f"Unparseable I4R detail: {number}") from exc
    meta: dict[str, list[str]] = {}
    for node in page.xpath("//meta[@name][@content]"):
        meta.setdefault(node.get("name").lower(), []).append(node.get("content"))
    title = meta.get("citation_title", [""])[0]
    year = meta.get("citation_year", [""])[0]
    handle = meta.get("handle", [""])[0]
    if (
        not title
        or not re.fullmatch(r"20\d\d", year)
        or handle.lower() != f"repec:zbw:i4rdps:{number}"
    ):
        raise ValueError(f"Incomplete/wrong I4R detail: {number}")
    urls = [unquote(href) for href in page.xpath("//a/@href")]
    pdfs = [
        m.group(0)
        for u in urls
        for m in re.finditer(r"https?://(?:www\.)?econstor\.eu/bitstream/[^;\s]+", u)
    ]
    handles = [m.group(1) for u in urls for m in re.finditer(r"(10419/\d+)", u)]
    abstract = meta.get("citation_abstract", [""])[0]
    quoted = re.search(r'["“](.+?)["”]', title)
    return {
        "number": number,
        "handle": handle,
        "title": title,
        "authors": meta.get("citation_author", meta.get("citation_authors", [])),
        "year": int(year),
        "abstract": abstract,
        "url": f"https://hdl.handle.net/{handles[0]}"
        if handles
        else SERIES + f"{number}.htm",
        "pdf_url": pdfs[0] if pdfs else "",
        "doi": clean_doi(meta.get("citation_doi", [""])[0]),
        "original_title": quoted.group(1) if quoted else "",
        "doi_o": "",
        "original_status": "stage3_required",
    }


def resolve_original(record: dict, cache: Path) -> None:
    """Only an exact, unique Crossref title match supplies a DOI hint.

    Comments, replies and methods papers may have no single original. The normal
    Stage 3 ladder handles unresolved targets and verifies every accepted link.
    """
    title = record["original_title"]
    if not title:
        return
    url = (
        "https://api.crossref.org/works?rows=3&query.title="
        + requests.utils.quote(title)
        + "&mailto="
        + requests.utils.quote(RESEARCHER_EMAIL)
    )
    try:
        items = _crossref_items(fetch(url, cache, validate=_crossref_items))
    except (requests.RequestException, ValueError):
        record["original_status"] = "crossref_unavailable_stage3_required"
        return

    def normalize(text: str) -> str:
        return re.sub(r"[^\w]", "", text).casefold()

    matches = {
        clean_doi(item.get("DOI", ""))
        for item in items
        if normalize((item.get("title") or [""])[0]) == normalize(title)
    } - {record["doi"], ""}
    if len(matches) == 1:
        record["doi_o"] = matches.pop()
        record["original_status"] = "crossref_exact_title_hint"


def identity_keys(value: str) -> set[str]:
    """Match repository handles across landing/PDF URLs and both RePEc mirrors."""
    value = unquote(value).lower()
    keys = {"hdl:" + m for m in re.findall(r"10419/(\d+)", value)}
    keys |= {"dp:" + str(int(m)) for m in re.findall(r"i4r-dp0*(\d+)", value)}
    keys |= {
        "dp:" + str(int(m))
        for m in re.findall(
            r"(?:repec:zbw:i4rdps:|/p/zbw/i4rdps/|/paper/zbwi4rdps/)(\d+)", value
        )
    }
    return keys


def existing_pool_ids(pool: Path, wanted: set[str]) -> dict[str, str]:
    """Reuse snapshot identity on exact DOI/handle matches, never title similarity."""
    found: dict[str, str] = {}
    for path in sorted(pool.glob("*.parquet")):
        parquet = pq.ParquetFile(path)
        columns = [
            c
            for c in ("id", "doi", "primary_location", "open_access")
            if c in parquet.schema_arrow.names
        ]
        for batch in parquet.iter_batches(columns=columns):
            for row in batch.to_pylist():
                ident = row.get("id") or ""
                doi = clean_doi(row.get("doi") or "")
                if doi and "doi:" + doi in wanted:
                    found.setdefault("doi:" + doi, ident)
                text = (row.get("primary_location") or "") + (
                    row.get("open_access") or ""
                )
                if "10419/" in text or "i4rdps" in text or "I4R-DP" in text:
                    for key in identity_keys(text) & wanted:
                        found.setdefault(key, ident)
    return found


def pool_record(record: dict) -> dict:
    abstract = record["abstract"]
    if record["doi_o"]:
        abstract += f"\nOriginal bibliographic hint (Crossref exact title): {record['original_title']}; DOI: {record['doi_o']}."
    return {
        "id": record.get("pool_id") or f"i4r:{record['number']}",
        "doi": record["doi"],
        "title": record["title"],
        "display_name": record["title"],
        "publication_year": record["year"],
        "type": "working-paper",
        "source": "i4r",
        "authorships": json.dumps(
            [{"author": {"display_name": name}} for name in record["authors"]]
        ),
        "primary_location": json.dumps(
            {
                "landing_page_url": record["url"],
                "source": {"display_name": "I4R Discussion Paper Series"},
            }
        ),
        "open_access": json.dumps({"oa_url": record["pdf_url"] or record["url"]}),
        "concepts": "[]",
        "abstract_text": abstract,
        "hit_token_title": False,
        "hit_token_abstract": False,
        "hit_concept": False,
    }


def harvest(
    cache: Path, pool: Path, flora_csv: Path, report: Path, write: bool = False
) -> dict:
    """Inventory all DPs; ingest only unseen records absent from published FLoRA."""
    papers = catalogue(cache)
    flora_rows = _flora_rows(flora_csv.read_text(encoding="utf-8-sig"))
    flora_keys = [identity_keys(row.get("url_r") or "") for row in flora_rows]
    known = set().union(*flora_keys)
    flora_dois = {clean_doi(row.get("doi_r", "")) for row in flora_rows} - {""}

    def detail(item: tuple[int, str]) -> dict:
        """Validate cached citation metadata before it becomes a checkpoint input."""
        number, url = item
        return parse_detail(
            number, fetch(url, cache, validate=lambda text: parse_detail(number, text))
        )

    with ThreadPoolExecutor(max_workers=I4R_WORKERS) as executor:
        records = list(executor.map(detail, papers.items()))
    wanted = set().union(
        *(
            identity_keys(r["url"]) | {f"dp:{r['number']}", "doi:" + r["doi"]}
            for r in records
        )
    )
    pool_ids = existing_pool_ids(pool, wanted)
    rows, matched_rows = [], set()
    if write:
        pool.mkdir(parents=True, exist_ok=True)
    pending = []
    for record in records:
        number = record["number"]
        keys = identity_keys(record["url"]) | {f"dp:{number}"}
        record["in_flora"] = bool(keys & known or record["doi"] in flora_dois)
        if record["in_flora"]:
            matching = [
                i
                for i, k in enumerate(flora_keys)
                if keys & k
                or (
                    record["doi"]
                    and clean_doi(flora_rows[i].get("doi_r", "")) == record["doi"]
                )
            ]
            matched_rows.update(matching)
            originals = {
                clean_doi(flora_rows[i].get("doi_o", "")) for i in matching
            } - {""}
            record["original_dois"] = sorted(originals)
            record["original_status"] = "published_flora"
            if len(originals) == 1:
                record["doi_o"] = originals.pop()
        shard = pool / f"curated-i4r-{number:06d}.parquet"
        record["in_pool"] = shard.exists()
        if record["in_pool"] and not record["in_flora"]:
            hint = (pq.read_metadata(shard).metadata or {}).get(b"original_hint")
            if hint:
                record.update(json.loads(hint))
        identifiers = sorted(
            keys | ({"doi:" + record["doi"]} if record["doi"] else set())
        )
        record["pool_id"] = next(
            (pool_ids[k] for k in identifiers if k in pool_ids), f"i4r:{number}"
        )
        if not record["in_flora"] and not record["in_pool"]:
            pending.append(record)
        rows.append(record)

    def original(record: dict) -> None:
        """Fetch original-title hints only for new, unpublished records."""
        resolve_original(record, cache)

    with ThreadPoolExecutor(max_workers=I4R_WORKERS) as executor:
        list(executor.map(original, pending))
    if write:
        for record in pending:
            shard = pool / f"curated-i4r-{record['number']:06d}.parquet"
            tmp = shard.with_suffix(".tmp")
            schema = _POOL_SCHEMA.with_metadata(
                {
                    b"curated_source": b"i4r",
                    b"catalogue": SERIES.encode(),
                    b"repec_handle": record["handle"].encode(),
                    b"original_hint": json.dumps(
                        {
                            k: record[k]
                            for k in ("doi_o", "original_title", "original_status")
                        }
                    ).encode(),
                }
            )
            pq.write_table(
                pa.Table.from_pylist([pool_record(record)], schema=schema), tmp
            )
            _durable_replace(tmp, shard)
    result = {
        "series": SERIES,
        "flora_csv_sha256": hashlib.sha256(flora_csv.read_bytes()).hexdigest(),
        "total": len(rows),
        "in_flora": sum(r["in_flora"] for r in rows),
        "matched_flora_rows": len(matched_rows),
        "new": sum(not r["in_flora"] for r in rows),
        "to_ingest": sum(not r["in_flora"] and not r["in_pool"] for r in rows),
        "doi_o_hints": sum(
            r["original_status"] == "crossref_exact_title_hint" for r in rows
        ),
        "doi_o_from_flora": sum(
            r["original_status"] == "published_flora" for r in rows
        ),
        "unresolved_originals": sum(
            not r["doi_o"] and not r.get("original_dois") for r in rows
        ),
        "write": write,
        "records": rows,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report_tmp = report.with_suffix(".tmp")
    report_tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    _durable_replace(report_tmp, report)
    return result


def main() -> None:
    """Run a locked inventory or incremental pool import and report its accounting."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write new pool shards; default is a dry run",
    )
    parser.add_argument("--cache", type=Path, default=CACHE_DIR / "i4r")
    parser.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument(
        "--flora-csv",
        type=Path,
        help="Otherwise fetch current fred-data output/flora.csv",
    )
    parser.add_argument("--report", type=Path, default=Path("logs/i4r.json"))
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    with FileLock(str(args.cache / "harvest.lock"), timeout=0):
        flora = args.flora_csv or args.cache / "flora.csv"
        if args.flora_csv is None:
            flora.write_text(
                fetch(FLORA_URL, args.cache, refresh=True, validate=_flora_rows)
            )
        result = harvest(args.cache, args.pool, flora, args.report, args.write)
    print(json.dumps({k: v for k, v in result.items() if k != "records"}, indent=2))


if __name__ == "__main__":
    main()
