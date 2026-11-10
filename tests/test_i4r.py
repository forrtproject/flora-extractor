"""The curated seam: catalogue, identity, routing, handoff and crash-safe resume."""

import json
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine.backends import eval_spec_rows
from filter.engine.route import route_batch
from filter.engine.spec import load_specs
from filter.engine.workids import work_id
from search import i4r
from search.snapshot_scan import _POOL_SCHEMA, _row_from_snapshot
from shared.schema import CANDIDATES_COLS, SOURCE_VALUES

DETAIL = """<meta name="handle" content="RePEc:zbw:i4rdps:30">
<meta name="citation_title" content="A comment on Smith (2020)">
<meta name="citation_author" content="Sangnier, Marc">
<meta name="citation_year" content="2023">
<a href="/scripts/redir.pf?u=https%3A%2F%2Fwww.econstor.eu%2Fbitstream%2F10419%2F270965%2F1%2FI4R-DP030.pdf;h=repec:zbw:i4rdps:30">PDF</a>"""


def test_paginated_catalogue_and_failure(tmp_path: Path) -> None:
    pages = [
        '<a href="30.htm">30</a><a href="default1.htm">Next</a>',
        '<a href="1.htm">1</a><a href="default1.htm">Same</a>',
    ]
    with patch.object(i4r, "fetch", side_effect=pages):
        assert set(i4r.catalogue(tmp_path)) == {1, 30}
    with (
        patch.object(i4r, "fetch", return_value="<html>Challenge</html>"),
        pytest.raises(ValueError),
    ):
        i4r.catalogue(tmp_path)
    with pytest.raises(ValueError):
        i4r.parse_detail(31, DETAIL)


def test_curated_routing_and_handoff() -> None:
    record = i4r.parse_detail(30, DETAIL)
    assert record["url"] == "https://hdl.handle.net/10419/270965"
    assert i4r.identity_keys(record["pdf_url"]) == {"dp:30", "hdl:270965"}
    assert i4r.identity_keys("https://ideas.repec.org/p/zbw/i4rdps/30.html") == {
        "dp:30"
    }
    assert work_id("i4r:30") == -30
    assert work_id("W30") == 30
    row = i4r.pool_record(record)
    # A repository-object DOI and no vocabulary/abstract must still reach screen.
    row["doi"] = "10.7910/dvn/example"
    batch = pa.RecordBatch.from_pylist([row], schema=_POOL_SCHEMA)
    specs = load_specs(Path(__file__).resolve().parents[1] / "filter" / "spec")
    routed = route_batch(specs, batch).to_pylist()[0]
    assert routed["pile"] == "screen_expensive"
    assert routed["rule_id"] == "curated-i4r"
    candidate = _row_from_snapshot(row, abstract="")
    assert set(candidate) == set(CANDIDATES_COLS)
    assert candidate["source"] in SOURCE_VALUES
    assert candidate["openalex_id_r"] == ""  # never invent an OpenAlex ID
    assert candidate["url_r"].endswith("I4R-DP030.pdf")
    curated = next(s for s in specs if s.id == "curated-i4r")
    assert eval_spec_rows(curated, [row, {**row, "source": ""}]) == [True, False]


def test_incremental_and_flora_handle_skip(tmp_path: Path) -> None:
    flora = tmp_path / "flora.csv"
    flora.write_text(
        "url_r,doi_r\nhttps://www.econstor.eu/bitstream/10419/270965/1/anything.pdf,\n"
    )
    with (
        patch.object(i4r, "catalogue", return_value={30: "detail"}),
        patch.object(i4r, "fetch", return_value=DETAIL),
    ):
        skipped = i4r.harvest(
            tmp_path / "cache",
            tmp_path / "pool",
            flora,
            tmp_path / "skip.json",
            write=True,
        )
        assert (skipped["total"], skipped["in_flora"], skipped["new"]) == (1, 1, 0)
        flora.write_text("url_r,doi_r\n")
        dry = i4r.harvest(
            tmp_path / "cache", tmp_path / "pool", flora, tmp_path / "dry.json"
        )
        assert dry["to_ingest"] == 1
        assert not list((tmp_path / "pool").glob("*.parquet"))
        first = i4r.harvest(
            tmp_path / "cache", tmp_path / "pool", flora, tmp_path / "first.json", True
        )
        second = i4r.harvest(
            tmp_path / "cache", tmp_path / "pool", flora, tmp_path / "second.json", True
        )
        assert (first["to_ingest"], second["to_ingest"]) == (1, 0)
        assert pq.read_table(next((tmp_path / "pool").glob("*.parquet"))).num_rows == 1
        assert json.loads((tmp_path / "second.json").read_text())["records"][0][
            "in_pool"
        ]


