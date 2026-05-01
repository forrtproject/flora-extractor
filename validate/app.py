"""
app.py — Flask entry point for the Stage 4 validation web app.

Usage:
    python validate/app.py    →  http://localhost:5001

Registers blueprints:
  batch_bp          (GET /batch, /api/batch/*)
  multi_orig_bp     (GET /multi-originals, /api/multi-originals/*)
  input_bp          (GET /input, /api/input/*)
  review_bp         (GET /review, POST /vote)
  export_bp         (GET /export)
  dashboard_bp      (GET /dashboard)
"""
from flask import Flask, send_from_directory

from shared.config import DATA_DIR, PDF_CACHE_DIR, log
from validate import state
from validate.routes.batch import batch_bp
from validate.routes.multi_originals import multi_orig_bp
from validate.routes.input import input_bp
from validate.routes.review import review_bp
from validate.routes.export import export_bp
from validate.routes.dashboard import dashboard_bp

app = Flask(__name__, template_folder="templates")
app.secret_key = "flora-extractor-dev"

# Register all blueprints
app.register_blueprint(batch_bp)
app.register_blueprint(multi_orig_bp)
app.register_blueprint(input_bp)
app.register_blueprint(review_bp)
app.register_blueprint(export_bp)
app.register_blueprint(dashboard_bp)


@app.route("/pdf/<path:filename>")
def serve_pdf(filename: str):
    """Serve cached PDF files for the disambiguation UI."""
    return send_from_directory(str(PDF_CACHE_DIR), filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
