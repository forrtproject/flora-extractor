"""One test per seam in the documentation API: it must describe the CODE, not a copy.

The whole point of generating these pages is that they cannot drift from the pipeline.
Each test therefore pins the generation, not a snapshot of today's text.
"""
import pytest

from validate.app import create_app


@pytest.fixture
def client():
    return create_app({"TESTING": True}).test_client()


def test_docs_page_renders(client):
    assert client.get("/docs").status_code == 200


def test_every_prompt_the_pipeline_can_send_is_listed_with_its_version(client):
    """PROMPT_NAMES is the registry the cache keys off; the page must show all of it."""
    from shared.prompts import PROMPT_NAMES

    payload = client.get("/api/docs/prompts").get_json()
    listed = {p["name"] for p in payload["prompts"]}

    assert listed == set(PROMPT_NAMES)
    assert all(p["version"] for p in payload["prompts"])


def test_a_builder_carries_the_fragments_it_splices(client):
    """A builder's own source is assembly; the prompt is the text it splices in.

    Showing only the assembly would report build_classify_prompt as ~6 lines when the
    prompt it sends is over a hundred.
    """
    payload = client.get("/api/docs/prompts").get_json()
    classify = next(p for p in payload["prompts"]
                    if p["name"] == "build_classify_prompt")

    assert classify["kind"] == "builder"
    assert "_CLASSIFY_PROMPT" in {f["name"] for f in classify["fragments"]}
    assert classify["lines"] > 50


def test_every_rule_spec_is_listed_in_precedence_order(client):
    """The engine loads filter/spec/*.json; aliases and conventions are data, not rules."""
    from pathlib import Path

    from shared.config import BASE_DIR

    on_disk = {p.stem for p in (BASE_DIR / "filter" / "spec").glob("*.json")
               if p.stem not in ("aliases", "conventions")}
    payload = client.get("/api/docs/rules").get_json()

    assert {r["id"] for r in payload["rules"]} == on_disk
    precedences = [r["precedence"] for r in payload["rules"]
                   if r["precedence"] is not None]
    assert precedences == sorted(precedences)


def test_the_pile_and_pending_vocabulary_comes_from_conventions_json(client):
    """`pending` is the pile a reader most needs explained; its reasons are declared."""
    payload = client.get("/api/docs/rules").get_json()
    conventions = payload["conventions"]

    assert "pending" in conventions["piles"]
    assert "no_text" in conventions["pending_reasons"]


def test_the_ladder_reports_its_live_version_and_resolved_methods(client):
    from extract.link_original import EXTRACT_LADDER_VERSION
    from shared.schema import RESOLVED_LINK_METHODS

    payload = client.get("/api/docs/ladder").get_json()

    assert payload["version"] == EXTRACT_LADDER_VERSION
    assert set(payload["resolved_link_methods"]) == set(RESOLVED_LINK_METHODS)
    assert payload["revisions"], "the ladder's revision record should not be empty"


def test_architecture_reads_each_module_docstring(client):
    payload = client.get("/api/docs/architecture").get_json()
    packages = {p["package"] for p in payload["packages"]}

    assert {"search", "filter", "extract", "validate", "shared"} <= packages
    shared = next(p for p in payload["packages"] if p["package"] == "shared")
    prompts = next(m for m in shared["modules"] if m["name"] == "prompts")
    assert prompts["summary"], "shared/prompts.py has a docstring to report"


def test_check_offers_only_outcome_values_the_pipeline_can_write(client):
    """The options were hardcoded and six of ten matched no row.

    It offered `success`/`failure` where the pipeline writes `successful`/`failed`,
    so the two largest categories filtered to nothing. Rendering the closed
    vocabulary means an option cannot drift out of the data.
    """
    import re

    from shared.schema import OUTCOME_VALUES

    html = client.get("/check").data.decode("utf-8")
    offered = set(re.findall(r'data-ms-val="outcome" value="([^"]+)"', html))

    assert offered == set(OUTCOME_VALUES)


def test_check_prefers_the_csv_when_the_parquet_mirror_is_older(tmp_path, monkeypatch):
    """A mirror older than the file it mirrors is a different dataset, not a cache.

    Served unconditionally, a 26-day-old mirror had Check answering every query from
    1,881 rows in a superseded vocabulary while the CSV held 3,147.
    """
    import os

    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    import validate.routes.check as check

    csv = tmp_path / "extracted.csv"
    pd.DataFrame({"doi_r": ["10.1/new"], "outcome": ["failed"]}).to_csv(csv, index=False)
    mirror = tmp_path / "extracted.parquet"
    pq.write_table(pa.table({"doi_r": ["10.1/stale"], "outcome": ["failure"]}), mirror)
    os.utime(mirror, (1, 1))                      # older than the CSV

    monkeypatch.setitem(check._STAGES, "extracted", csv)
    monkeypatch.setattr(check, "DASHBOARD_DIR", tmp_path)

    rows = check._read_one_stage("extracted", {"outcomes": ["failed"]})

    assert list(rows["doi_r"]) == ["10.1/new"]


def test_check_has_controls_for_the_flags_its_api_accepts(client):
    """The API honoured no_doi/no_abstract, but the page had no control for them.

    Landing on /check?no_doi=1 therefore rendered, then rebuilt its own query
    WITHOUT the flag and returned all 3,147 rows — a filter link that silently
    did nothing.
    """
    html = client.get("/check").data.decode("utf-8")

    assert 'id="flagNoDoi"' in html
    assert 'id="flagNoAbstract"' in html
    assert "params.set('no_doi', '1')" in html
    assert "params.set('no_abstract', '1')" in html


def test_concerns_that_name_a_row_population_link_to_it(client):
    """A count naming a defect it cannot show is not actionable."""
    payload = client.get("/api/dashboard/concerns").get_json()
    by_id = {c["id"]: c for c in payload["concerns"]}

    assert by_id["blank_doi_r"]["check_url"] == "/check?no_doi=1"
    assert by_id["outcome_api_error"]["check_url"] == "/check?outcome=api_error"
    # State-of-the-pipeline concerns have no rows to list, so they carry no link.
    assert by_id["foreign_stats"]["check_url"] == ""