def test_snapshot_identity_is_reused(tmp_path: Path) -> None:
    flora = tmp_path / "flora.csv"
    flora.write_text("url_r,doi_r\n")
    pool = tmp_path / "pool"
    pool.mkdir()
    snapshot = i4r.pool_record(i4r.parse_detail(30, DETAIL))
    snapshot.update(id="https://openalex.org/W123", source="openalex_snapshot")
    pq.write_table(
        pa.Table.from_pylist([snapshot], schema=_POOL_SCHEMA), pool / "part-old.parquet"
    )
    with (
        patch.object(i4r, "catalogue", return_value={30: "detail"}),
        patch.object(i4r, "fetch", return_value=DETAIL),
    ):
        result = i4r.harvest(
            tmp_path / "cache", pool, flora, tmp_path / "report.json", True
        )
    assert result["records"][0]["pool_id"] == "https://openalex.org/W123"
    assert pq.read_table(pool / "curated-i4r-000030.parquet")["id"].to_pylist() == [
        "https://openalex.org/W123"
    ]


def test_original_hint_requires_exact_unique_title(tmp_path: Path) -> None:
    record = i4r.parse_detail(30, DETAIL)
    record["original_title"] = "The named original"
    with patch.object(
        i4r,
        "fetch",
        return_value=json.dumps(
            {
                "message": {
                    "items": [
                        {"title": ["Something similar"], "DOI": "10.1234/wrong"},
                        {
                            "title": ["The named original"],
                            "DOI": "https://doi.org/10.1234/right",
                        },
                    ]
                }
            }
        ),
    ):
        i4r.resolve_original(record, tmp_path)
    assert record["doi_o"] == "10.1234/right"
    assert "bibliographic hint" in i4r.pool_record(record)["abstract_text"]
    record["doi_o"] = ""
    with patch.object(i4r, "fetch", side_effect=i4r.requests.HTTPError("429")):
        i4r.resolve_original(record, tmp_path)
    assert record["doi_o"] == ""
    assert record["original_status"] == "crossref_unavailable_stage3_required"


def test_textless_i4r_reaches_tier_with_curated_provenance(tmp_path: Path) -> None:
    import duckdb

    from filter.engine.tiers import pile_works
    from shared.prescreen import prescreen_bypass

    row = i4r.pool_record(i4r.parse_detail(30, DETAIL))
    pq.write_table(
        pa.Table.from_pylist([row], schema=_POOL_SCHEMA), tmp_path / "curated.parquet"
    )
    with duckdb.connect() as con:
        con.execute(
            "CREATE TABLE routing(work_id BIGINT, release_id VARCHAR, pile VARCHAR)"
        )
        con.execute("INSERT INTO routing VALUES (-30, 'r', 'screen_expensive')")
        works = pile_works(con, "r", "screen_expensive", pool_dir=tmp_path)
    assert len(works) == 1
    assert works[0].work_id == -30
    assert works[0].source == "i4r"
    assert prescreen_bypass(works[0].title, works[0].abstract, works[0].source)


@pytest.mark.parametrize(
    "items,own_doi",
    [
        (
            [
                {"title": ["The named original"], "DOI": "10.1234/first"},
                {"title": ["The named original"], "DOI": "10.1234/second"},
            ],
            "",
        ),
        (
            [{"title": ["The named original"], "DOI": "https://doi.org/10.1234/self"}],
            "10.1234/self",
        ),
    ],
)
def test_ambiguous_and_self_originals_are_not_hints(
    tmp_path: Path, items, own_doi
) -> None:
    record = i4r.parse_detail(30, DETAIL)
    record.update(original_title="The named original", doi=own_doi)
    with patch.object(
        i4r, "fetch", return_value=json.dumps({"message": {"items": items}})
    ):
        i4r.resolve_original(record, tmp_path)
    assert record["doi_o"] == ""
    assert record["original_status"] == "stage3_required"


def test_free_text_is_not_a_flora_identifier(tmp_path: Path) -> None:
    import csv

    flora = tmp_path / "flora.csv"
    with flora.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["url_r", "doi_r", "title_r", "abstract_r", "bibtex_ref_r"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "url_r": "https://example.org/another-paper",
                "doi_r": "10.1234/another",
                "title_r": "Discussion of I4R-DP030",
                "abstract_r": "Cites 10419/270965",
                "bibtex_ref_r": "See https://econstor.eu/handle/10419/270965",
            }
        )
    with (
        patch.object(i4r, "catalogue", return_value={30: "detail"}),
        patch.object(i4r, "fetch", return_value=DETAIL),
    ):
        result = i4r.harvest(
            tmp_path / "cache", tmp_path / "pool", flora, tmp_path / "report.json"
        )
    assert result["in_flora"] == 0
    assert result["to_ingest"] == 1


