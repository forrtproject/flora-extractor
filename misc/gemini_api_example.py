"""
gemini_api_example.py — Standalone example of calling the Gemini API.

Run directly:  python misc/gemini_api_example.py

This shows how to call Gemini with JSON output mode (used throughout this project).
It does NOT depend on any other module in this repo — copy and run anywhere.

Get a free API key at: https://aistudio.google.com
"""
import json
import os
import requests

GEMINI_MODEL = "gemini-3-flash-preview"
API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_KEY_HERE")


def call_gemini_json(prompt: str) -> dict | None:
    """
    Call Gemini and get a JSON response.
    responseMimeType=application/json forces valid JSON output.
    Returns parsed dict or None on failure.
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1024,
        },
    }
    try:
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        body = r.json()
        candidates = body.get("candidates", [])
        if not candidates:
            print(f"  No candidates. Block reason: {body.get('promptFeedback', {}).get('blockReason')}")
            return None
        text = candidates[0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except Exception as e:
        print(f"  Gemini call failed: {e}")
        return None


# ── Example 1: Is this a replication? ────────────────────────────────────────

FILTER_PROMPT = """
You are classifying academic papers. Determine whether the following paper is a replication study.

A replication study:
- Explicitly states it is replicating a SPECIFIC previous study
- Collects NEW data to test whether a prior finding holds
- Names the specific original study it replicates

Title: "A Direct Replication of Smith et al. (2018): Does Money Motivate?"
Abstract: "We attempted to replicate the findings of Smith, Jones & Brown (2018) who found that monetary incentives increased task performance. We collected new data from 200 participants using the same paradigm."

Respond with ONLY this JSON:
{
  "is_replication": true or false,
  "is_reproduction": true or false,
  "filter_status": "replication" or "reproduction" or "false_positive" or "needs_review",
  "filter_evidence": "the phrase from the text that indicates this",
  "filter_confidence": 0.0 to 1.0
}
"""

# ── Example 2: What is the outcome? ──────────────────────────────────────────

OUTCOME_PROMPT = """
You are extracting the replication outcome from an academic abstract.

Abstract: "We successfully replicated the main finding of Smith et al. (2018).
Participants in the incentive condition significantly outperformed controls
(d = 0.42, p < .001), consistent with the original effect (d = 0.45)."

Respond with ONLY this JSON:
{
  "outcome": "success" or "failure" or "mixed" or "uninformative" or "pending",
  "outcome_phrase": "the exact quote that indicates the outcome",
  "outcome_confidence": 0.0 to 1.0
}
"""


if __name__ == "__main__":
    print("=== Example 1: Filter classification ===")
    result = call_gemini_json(FILTER_PROMPT)
    if result:
        print(json.dumps(result, indent=2))

    print()
    print("=== Example 2: Outcome extraction ===")
    result = call_gemini_json(OUTCOME_PROMPT)
    if result:
        print(json.dumps(result, indent=2))
