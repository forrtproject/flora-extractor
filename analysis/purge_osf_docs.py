"""Purge every document the acquisition waterfall took from OSF file storage.

Ladder 25 demotes OSF plan files: the tier that supplied a row's document is replayed
from the sidecar beside the file (`pdfsrc_<key>.json`), so a document already on disk
is served again whatever the new ranking says. The re-ranking therefore only reaches
the rows whose `osf_files` document is GONE — sidecar, file and parse entry alike:

  a. the sidecar        `cache/pdfs/pdfsrc_<key>.json`   (source == "osf_files")
  b. the document       `cache/pdfs/<key>.pdf` / `.docx`
  c. the parse entries  `cache/parse/parse_<key>_v<extraction version>.json`

(c) matters on its own: leaving it behind serves the OLD document's parsed text for
the NEW document the next run acquires, and nothing downstream can tell.

Keys, and why (c) is not always derivable:

  * The sidecar is written under `doi_r or url_r` — call its md5 H, which is the
    `<key>` in every path above.
  * The document is written under `doi_r or <download url>`. For a row with a DOI
    those are the same string, so `H.pdf` is the file. For a DOI-less row they are
    not, so the download URL recorded IN the sidecar is hashed as a second candidate.
  * The parse entry is keyed on the row's identity — `doi_r`, else `primary_key()`'s
    `oa:` / `url:` / `title:` form (`_cache_id` in `extract/run_extract.py`). Only the
    DOI case hashes to H. For the rest the sidecar does not carry the identity, and
    the entry cannot be found; the run counts those instead of pretending otherwise.

Dry run by default. `--apply` deletes.

    .venv/bin/python -m analysis.purge_osf_docs
    .venv/bin/python -m analysis.purge_osf_docs --apply
"""

import argparse
import json
import sys
from pathlib import Path

from shared.config import PARSE_CACHE_DIR, PDF_CACHE_DIR
from shared.utils import cache_key

SOURCE = "osf_files"
DOCUMENT_SUFFIXES = (".pdf", ".docx")


def sidecars(pdf_dir: Path) -> list[tuple[Path, dict]]:
    """Every `pdfsrc_*.json` in *pdf_dir* whose source is `osf_files`, with its record.

    An unreadable sidecar is skipped rather than deleted: this run may not be the only
    thing writing that directory, and a file it cannot read is a file it cannot judge.
    """
    found = []
    for path in sorted(pdf_dir.glob("pdfsrc_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  ! unreadable, skipped: {path.name} ({exc})")
            continue
        if isinstance(record, dict) and str(record.get("source") or "") == SOURCE:
            found.append((path, record))
    return found


def victims(sidecar: Path, record: dict, pdf_dir: Path,
            parse_dir: Path) -> tuple[list[Path], bool]:
    """`(the files this sidecar accounts for, whether a parse entry was found)`.

    The parse flag is reported rather than assumed: an absent `parse_<H>.json` is
    either a row that was never parsed or a row whose parse is filed under an identity
    this sidecar cannot reconstruct, and the two are indistinguishable from here.
    """
    key = sidecar.name[len("pdfsrc_"):-len(".json")]
    keys = [key]
    url = str(record.get("url") or "")
    if url:
        keys.append(cache_key(url))

    paths = [sidecar]
    for candidate in keys:
        for suffix in DOCUMENT_SUFFIXES:
            document = pdf_dir / f"{candidate}{suffix}"
            if document.exists():
                paths.append(document)

    # Every extraction version this row has been parsed under, plus the unversioned
    # name older entries carry: the document is going, so no parse of it may stay.
    parses = sorted(set(parse_dir.glob(f"parse_{key}.json"))
                    | set(parse_dir.glob(f"parse_{key}_v*.json")))
    paths.extend(parses)
    return paths, bool(parses)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analysis.purge_osf_docs",
        description="Delete every osf_files document, its sidecar and its parse "
                    "entry, so the acquisition waterfall re-ranks OSF storage files.")
    parser.add_argument("--apply", action="store_true",
                        help="Delete. Without it, print what would be deleted.")
    parser.add_argument("--pdf-dir", type=Path, default=PDF_CACHE_DIR)
    parser.add_argument("--parse-dir", type=Path, default=PARSE_CACHE_DIR)
    parser.add_argument("--verbose", action="store_true",
                        help="Print every file, not just the counts.")
    args = parser.parse_args(argv)

    records = sidecars(args.pdf_dir)
    print(f"{len(records):,} sidecar(s) with source={SOURCE} in {args.pdf_dir}")

    counts = {"sidecar": 0, "document": 0, "parse": 0}
    no_document = 0
    no_parse = 0
    bytes_freed = 0
    for sidecar, record in records:
        paths, parsed = victims(sidecar, record, args.pdf_dir, args.parse_dir)
        documents = [p for p in paths if p.suffix in DOCUMENT_SUFFIXES]
        if not documents:
            no_document += 1
        if not parsed:
            no_parse += 1
        for path in paths:
            kind = ("sidecar" if path.name.startswith("pdfsrc_")
                    else "parse" if path.name.startswith("parse_") else "document")
            counts[kind] += 1
            try:
                bytes_freed += path.stat().st_size
            except OSError:
                pass
            if args.verbose:
                print(f"  {'delete' if args.apply else 'would delete'} {path}")
            if args.apply:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"  ! delete failed: {path} ({exc})")

    verb = "deleted" if args.apply else "would delete"
    print(f"  {verb}: {counts['sidecar']:,} sidecar(s), "
          f"{counts['document']:,} document(s), {counts['parse']:,} parse entr(ies) "
          f"— {bytes_freed / 1e6:,.1f} MB")
    print(f"  {no_document:,} sidecar(s) with no document on disk")
    print(f"  {no_parse:,} sidecar(s) with no parse entry found — either never "
          "parsed, or keyed on a row identity (oa:/url:/title:) the sidecar does "
          "not carry")
    if not args.apply:
        print("\n  dry run — nothing was deleted. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
