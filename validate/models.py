"""
models.py — SQLAlchemy models for the Stage 4 validation database.

Tables:
  Replication   — one row per (doi_r, original_rank) extracted record
  Vote          — one vote per reviewer per replication record

Usage:
    from validate.models import db, Replication, Vote
    db.init_app(app)
    with app.app_context():
        db.create_all()
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Replication(db.Model):
    """One row per extracted (doi_r, original_rank) record."""
    __tablename__ = "replications"

    id              = db.Column(db.Integer,  primary_key=True)
    doi_r           = db.Column(db.String,   nullable=False, index=True)
    original_rank   = db.Column(db.Integer,  nullable=False, default=1)
    n_originals     = db.Column(db.Integer,  nullable=False, default=1)

    # Original study fields
    doi_o           = db.Column(db.String,   default="")
    title_o         = db.Column(db.String,   default="")
    year_o          = db.Column(db.Integer,  nullable=True)
    authors_o       = db.Column(db.String,   default="")

    # Replication paper fields (pass-through from filtered.csv)
    title_r         = db.Column(db.String,   default="")
    year_r          = db.Column(db.Integer,  nullable=True)
    abstract_r      = db.Column(db.Text,     default="")

    # Linking
    link_method     = db.Column(db.String,   default="target_pending")
    link_evidence   = db.Column(db.Text,     default="")
    link_confidence = db.Column(db.Float,    default=0.0)

    # Outcome
    outcome             = db.Column(db.String, default="pending")
    outcome_phrase      = db.Column(db.Text,   default="")
    outcome_confidence  = db.Column(db.Float,  default=0.0)
    out_quote_source    = db.Column(db.String, default="")
    type                = db.Column(db.String, default="replication")

    # Stage 2 classification
    original_match_type = db.Column(db.String, default="single_original")

    # FLoRA database status (from source CSV)
    flora_status        = db.Column(db.String, default="")

    # Validation state (aggregated from votes)
    validation_status   = db.Column(db.String, default="pending")
    vote_count          = db.Column(db.Integer, default=0)
    confirm_votes       = db.Column(db.Integer, default=0)
    reject_votes        = db.Column(db.Integer, default=0)
    validator_notes     = db.Column(db.Text,    default="")

    votes = db.relationship("Vote", backref="replication", lazy=True,
                            cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("doi_r", "original_rank", name="uq_doi_rank"),
    )


class Vote(db.Model):
    """One vote per reviewer per replication record."""
    __tablename__ = "votes"

    id              = db.Column(db.Integer, primary_key=True)
    replication_id  = db.Column(db.Integer, db.ForeignKey("replications.id"),
                                nullable=False)
    reviewer_id     = db.Column(db.String,  nullable=False)
    vote            = db.Column(db.String,  nullable=False)  # confirm | reject | needs_review
    comment         = db.Column(db.Text,    default="")
    created_at      = db.Column(db.String,  default="")

    __table_args__ = (
        db.UniqueConstraint("replication_id", "reviewer_id", name="uq_vote_reviewer"),
    )