def test_published_original_supersedes_a_shard_hint(tmp_path: Path) -> None:
    flora = tmp_path / "flora.csv"
    flora.write_text("url_r,doi_r,doi_o\n")
    pool = tmp_path / "pool"
    with (
        patch.object(i4r, "catalogue", return_value={30: "detail"}),
        patch.object(i4r, "fetch", return_value=DETAIL),
    ):
        i4r.harvest(
            tmp_path / "cache", pool, flora, tmp_path / "first.json", write=True
        )
        shard = pool / "curated-i4r-000030.parquet"
        table = pq.read_table(shard)
        metadata = dict(table.schema.metadata or {})
        metadata[b"original_hint"] = json.dumps(
            {
                "doi_o": "10.1234/stale",
                "original_title": "Stale target",
                "original_status": "crossref_exact_title_hint",
            }
        ).encode()
        pq.write_table(table.replace_schema_metadata(metadata), shard)
        flora.write_text(
            "url_r,doi_r,doi_o\nhttps://hdl.handle.net/10419/270965,,10.1234/published\n"
        )
        result = i4r.harvest(
            tmp_path / "cache", pool, flora, tmp_path / "second.json", write=True
        )
    assert result["records"][0]["doi_o"] == "10.1234/published"
    assert result["records"][0]["original_status"] == "published_flora"
    assert result["to_ingest"] == 0


def test_bad_detail_cache_is_evicted_and_retried(tmp_path: Path) -> None:
    import hashlib
    from unittest.mock import Mock

    url = i4r.SERIES + "30.htm"
    cached = tmp_path / (hashlib.sha256(url.encode()).hexdigest() + ".txt")
    response = Mock(text="<html>Challenge</html>")

    def validate(text: str) -> dict:
        return i4r.parse_detail(30, text)

    with (
        patch.object(i4r.requests, "get", return_value=response),
        patch.object(i4r, "throttle"),
    ):
        with pytest.raises(ValueError):
            i4r.fetch(url, tmp_path, validate=validate)
        assert not cached.exists()
        response.text = DETAIL
        assert i4r.fetch(url, tmp_path, validate=validate) == DETAIL
        cached.write_text("")  # a bad legacy cached body also recovers in this call
        assert i4r.fetch(url, tmp_path, validate=validate) == DETAIL
        assert cached.read_text() == DETAIL


@pytest.mark.parametrize(
    "body",
    [
        "<html>Challenge</html>",
        "{}",
        '{"message":{"items":null}}',
        '{"message":{"items":[{"title":"invalid"}]}}',
    ],
)
def test_bad_crossref_payload_does_not_block_harvesting(
    tmp_path: Path, body: str
) -> None:
    from unittest.mock import Mock

    record = i4r.parse_detail(30, DETAIL)
    record["original_title"] = "The named original"
    with (
        patch.object(i4r.requests, "get", return_value=Mock(text=body)),
        patch.object(i4r, "throttle"),
    ):
        i4r.resolve_original(record, tmp_path)
    assert record["doi_o"] == ""
    assert record["original_status"] == "crossref_unavailable_stage3_required"
    assert not list(tmp_path.glob("*.txt"))


def test_checkpoint_syncs_file_before_rename_and_directory_after(
    tmp_path: Path,
) -> None:
    import stat

    tmp, final = tmp_path / "shard.tmp", tmp_path / "shard.parquet"
    tmp.write_bytes(b"complete")
    events = []
    replace = Path.replace

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(i4r.os.fstat(fd).st_mode) else "file")

    def publish(source, destination):
        events.append("rename")
        return replace(source, destination)

    with (
        patch.object(i4r.os, "fsync", side_effect=sync),
        patch.object(Path, "replace", publish),
    ):
        i4r._durable_replace(tmp, final)
    assert events == ["file", "rename", "directory"]
    assert final.read_bytes() == b"complete"


@pytest.mark.parametrize("body", ["<html>Challenge</html>", "title_r\nA report\n"])
def test_invalid_flora_input_cannot_mark_everything_new(
    tmp_path: Path, body: str
) -> None:
    flora = tmp_path / "flora.csv"
    flora.write_text(body)
    with (
        patch.object(i4r, "catalogue", return_value={30: "detail"}),
        pytest.raises(ValueError, match="url_r and doi_r"),
    ):
        i4r.harvest(
            tmp_path / "cache", tmp_path / "pool", flora, tmp_path / "report.json"
        )
    assert not (tmp_path / "report.json").exists()


def test_flora_doi_match_is_separate_from_url_match(tmp_path: Path) -> None:
    flora = tmp_path / "flora.csv"
    flora.write_text(
        "url_r,doi_r,doi_o\n,https://doi.org/10.1234/report,10.1234/original\n"
    )
    detail = DETAIL + '<meta name="citation_doi" content="10.1234/report">'
    with (
        patch.object(i4r, "catalogue", return_value={30: "detail"}),
        patch.object(i4r, "fetch", return_value=detail),
    ):
        result = i4r.harvest(
            tmp_path / "cache", tmp_path / "pool", flora, tmp_path / "report.json"
        )
    assert result["in_flora"] == 1
    assert result["records"][0]["doi_o"] == "10.1234/original"
