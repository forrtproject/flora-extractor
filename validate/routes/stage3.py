"""routes/stage3.py — the Stage 3 page shell.

Holds no content of its own: every section is fetched from `api_stage3`, plus the
ladder's changelog from `api_docs` and the set-aside counts from `api_flow`, both of
which already read them. See those modules for why.
"""
from flask import Blueprint, render_template

stage3_page_bp = Blueprint("stage3_page", __name__)


@stage3_page_bp.route("/stage3")
def stage3_page():
    return render_template("stage3.html", active_page="stage3")
