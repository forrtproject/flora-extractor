"""
llm.py — LLM-based original study identification.

Primary model  : OpenRouter / Qwen (when OPENROUTER_API_KEY is set)
Fallback chain : Gemini → OpenAI

Public API:
    identify_original_with_llm(doi_r, study_r, abstract_r, pattern,
                                candidates, sections) → dict
"""
import base64
import json
import re
import textwrap
import time
from pathlib import Path
from typing import Optional

import requests

from .config import (
    GEMINI_API_KEYS, GEMINI_MODEL, GEMINI_LIGHT_MODEL, GEMINI_HEAVY_MODEL,
    LLM_CACHE_DIR, LLM_RATE_SEC,
    OPENAI_API_KEY, OPENAI_MODEL,
    OPENROUTER_API_KEY, OPENROUTER_HEAVY_MODEL,
    log,
)
from .utils import cache_key


# ── JSON parsing (handles markdown-fenced output) ─────────────────────────────

def _parse_llm_json(text: str) -> Optional[dict]:
    """
    Parse a JSON dict from an LLM response.
    Strips markdown fences (```json ... ```) before parsing.
    Falls back to extracting the first {...} block.
    """
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$",          "", text).strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


# ── Gemini (primary) ──────────────────────────────────────────────────────────

def call_gemini(prompt: str, model: str = GEMINI_MODEL) -> tuple[Optional[dict], str]:
    """
    Call Gemini via the REST API with responseMimeType=application/json.

    Rotates through all keys in GEMINI_API_KEYS when a 429 (quota exhausted)
    is returned — useful when running on multiple free-tier projects.
    Retries once on transient 500/503 within each key.

    Returns (result_dict_or_None, error_description).
    """
    if not GEMINI_API_KEYS:
        log.warning("No GEMINI_API_KEY set — skipping Gemini")
        return None, "no API keys configured"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature"     : 0.0,
            "responseMimeType": "application/json",
            "maxOutputTokens" : 8192,
            # Note: thinkingConfig is intentionally omitted.
            # Setting thinkingBudget:0 while also using responseMimeType:
            # application/json causes gemini-3-flash-preview to return a
            # non-200 error or empty candidates, so all calls fell through to
            # OpenAI.  Letting the model use its default thinking mode fixes this.
        },
    }

    last_error = "all keys exhausted"
    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        key_label = f"key {key_idx + 1}/{len(GEMINI_API_KEYS)}"

        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, timeout=90)

                if r.status_code == 429:
                    last_error = f"quota exhausted on {key_label} (429)"
                    print(f"  [Gemini] {key_label} quota exhausted (429) — "
                          f"{'trying next key' if key_idx + 1 < len(GEMINI_API_KEYS) else 'no more keys'}")
                    log.warning("Gemini quota exhausted on %s", key_label)
                    break   # break inner retry loop → next key

                if r.status_code == 404:
                    # Model not found — changing keys won't help; bail out immediately.
                    err_msg = r.json().get("error", {}).get("message", r.text[:200])
                    log.error(
                        "Gemini model not found: %s — update GEMINI_LIGHT_MODEL or "
                        "GEMINI_HEAVY_MODEL in .env. API said: %s", model, err_msg
                    )
                    return None, f"model not found: {model}"

                if r.status_code in (500, 503) and attempt == 0:
                    last_error = f"HTTP {r.status_code} on {key_label} (retrying)"
                    log.debug("Gemini transient %s on %s, retrying…", r.status_code, key_label)
                    time.sleep(3)
                    continue

                if r.status_code != 200:
                    last_error = f"HTTP {r.status_code} on {key_label}: {r.text[:200]}"
                    print(f"  [Gemini] {key_label} HTTP {r.status_code}: {r.text[:400]}")
                    log.warning("Gemini HTTP %s for %s model=%s", r.status_code, key_label, model)
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break   # non-retryable error on this key → try next

                body = r.json()
                if not body.get("candidates"):
                    blocked = body.get("promptFeedback", {}).get("blockReason", "unknown")
                    last_error = f"no candidates on {key_label} — blockReason={blocked}"
                    print(f"  [Gemini] {key_label} no candidates — blockReason={blocked}")
                    return None, last_error

                text   = body["candidates"][0]["content"]["parts"][0]["text"]
                result = _parse_llm_json(text)
                if result is not None:
                    if key_idx > 0:
                        log.info("Gemini succeeded on %s", key_label)
                    return result, ""

                last_error = f"non-JSON response on {key_label}: {text[:150]}"
                print(f"  [Gemini] {key_label} non-JSON response: {text[:200]}")
                log.warning("Gemini returned non-JSON: %.200s", text)
                return None, last_error

            except Exception as e:
                last_error = f"exception on {key_label} attempt {attempt+1}: {e}"
                print(f"  [Gemini] {key_label} exception (attempt {attempt+1}): {e}")
                log.warning("Gemini call failed on %s (attempt %d): %s", key_label, attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)

    return None, last_error


