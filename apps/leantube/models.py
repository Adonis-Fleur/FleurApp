from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone

db = SQLAlchemy()

class Profile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    ip = db.Column(db.String(64), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    pattern = db.Column(db.String(256), default="Vice_Clip_{n}")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    clips = db.relationship('Clip', backref='profile', lazy='dynamic', cascade='all, delete-orphan')

class Clip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('profile.id'), nullable=False)
    clip_number = db.Column(db.Integer, nullable=False)
    original_name = db.Column(db.String(128), nullable=False)
    custom_name = db.Column(db.String(256))
    thumbnail = db.Column(db.String(256))
    custom_thumbnail = db.Column(db.String(512))
    video_url = db.Column(db.String(512))
    local_path = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Config(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)
