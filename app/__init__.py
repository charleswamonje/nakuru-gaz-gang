import os
from datetime import timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask
from flask_compress import Compress
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.pool import NullPool

load_dotenv()

db = SQLAlchemy()
csrf = CSRFProtect()
compress = Compress()
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])


def database_url():
    raw = os.getenv("DATABASE_URL", "").strip()
    # Render/other providers may expose postgres://; SQLAlchemy expects postgresql://.
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw or "sqlite:///nakuru_gaz_gang.db"


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    production = os.getenv("FLASK_ENV", "production").lower() == "production"

    secret = os.getenv("SECRET_KEY")
    if production and not secret:
        raise RuntimeError("SECRET_KEY must be set in production.")
    app.config["SECRET_KEY"] = secret or "dev-only-change-me"
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": int(os.getenv("DB_POOL_SIZE", "3")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "2")),
    }
    # SQLite does not benefit from a large connection pool on the dev machine.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

    app.config.update(
        BUSINESS_START_DATE=os.getenv("BUSINESS_START_DATE", "2026-09-16"),
        EMAIL_PUBLIC_AFTER_DAYS=int(os.getenv("EMAIL_PUBLIC_AFTER_DAYS", "30")),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", ""),
        SMTP_HOST=os.getenv("SMTP_HOST"),
        SMTP_PORT=int(os.getenv("SMTP_PORT", "587")),
        SMTP_USERNAME=os.getenv("SMTP_USERNAME"),
        SMTP_PASSWORD=os.getenv("SMTP_PASSWORD"),
        SMTP_FROM=os.getenv("SMTP_FROM", "redgroup@gmail.com"),
        RESEND_API_KEY=os.getenv("RESEND_API_KEY"),
        RESEND_FROM=os.getenv("RESEND_FROM", ""),
        SHOW_DEV_EMAIL_LINK=os.getenv("SHOW_DEV_EMAIL_LINK", "false").lower() == "true",
        SESSION_COOKIE_SECURE=production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    )

    if production and not app.config["ADMIN_PASSWORD"]:
        app.logger.warning("ADMIN_PASSWORD is not configured; admin login will be unavailable.")

    db.init_app(app)
    csrf.init_app(app)
    compress.init_app(app)
    limiter.init_app(app)

    from .routes import main
    app.register_blueprint(main)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["X-XSS-Protection"] = "0"
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; img-src 'self' data:; frame-ancestors 'self'; "
                "base-uri 'self'; form-action 'self'; object-src 'none'"
            )
        # Static assets are immutable enough for short browser caching; HTML/API stays dynamic.
        if response.content_type and response.content_type.startswith(("text/css", "application/javascript")):
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    with app.app_context():
        from . import models
        db.create_all()
        models.seed_data()

    return app
