"""api_docs.py — the pipeline documented from the code that runs it.

Nothing here is written prose about behaviour. Every rule, prompt, pile and ladder
revision is read from the artifact the pipeline itself loads: `filter/spec/*.json`,
`shared.prompts.PROMPT_NAMES`, `filter/spec/conventions.json`,
`extract/link_original.py`. Edit a prompt and this page changes with it.

That is the whole point. A hand-written page describing a prompt is wrong the moment
someone edits the prompt, and nothing warns the reader — which is exactly the failure
this dashboard exists to make impossible for numbers.
"""
import ast
import inspect
import json
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify

from shared.config import BASE_DIR

docs_bp = Blueprint("docs", __name__)

SPEC_DIR = BASE_DIR / "filter" / "spec"

# The packages that make up the pipeline, in the order a reader should meet them.
_PACKAGES = (
    ("search",   "Stage 1 — discovery", "Scans the OpenAlex snapshot into the survivor pool."),
    ("filter",   "Stage 2 — routing and screening", "Sorts the pool into piles; only an LLM screen may admit."),
    ("extract",  "Stage 3 — linking and coding", "Finds each paper's original and codes the outcome."),
    ("validate", "Stage 4 — monitoring", "This dashboard. Read-only; writes nothing."),
    ("shared",   "Shared services", "Clients, caches, prompts, schema — used by every stage."),
)


def _module_summary(path: Path) -> Optional[str]:
    """A module's docstring first line, read without importing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    doc = ast.get_docstring(tree)
    if not doc:
        return None
    first = doc.strip().split("\n")[0].strip()
    # Most docstrings here open "name.py — what it does"; the filename is already
    # the column beside it, so it is not repeated in the summary.
    if "—" in first:
        first = first.split("—", 1)[1].strip()
    # Some docstrings continue the filename into a lowercase clause ("cli.py — the
    # engine's operations"); once the filename is dropped the clause has to stand as
    # its own sentence beside the ones that already do.
    return first[:1].upper() + first[1:] if first else first


@docs_bp.route("/api/docs/architecture")
def api_architecture():
    """Every pipeline module with its own docstring's first line."""
    packages = []
    for name, title, blurb in _PACKAGES:
        directory = BASE_DIR / name
        modules = []
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "__init__.py":
                continue
            modules.append({
                "path": str(path.relative_to(BASE_DIR)).replace("\\", "/"),
                "name": path.stem,
                "summary": _module_summary(path) or "",
                "lines": sum(1 for _ in path.open("rb")),
            })
        packages.append({"package": name, "title": title, "blurb": blurb,
                         "modules": modules})
    return jsonify({"packages": packages})


@docs_bp.route("/api/docs/rules")
def api_rules():
    """The Stage 2 rule book: every spec the engine loads, with its measured evidence.

    `description` and `measured` are the specs' own fields — the record of what a rule
    was tested against and what was rejected while writing it. That is the material a
    reader needs to change a rule responsibly, so it is served whole rather than
    summarised.
    """
    rules = []
    for path in sorted(SPEC_DIR.glob("*.json")):
        if path.stem in ("aliases", "conventions"):
            continue                                    # data, not rules
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rules.append({
            "id":          spec.get("id", path.stem),
            "file":        f"filter/spec/{path.name}",
            "pile":        spec.get("pile"),
            "precedence":  spec.get("precedence"),
            "shadow":      bool(spec.get("shadow")),
            "vocabulary":  spec.get("vocabulary"),
            "domain":      spec.get("domain"),
            "description": spec.get("description", ""),
            "measured":    spec.get("measured"),
            "match":       spec.get("match"),
        })
    rules.sort(key=lambda r: (r["precedence"] if r["precedence"] is not None else 999,
                              r["id"]))

    conventions = {}
    conv = SPEC_DIR / "conventions.json"
    if conv.exists():
        try:
            conventions = json.loads(conv.read_text(encoding="utf-8"))
        except ValueError:
            conventions = {}
    return jsonify({"rules": rules, "conventions": conventions})


@docs_bp.route("/api/docs/prompts")
def api_prompts():
    """Every prompt the pipeline can send, with the version hash that keys its cache.

    A `build_*` prompt is assembled at call time from spliced fragments, so its source
    is served rather than a rendered string: the source IS the prompt, and it is what
    `prompt_version` hashes. A `*_PROMPT` constant is served as its text.
    """
    from shared import prompts as P

    out = []
    for name in P.PROMPT_NAMES:
        obj = getattr(P, name)
        fragments: list[dict] = []
        if isinstance(obj, str):
            body, kind = obj, "constant"
        else:
            try:
                body, kind = inspect.getsource(obj), "builder"
            except OSError:
                body, kind = "", "builder"
            # The builder's own source is a few lines of assembly; the PROMPT is the
            # text it splices. `_collect` is what `prompt_version` hashes, so reading
            # the fragments from it means the page and the cache key can never
            # disagree about what a prompt is.
            parts: dict = {}
            try:
                P._collect(obj, parts)
            except (OSError, SyntaxError):
                parts = {}
            for part_name, rendered in sorted(parts.items()):
                if part_name == name:
                    continue
                try:
                    value = ast.literal_eval(rendered)
                except (ValueError, SyntaxError):
                    value = rendered
                text = value if isinstance(value, str) else repr(value)
                if isinstance(value, str) and len(text.strip()) < 40:
                    continue                 # a cap or a label, not prompt text
                fragments.append({"name": part_name, "text": text,
                                  "lines": text.count("\n") + 1})
        try:
            version = P.prompt_version(name)
        except KeyError:
            version = ""
        total = body.count("\n") + 1 + sum(f["lines"] for f in fragments)
        out.append({"name": name, "kind": kind, "version": version,
                    "lines": total, "body": body, "fragments": fragments})
    out.sort(key=lambda p: (p["kind"] != "constant", p["name"]))
    return jsonify({"prompts": out, "count": len(out)})


@docs_bp.route("/api/docs/ladder")
def api_ladder():
    """Stage 3's resolution ladder: its version, its revisions, and what counts resolved.

    The numbered revisions are the comment block `EXTRACT_LADDER_VERSION` sits under —
    the running record of why each step behaves the way it does.
    """
    from extract.link_original import EXTRACT_LADDER_VERSION
    from shared.schema import RESOLVED_LINK_METHODS

    source = (BASE_DIR / "extract" / "link_original.py").read_text(encoding="utf-8")
    revisions = []
    for line in source.split("EXTRACT_LADDER_VERSION")[0].split("\n"):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        text = stripped.lstrip("#").rstrip()
        head = text.strip().split(" ", 1)
        if head and head[0].isdigit():
            revisions.append({"n": int(head[0]),
                              "text": head[1].strip() if len(head) > 1 else ""})
        elif revisions and text.strip():
            revisions[-1]["text"] += " " + text.strip()
    revisions.sort(key=lambda r: -r["n"])

    return jsonify({
        "version": EXTRACT_LADDER_VERSION,
        "revisions": revisions,
        "resolved_link_methods": sorted(RESOLVED_LINK_METHODS),
    })
