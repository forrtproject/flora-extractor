"""routes/docs.py — the documentation page shell.

The page holds no content of its own: every section is fetched from `api_docs`, which
reads the running code. See that module for why.
"""
from flask import Blueprint, render_template

docs_page_bp = Blueprint("docs_page", __name__)


@docs_page_bp.route("/docs")
def docs_page():
    return render_template("docs.html", active_page="docs")
