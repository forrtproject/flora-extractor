"""
openalex_api_example.py — Standalone example of calling the OpenAlex API.

Run directly:  python misc/openalex_api_example.py

This shows how to search for replication papers and fetch referenced works.
It does NOT depend on any other module in this repo — copy and run anywhere.
"""
import time
import requests

EMAIL = "you@example.com"  # replace with your email
HEADERS = {"User-Agent": f"FLoRA-Extractor/1.0 (mailto:{EMAIL})"}


def search_replications(query: str = "direct replication", per_page: int = 5) -> list[dict]:
    """Search OpenAlex for papers containing a replication phrase."""
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "filter": "type:article",
        "per-page": per_page,
        "select": "id,doi,title,publication_year,authorships,abstract_inverted_index,open_access",
        "mailto": EMAIL,
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


def fetch_referenced_works(openalex_id: str) -> list[dict]:
    """Fetch metadata for all works referenced by a given OpenAlex ID."""
    # Step 1: get the work's referenced_works list
    bare_id = openalex_id.replace("https://openalex.org/", "")
    r = requests.get(
        f"https://api.openalex.org/works/{bare_id}",
        headers=HEADERS,
        params={"mailto": EMAIL},
        timeout=30,
    )
    r.raise_for_status()
    work = r.json()
    ref_ids = work.get("referenced_works", [])
    print(f"  Found {len(ref_ids)} referenced works")

    # Step 2: batch-fetch metadata (50 IDs per request)
    results = []
    bare_refs = [rid.replace("https://openalex.org/", "") for rid in ref_ids]
    for i in range(0, len(bare_refs), 50):
        batch = bare_refs[i:i+50]
        time.sleep(0.1)  # rate limit
        r2 = requests.get(
            "https://api.openalex.org/works",
            headers=HEADERS,
            params={
                "filter": f"openalex_id:{'|'.join(batch)}",
                "per-page": "50",
                "select": "id,doi,title,publication_year,authorships",
                "mailto": EMAIL,
            },
            timeout=30,
        )
        if r2.status_code == 200:
            results.extend(r2.json().get("results", []))
    return results


if __name__ == "__main__":
    print("=== Searching for replication papers ===")
    papers = search_replications("direct replication of", per_page=3)
    for p in papers:
        print(f"  DOI: {p.get('doi', 'N/A')}")
        print(f"  Title: {p.get('title', 'N/A')}")
        print(f"  Year: {p.get('publication_year', 'N/A')}")
        print()

    if papers:
        first = papers[0]
        oa_id = first.get("id", "")
        print(f"=== Fetching referenced works for: {first.get('title', '')[:60]} ===")
        refs = fetch_referenced_works(oa_id)
        for ref in refs[:5]:
            print(f"  {ref.get('title', 'N/A')} ({ref.get('publication_year', '?')})")
