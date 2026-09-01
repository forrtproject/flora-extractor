"""routes/stage1.py — the Stage 1 page shell.

Holds no content of its own: every section is fetched from `api_stage1`, which reads
the gate out of the running code and the pool off disk. See that module for why.
"""
from flask import Blueprint, render_template

stage1_page_bp = Blueprint("stage1_page", __name__)


@stage1_page_bp.route("/stage1")
def stage1_page():
    return render_template("stage1.html", active_page="stage1")
