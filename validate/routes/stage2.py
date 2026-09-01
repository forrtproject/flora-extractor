"""routes/stage2.py — the Stage 2 page shell.

Holds no content of its own: every section is fetched from `api_stage2` (and the rule
book from `api_docs`, which already reads the specs). See those modules for why.
"""
from flask import Blueprint, render_template

stage2_page_bp = Blueprint("stage2_page", __name__)


@stage2_page_bp.route("/stage2")
def stage2_page():
    return render_template("stage2.html", active_page="stage2")