# ── OpenAI (fallback) ─────────────────────────────────────────────────────────

def call_openai(prompt: str, model: str = OPENAI_MODEL) -> tuple[Optional[dict], str]:
    """
    Call OpenAI chat completion with response_format=json_object.
    Returns (result_dict_or_None, error_description).
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — skipping OpenAI")
        return None, "OPENAI_API_KEY not configured"

    import openai
    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": ("You are a research methodology expert that identifies "
                              "original studies from replication papers. "
                              "Always respond with valid JSON only.")},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=1024,
        )
        result = _parse_llm_json(response.choices[0].message.content)
        return result, ("" if result else "response was not valid JSON")
    except Exception as e:
        print(f"  [OpenAI] Exception: {e}")
        log.warning("OpenAI call failed: %s", e)
        return None, f"exception: {e}"


# ── OpenRouter (OpenAI-compatible alternative LLMs) ──────────────────────────

def call_openrouter(prompt: str, model: str = "") -> tuple[Optional[dict], str]:
    """
    Call any model available on OpenRouter via the OpenAI-compatible API.

    model — OpenRouter model ID e.g. "qwen/qwen3-30b-a3b".
            Defaults to OPENROUTER_HEAVY_MODEL from config.

    Returns (result_dict_or_None, error_description).
    """
    if not OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY not configured"

    import openai
    client = openai.OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    use_model = model or OPENROUTER_HEAVY_MODEL
    try:
        response = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system",
                 "content": ("You are a research methodology expert that identifies "
                              "original studies from replication papers. "
                              "Always respond with valid JSON only.")},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=1024,
            temperature=0.0,
        )
        result = _parse_llm_json(response.choices[0].message.content)
        return result, ("" if result else "response was not valid JSON")
    except Exception as e:
        log.warning("OpenRouter call failed (model=%s): %s", use_model, e)
        return None, f"exception: {e}"


# ── Unified LLM router ───────────────────────────────────────────────────────

def call_llm(prompt: str, gemini_model: str = "") -> tuple[Optional[dict], str, str]:
    """
    Route a prompt through the configured provider chain and return the first
    successful result.

    Order: Gemini -> OpenAI -> OpenRouter (Qwen as last resort).

    gemini_model — Gemini model to use (defaults to GEMINI_LIGHT_MODEL).

    Returns (result_dict_or_None, model_used, error_description).
    model_used is the exact model string that answered, or "" if all providers failed.
    """
    from .config import GEMINI_LIGHT_MODEL as _LIGHT

    model = gemini_model or _LIGHT
    result, gemini_err = call_gemini(prompt, model=model)
    if result:
        return result, model, ""

    result, openai_err = call_openai(prompt)
    if result:
        return result, OPENAI_MODEL, ""

    if OPENROUTER_API_KEY:
        result, or_err = call_openrouter(prompt)
        if result:
            return result, OPENROUTER_HEAVY_MODEL, ""
        return None, "", f"Gemini: {gemini_err} | OpenAI: {openai_err} | OpenRouter: {or_err}"

    return None, "", f"Gemini: {gemini_err} | OpenAI: {openai_err}"


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_identification_prompt(study_r:        str,
                                 abstract_r:     str,
                                 pattern:        str,
                                 candidates:     list[dict],
                                 sections:       dict,
                                 pdf_url:        str = "",
                                 html_text:      str = "",
                                 validator_note: str = "") -> str:
    """Build the LLM identification prompt.

    pdf_url   — passed when PDF download failed but a URL was found; LLM may
                be able to retrieve it (Gemini supports URL grounding).
    html_text — extracted landing-page text used as a full-text substitute.
    """
    # Candidate block (unchanged)
    if candidates:
        def _authors_str(c: dict) -> str:
            authors = c.get("all_authors") or ([c["first_author"]] if c.get("first_author") else [])
            return ", ".join(authors) if authors else "unknown"

        cand_lines = [
            f"{i}. \"{c['title']}\" ({c['year']}, authors: {_authors_str(c)})\n"
            f"   DOI: {c['doi'] or 'unknown'}  |  OpenAlex: {c['openalex_id']}"
            for i, c in enumerate(candidates, 1)
        ]
        cand_text = "\n".join(cand_lines)
        cand_instruction = (
            f"Select the candidate number (1–{len(candidates)}) that is the "
            f"ORIGINAL STUDY being replicated.\n"
            f"If none is correct, set selected_candidate_number to null and fill "
            f"selected_doi/selected_title from the reference list below."
        )
    else:
        cand_text        = "(No candidates pre-identified — use reference list below.)"
        cand_instruction = (
            "No candidates were pre-identified. Use the reference list and full-text "
            "excerpts to find the original study. Set selected_candidate_number to null."
        )

    # Reference list — 30 entries is enough to find the original; keeps tokens low
    ref_lines = []
    for ref in sections.get("references", [])[:30]:
        authors = "; ".join(ref["authors"][:2])
        if len(ref["authors"]) > 2:
            authors += " et al."
        ref_lines.append(f"- {authors} ({ref['year'] or '?'}). {ref['title']}")
    ref_text = "\n".join(ref_lines) if ref_lines else "(no references extracted)"

    # Truncated snippets — prefer GROBID intro over abstract (less overlap with OpenAlex)
    abstract_snip = (abstract_r[:700] + "…") if len(abstract_r) > 700 else abstract_r
    intro_snip    = (sections.get("intro",   "") or "")[:600]

    # Include methods only when intro is short (avoid redundancy)
    methods_snip = ""
    if len(intro_snip) < 300:
        methods_snip = (sections.get("methods", "") or "")[:400]

    # HTML text fallback: use first 1000 chars as a substitute intro/body
    html_snip = ""
    if html_text and not intro_snip:
        html_snip = (html_text[:1000] + "…") if len(html_text) > 1000 else html_text

    # PDF URL block — only included when download failed but URL is known
    if pdf_url:
        pdf_url_block = (
            "\n    ---\n\n"
            "    ## Paper URL\n"
            "    The full text could not be downloaded, but the paper may be available at:\n"
            f"    {pdf_url}\n"
            "    If you can access this URL, use it to help identify the original study.\n"
        )
    else:
        pdf_url_block = ""

    validator_block = ""
    if validator_note and validator_note.strip():
        text = validator_note.strip()
        if text.startswith("⚠ FLoRA ANCHOR"):
            validator_block = text + "\n\n---\n\n"
        else:
            validator_block = (
                "⚠️ VALIDATOR FEEDBACK — A human reviewer marked the previous answer as INCORRECT:\n"
                + text
                + "\nUse this feedback to correct your selection. The previous candidate was wrong.\n\n---\n\n"
            )

    prompt = textwrap.dedent(f"""
    {validator_block}Identify the ORIGINAL STUDY that the replication paper below directly replicates.

    TITLE: {study_r}
    ABSTRACT: {abstract_snip or "(not available)"}
    CITED PATTERN: {pattern or "(not available)"}

    CANDIDATES:
    {cand_text}

    INTRODUCTION (from PDF):
    {intro_snip or html_snip or "(not available)"}
    {f"METHODS:{chr(10)}{methods_snip}" if methods_snip else ""}
    REFERENCE LIST (up to 50 entries):
    {ref_text}
    {pdf_url_block}
    TASK: {cand_instruction}

    KEY RULES:
    - Find the study named with phrases like "we replicated", "direct replication of",
      "we aimed to replicate" — NOT background citations.
    - Umbrella project papers (#EEGManyLabs, ManyLabs, PSA, StudySwap) are NEVER the
      original — find the specific experiment they ran.
    - When selecting a candidate number, leave selected_doi EMPTY — the candidate's
      verified DOI will be used. Only populate selected_doi for originals not in the list.

    Respond with ONLY this JSON:
    {{
      "selected_candidate_number": <integer or null>,
      "selected_doi": "<DOI only if not from candidate list, else empty>",
      "selected_title": "<full title>",
      "selected_year": <year or null>,
      "selected_first_author": "<surname>",
      "confidence": "<high|medium|low>",
      "evidence": "<1-2 sentence quote from the paper>",
      "reasoning": "<why other candidates were ruled out>"
    }}
    """).strip()

    return prompt


# ── Main dispatcher ───────────────────────────────────────────────────────────

def identify_original_with_llm(doi_r:          str,
                                 study_r:        str,
                                 abstract_r:     str,
                                 pattern:        str,
                                 candidates:     list[dict],
                                 sections:       dict,
                                 pdf_url:        str = "",
                                 html_text:      str = "",
                                 validator_note: str = "") -> dict:
    """
    Identify the original study via LLM.

    pdf_url   — URL to include in prompt when PDF download failed.
    html_text — extracted landing-page text as full-text substitute.

    Order: OpenRouter/Qwen (primary when OPENROUTER_API_KEY set) → Gemini → OpenAI.
    Successful results are cached in LLM_CACHE_DIR.
    """
    cache_file = LLM_CACHE_DIR / f"llm_{cache_key(doi_r)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            cached = json.load(fh)
        cached.setdefault("llm_source", "cache")
        cached.setdefault("llm_prompt", "")
        cached.setdefault("llm_error",  "")
        return cached

    prompt     = build_identification_prompt(study_r, abstract_r, pattern,
                                             candidates, sections,
                                             pdf_url=pdf_url,
                                             html_text=html_text,
                                             validator_note=validator_note)
    result     = None
    llm_source = "none"
    llm_model  = ""
    llm_error  = ""
    gemini_err = ""
    openai_err = ""
    or_err     = ""

    # Primary: Gemini
    result, gemini_err = call_gemini(prompt, model=GEMINI_HEAVY_MODEL)
    if result:
        llm_source = "gemini"
        llm_model  = GEMINI_HEAVY_MODEL
        time.sleep(LLM_RATE_SEC)

    # Fallback 1: OpenAI
    if not result:
        result, openai_err = call_openai(prompt)
        if result:
            llm_source = "openai"
            llm_model  = OPENAI_MODEL
            time.sleep(LLM_RATE_SEC)

    # Fallback 2: OpenRouter (Qwen)
    if not result and OPENROUTER_API_KEY:
        result, or_err = call_openrouter(prompt)
        if result:
            llm_source = "openrouter"
            llm_model  = OPENROUTER_HEAVY_MODEL
            time.sleep(LLM_RATE_SEC)

    if not result:
        parts = [f"Gemini: {gemini_err}", f"OpenAI: {openai_err}"]
        if OPENROUTER_API_KEY:
            parts.append(f"OpenRouter: {or_err}")
        llm_error = " | ".join(parts)

    _empty = {
        "resolved"          : False,
        "resolution_method" : "llm_failed",
        "resolved_doi_o"    : "",
        "resolved_title_o"  : "",
        "resolved_year_o"   : None,
        "resolved_author_o" : "",
        "resolution_score"  : 0.0,
        "llm_source"        : "none",
        "llm_model"         : "",
        "llm_confidence"    : "",
        "llm_evidence"      : "",
        "llm_reasoning"     : "",
        "llm_prompt"        : prompt,
        "llm_error"         : llm_error,
    }

    if not result:
        return _empty

    cand_num       = result.get("selected_candidate_number")
    resolved_doi   = (result.get("selected_doi")          or "").strip()
    resolved_title = (result.get("selected_title")        or "").strip()
    resolved_year  = result.get("selected_year")
    resolved_auth  = (result.get("selected_first_author") or "").strip()

    if cand_num is not None:
        try:
            idx = int(cand_num) - 1
            if 0 <= idx < len(candidates):
                c = candidates[idx]
                # Prefer the candidate's verified OpenAlex DOI over any DOI the LLM
                # may have hallucinated — only use resolved_doi if no candidate DOI exists.
                resolved_doi   = c.get("doi", "") or resolved_doi
                resolved_title = resolved_title or c.get("title",        "")
                resolved_year  = resolved_year  or c.get("year")
                resolved_auth  = resolved_auth  or c.get("first_author", "")
        except (ValueError, TypeError):
            pass

    resolved = bool(resolved_title)

    confidence_map = {"high": 1.0, "medium": 0.6, "low": 0.3}
    conf_str   = result.get("confidence", "low")
    conf_score = confidence_map.get(conf_str, 0.3)

    output = {
        "resolved"          : resolved,
        # llm_no_target: LLM ran successfully but concluded no identifiable original exists.
        # Distinct from llm_failed (all API calls errored) and llm_fulltext (original found).
        "resolution_method" : f"llm_{llm_source}" if resolved else "llm_no_target",
        "resolved_doi_o"    : resolved_doi,
        "resolved_title_o"  : resolved_title,
        "resolved_year_o"   : resolved_year,
        "resolved_author_o" : resolved_auth,
        "resolution_score"  : conf_score,
        "llm_source"        : llm_source,
        "llm_model"         : llm_model,
        "llm_confidence"    : conf_str,
        "llm_evidence"      : result.get("evidence",  ""),
        "llm_reasoning"     : result.get("reasoning", ""),
        "llm_prompt"        : prompt,
        "llm_response"      : json.dumps(result, ensure_ascii=False) if result else "",
        "llm_error"         : "",
    }

    if resolved:
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)

    return output


# ── Gemini with image parts (for PDF reference-page parsing) ──────────────────

def call_gemini_with_images(prompt: str,
                             image_b64_list: list[dict],
                             model: str = GEMINI_MODEL) -> Optional[dict]:
    """
    Call Gemini with inline image parts (base64 PNG/JPEG).

    image_b64_list: [{"mime_type": "image/png", "data": "<base64>"}]

    Requires PyMuPDF (fitz) for rendering — callers must catch ImportError.
    """
    if not GEMINI_API_KEYS:
        return None

    parts: list[dict] = [{"text": prompt}]
    for img in image_b64_list:
        parts.append({"inline_data": {"mime_type": img["mime_type"], "data": img["data"]}})

    payload = {
        "contents"       : [{"parts": parts}],
        "generationConfig": {
            "temperature"     : 0.0,
            "responseMimeType": "application/json",
            "maxOutputTokens" : 4096,
        },
    }

    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        try:
            r = requests.post(url, json=payload, timeout=120)
            if r.status_code == 429:
                continue
            if r.status_code != 200:
                log.warning("Gemini image call HTTP %s", r.status_code)
                continue
            body = r.json()
            if not body.get("candidates"):
                return None
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return _parse_llm_json(text)
        except Exception as e:
            log.warning("Gemini image call failed: %s", e)

    return None


# ── Gemini with inline PDF (for direct PDF reference extraction) ──────────────

def call_gemini_with_pdf(prompt: str,
                          pdf_bytes: bytes,
                          model: str = GEMINI_MODEL) -> Optional[dict]:
    """
    Call Gemini with an inline PDF payload.

    Uses MEDIA_RESOLUTION_LOW to minimise token cost: for native-text PDFs,
    Gemini reads the embedded text directly (not billed as image tokens); for
    scanned PDFs it applies lower-resolution OCR.  Max supported: 50 MB / 1 000 pages.

    Returns a parsed dict or None.
    """
    if not GEMINI_API_KEYS:
        return None

    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
            ],
        }],
        "generationConfig": {
            "temperature"     : 0.0,
            "responseMimeType": "application/json",
            "maxOutputTokens" : 4096,
            "mediaResolution" : "MEDIA_RESOLUTION_LOW",
        },
    }

    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        for attempt in range(2):
            try:
                r = requests.post(url, json=payload, timeout=45)
                if r.status_code == 429:
                    break
                if r.status_code in (500, 503) and attempt == 0:
                    time.sleep(3)
                    continue
                if r.status_code != 200:
                    log.warning("Gemini PDF call HTTP %s on key %d", r.status_code, key_idx + 1)
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break
                body = r.json()
                if not body.get("candidates"):
                    return None
                text = body["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_llm_json(text)
            except Exception as e:
                log.warning("Gemini PDF call failed (key %d, attempt %d): %s",
                            key_idx + 1, attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)

    return None


# ── Multi-original prompt & dispatcher ────────────────────────────────────────

def build_multi_original_prompt(study_r:     str,
                                  abstract_r:  str,
                                  candidates:  list[dict],
                                  sections:    dict,
                                  pdf_url:     str = "",
                                  html_text:   str = "",
                                  force_multi: bool = False) -> str:
    """
    Build the LLM prompt for identifying ALL original studies in a multi-target
    replication paper.
    """
    if candidates:
        def _authors_str_m(c: dict) -> str:
            authors = c.get("all_authors") or ([c["first_author"]] if c.get("first_author") else [])
            return ", ".join(authors) if authors else "unknown"

        cand_lines = [
            f"{i}. \"{c['title']}\" ({c['year']}, authors: {_authors_str_m(c)})\n"
            f"   DOI: {c['doi'] or 'unknown'}  |  OpenAlex: {c['openalex_id']}"
            for i, c in enumerate(candidates, 1)
        ]
        cand_text = "\n".join(cand_lines)
    else:
        cand_text = "(No candidates pre-identified — use reference list and full text below.)"

    ref_lines = []
    for ref in sections.get("references", [])[:100]:
        authors = "; ".join(ref["authors"][:3])
        if len(ref["authors"]) > 3:
            authors += " et al."
        ref_lines.append(f"- {authors} ({ref['year'] or '?'}). {ref['title']}")
    ref_text = "\n".join(ref_lines) if ref_lines else "(no references extracted)"

    abstract_snip = (abstract_r[:2000] + "…") if len(abstract_r) > 2000 else abstract_r
    intro_snip    = (sections.get("intro",   "") or "")[:1200]
    methods_snip  = (sections.get("methods", "") or "")[:800]
    html_snip     = ""
    if html_text and not intro_snip:
        html_snip = (html_text[:2000] + "…") if len(html_text) > 2000 else html_text

    pdf_url_block = ""
    if pdf_url:
        pdf_url_block = (
            f"\n    ## Paper URL\n"
            f"    Full text may be available at: {pdf_url}\n"
            f"    Use it if you can access it to identify all replicated originals.\n"
        )

    force_multi_directive = ""
    if force_multi:
        force_multi_directive = textwrap.dedent("""
    ⚠ CONFIRMED MULTI-TARGET: Automated rules have definitively identified this paper
    as a large-scale multi-target replication (e.g., Many Labs, Registered Replication
    Report). You MUST set is_false_positive to false. Every study listed in the reference
    list that the paper explicitly replicates is an original — list ALL of them. If the
    abstract says "replications of N studies", aim to find N originals.
    """).strip()

    prompt = textwrap.dedent(f"""
    You are an expert in research methodology identifying ALL original studies
    that are replicated or reproduced in a scientific paper.

    This paper has been classified as potentially targeting MULTIPLE original studies.
    Your task: determine if this classification is correct (true multi-target) or a
    false positive (only 1 original), and list ALL originals found.
    {force_multi_directive}

    ## Replication paper
    **Title:** {study_r}

    **Abstract:**
    {abstract_snip or "(not available)"}

    ---

    ## Pre-identified candidate original studies (from OpenAlex)
    {cand_text}

    ---

    ## Full-text excerpts

    **Abstract (from PDF):**
    {(sections.get("abstract","") or "")[:700] or "(not available)"}

    **Introduction:**
    {intro_snip or html_snip or "(not available)"}

    **Methods:**
    {methods_snip or "(not available)"}

    **Reference list (up to 100 entries):**
    {ref_text}
    {pdf_url_block}
    ---

    ## Task

    Identify ALL distinct original studies that this paper directly replicates or reproduces,
    and for each one determine the replication outcome.

    Rules:
    - A study is being replicated if the paper explicitly runs the same procedure again
    - Do NOT include studies that are merely cited for context or background
    - If you find only 1 original, set is_false_positive to true
    - For each candidate number used, reference it in candidate_number (or null if not in list)
    - For outcome: look for the result for THAT SPECIFIC study (e.g. in a results table or
      per-study section), NOT the overall aggregate across all studies
    - outcome values: success (effect confirmed), failure (effect not found), mixed
      (partial), uninformative (cannot determine from available text)

    Respond with **only** this JSON (no prose outside the braces):
    {{
      "is_false_positive": <true if only 1 original found>,
      "reasoning": "<brief explanation of why this is/is not multi-target>",
      "originals": [
        {{
          "rank": 1,
          "candidate_number": <integer from candidate list or null>,
          "title": "<full title of the original study>",
          "doi": "<DOI if identifiable, else empty>",
          "first_author_surname": "<surname of first author>",
          "year": <4-digit year or null>,
          "evidence": "<1-2 sentence quote from the paper showing this study is replicated>",
          "confidence": "<high|medium|low>",
          "outcome": "<success|failure|mixed|uninformative>",
          "outcome_evidence": "<1-2 sentence quote showing the outcome for THIS specific study, or empty if not found>"
        }}
      ]
    }}
    """).strip()

    return prompt


def identify_all_originals_with_llm(doi_r:        str,
                                      study_r:      str,
                                      abstract_r:   str,
                                      candidates:   list[dict],
                                      sections:     dict,
                                      pdf_url:      str = "",
                                      html_text:    str = "",
                                      force_multi:  bool = False) -> dict:
    """
    Identify ALL original studies in a multi-target replication paper.

    Returns:
        {
          "resolved": bool,
          "is_false_positive": bool,
          "n_originals": int,
          "originals": [{"rank", "title", "doi", "first_author", "year",
                          "evidence", "confidence", "candidate_number"}],
          "llm_source": str,
          "llm_reasoning": str,
        }
    """
    cache_file = LLM_CACHE_DIR / f"multi_{cache_key(doi_r)}.json"
    if cache_file.exists() and not force_multi:
        # When force_multi=True we skip the cache to ensure the stronger prompt runs.
        with cache_file.open(encoding="utf-8") as fh:
            cached = json.load(fh)
        cached.setdefault("llm_source", "cache")
        return cached

    _empty = {
        "resolved"         : False,
        "is_false_positive": False,
        "n_originals"      : 0,
        "originals"        : [],
        "llm_source"       : "none",
        "llm_reasoning"    : "",
    }

    prompt = build_multi_original_prompt(study_r, abstract_r, candidates,
                                          sections, pdf_url=pdf_url,
                                          html_text=html_text,
                                          force_multi=force_multi)
    result     = None
    llm_source = "none"
    llm_model  = ""

    # Primary: Gemini
    result, _ = call_gemini(prompt, model=GEMINI_HEAVY_MODEL)
    if result:
        llm_source = "gemini"
        llm_model  = GEMINI_HEAVY_MODEL
        time.sleep(LLM_RATE_SEC)

    # Fallback 1: OpenAI
    if not result:
        result, _ = call_openai(prompt)
        if result:
            llm_source = "openai"
            llm_model  = OPENAI_MODEL
            time.sleep(LLM_RATE_SEC)

    # Fallback 2: OpenRouter (Qwen)
    if not result and OPENROUTER_API_KEY:
        result, _ = call_openrouter(prompt)
        if result:
            llm_source = "openrouter"
            llm_model  = OPENROUTER_HEAVY_MODEL
            time.sleep(LLM_RATE_SEC)

    if not result:
        return _empty

    raw_originals = result.get("originals", [])
    originals = []
    for o in raw_originals:
        if not isinstance(o, dict):
            continue
        # If candidate_number given, fill missing fields from candidate list
        cand_num = o.get("candidate_number")
        if cand_num is not None:
            try:
                idx = int(cand_num) - 1
                if 0 <= idx < len(candidates):
                    c = candidates[idx]
                    o.setdefault("doi",   c.get("doi",          ""))
                    o.setdefault("title", c.get("title",        ""))
                    o.setdefault("year",  c.get("year"))
                    o.setdefault("first_author_surname", c.get("first_author", ""))
            except (ValueError, TypeError):
                pass
        raw_outcome = str(o.get("outcome", "uninformative") or "uninformative").lower()
        if raw_outcome not in {"success", "failure", "mixed", "uninformative", "descriptive"}:
            raw_outcome = "uninformative"
        originals.append({
            "rank"             : o.get("rank", len(originals) + 1),
            "title"            : str(o.get("title", "") or ""),
            "doi"              : str(o.get("doi",   "") or ""),
            "first_author"     : str(o.get("first_author_surname", "") or ""),
            "year"             : o.get("year"),
            "evidence"         : str(o.get("evidence",        "") or ""),
            "confidence"       : str(o.get("confidence", "low") or "low"),
            "candidate_number" : cand_num,
            "outcome"          : raw_outcome,
            "outcome_evidence" : str(o.get("outcome_evidence", "") or ""),
        })

    n_originals = len(originals)
    # When force_multi=True the rule already confirmed this is multi-target;
    # never trust is_false_positive from the LLM in that case.
    if force_multi:
        is_false_positive = False
    else:
        is_false_positive = bool(result.get("is_false_positive", n_originals <= 1))

    output = {
        "resolved"         : n_originals > 0,
        "is_false_positive": is_false_positive,
        "n_originals"      : n_originals,
        "originals"        : originals,
        "llm_source"       : llm_source,
        "llm_model"        : llm_model,
        "llm_reasoning"    : str(result.get("reasoning", "") or ""),
    }

    if n_originals > 0:
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)

    return output
