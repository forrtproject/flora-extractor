"""
app.py — Flask entry point for the FLoRA monitoring web app.

Read-only monitoring dashboard for the extraction pipeline.
Validation has moved to a separate repo backed by Supabase.

Usage:
    python -m validate.app  →  http://localhost:5001
"""
from flask import Flask, redirect, request, send_from_directory, session, url_for

from shared.config import DATA_DIR, PDF_CACHE_DIR


def create_app(test_config: dict | None = None) -> Flask:
    import os

    app = Flask(__name__, template_folder="templates")
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "flora-extractor-dev")

    if test_config:
        app.config.update(test_config)

    # FLORA_READONLY=1 skips pipeline blueprints that require heavy deps
    # (pdfminer, pymupdf, playwright). Used for read-only hosting deployments.
    readonly = os.getenv("FLORA_READONLY", "").lower() in ("1", "true", "yes")

    from validate.routes.dashboard import dashboard_bp
    from validate.routes.check import check_bp
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(check_bp)

    if not readonly:
        from validate.routes.batch import batch_bp
        from validate.routes.multi_originals import multi_orig_bp
        from validate.routes.disambiguation import disambiguation_bp
        app.register_blueprint(batch_bp)
        app.register_blueprint(multi_orig_bp)
        app.register_blueprint(disambiguation_bp)

    @app.route("/pdf/<path:filename>")
    def serve_pdf(filename: str):
        return send_from_directory(str(PDF_CACHE_DIR), filename)

    @app.route("/set-name", methods=["GET", "POST"])
    def set_name():
        from flask import render_template, request, session
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if name:
                session["reviewer_id"] = name
            next_url = request.args.get("next") or url_for("dashboard.dashboard_page")
            return redirect(next_url)
        return render_template("set_name.html")

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.dashboard_page"))

    @app.route("/pipeline")
    def pipeline_redirect():
        return redirect(url_for("dashboard.dashboard_page"), code=301)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
