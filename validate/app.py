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
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "flora-extractor-dev"

    if test_config:
        app.config.update(test_config)

    # Blueprints
    from validate.routes.batch import batch_bp
    from validate.routes.multi_originals import multi_orig_bp
    from validate.routes.dashboard import dashboard_bp
    from validate.routes.disambiguation import disambiguation_bp
    from validate.routes.pipeline import pipeline_bp
    from validate.routes.extract_view import make_extract_blueprint, add_shared_routes
    from validate.routes.search_view import search_view_bp
    from validate.routes.filter_view import filter_view_bp
    from validate.routes.target_pending import target_pending_bp
    from validate.routes.input import input_bp

    extract_view_bp = make_extract_blueprint(
        "extract_view", DATA_DIR / "extracted.csv", "/extract", "extract"
    )
    add_shared_routes(extract_view_bp)

    extract_test_view_bp = make_extract_blueprint(
        "extract_test_view", DATA_DIR / "extracted-test.csv",
        "/extract-test", "extract_test", test_mode=True,
    )

    app.register_blueprint(batch_bp)
    app.register_blueprint(multi_orig_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(disambiguation_bp)
    app.register_blueprint(pipeline_bp)
    app.register_blueprint(extract_view_bp)
    app.register_blueprint(extract_test_view_bp)
    app.register_blueprint(search_view_bp)
    app.register_blueprint(filter_view_bp)
    app.register_blueprint(target_pending_bp)
    app.register_blueprint(input_bp)

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
        return redirect(url_for("extract_view.extract_page"), code=301)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
