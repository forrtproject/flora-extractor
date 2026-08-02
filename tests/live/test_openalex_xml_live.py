"""Live test for the OpenAlex GROBID-XML full-text tier.

Run with: TEST_LIVE_API=1 python -m pytest tests/live/test_openalex_xml_live.py -v

COSTS MONEY: content.openalex.org is metered at X-RateLimit-Cost-USD 0.01 per
download against a $1/day allowance. The result is cached, so a repeat run of this
test is free; a cold run buys one work.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)


def test_openalex_xml_yields_real_text_and_references():
    """W2982430379 has stored GROBID XML: gzip on the wire, HTML-lowercased TEI inside."""
    from shared.config import OPENALEX_API_KEYS
    from shared.pdf_parsing import outcome_text
    from shared.pdf_sources import get_openalex_fulltext, openalex_xml_has_content

    if not OPENALEX_API_KEYS:
        pytest.skip("no OPENALEX_API_KEY — the content endpoint is key-gated")

    result = get_openalex_fulltext("W2982430379")
    assert result is not None
    assert openalex_xml_has_content(result)

    sections = result["sections"]
    assert len(sections["references"]) > 10
    assert any(r["title"] for r in sections["references"])
    assert len(sections["raw_text"]) > 10_000

    # Body text, structured: headings survive on their own lines and the
    # bibliography is not in it, so outcome_text() can find the discussion.
    raw = sections["raw_text"]
    assert "\n" in raw
    titles = [r["title"] for r in sections["references"] if len(r.get("title") or "") > 30]
    assert titles and not any(t in raw for t in titles)
    text, provenance = outcome_text(raw)
    assert provenance in ("discussion", "tail")
    assert not any(t in text for t in titles)
