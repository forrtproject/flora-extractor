"""
app.py — Flask entry point for the Stage 4 validation web app.

Usage:
    python -m validate.app  →  http://localhost:5001

Run import first:
    python -m validate.import_csv
"""
from flask import Flask, redirect, request, send_from_directory, session, url_for

from shared.config import DATA_DIR, PDF_CACHE_DIR


def create_app(test_config: dict | None = None) -> Flask:
    from validate.models import db

    app = Flask(__name__, template_folder="templates")
    app.secret_key = "flora-extractor-dev"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATA_DIR / 'flora.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    with app.app_context():
        db.create_all()

    # ── Blueprints ────────────────────────────────────────────────────────────
    from validate.routes.batch import batch_bp
    from validate.routes.multi_originals import multi_orig_bp
    from validate.routes.input import input_bp
    from validate.routes.review import review_bp
    from validate.routes.export import export_bp
    from validate.routes.dashboard import dashboard_bp
    from validate.routes.flora import flora_bp
    from validate.routes.disambiguation import disambiguation_bp
    from validate.routes.pipeline import pipeline_bp

    app.register_blueprint(batch_bp)
    app.register_blueprint(multi_orig_bp)
    app.register_blueprint(input_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(flora_bp)
    app.register_blueprint(disambiguation_bp)
    app.register_blueprint(pipeline_bp)

    # ── Name prompt guard ─────────────────────────────────────────────────────
    @app.before_request
    def require_reviewer_name():
        if (
            request.path.startswith("/static")
            or request.path.startswith("/api/")
            or request.path == "/set-name"
            or request.path.startswith("/pdf")
        ):
            return
        if not session.get("reviewer_id"):
            next_url = request.url
            return redirect(f"/set-name?next={next_url}")

    # ── Static routes ─────────────────────────────────────────────────────────
    @app.route("/pdf/<path:filename>")
    def serve_pdf(filename: str):
        return send_from_directory(str(PDF_CACHE_DIR), filename)

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.dashboard_page"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
