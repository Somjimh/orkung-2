import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "orkung-dev-secret-change-me")
    DATABASE = os.environ.get("ORKUNG_DB", os.path.join(BASE_DIR, "instance", "orkung.db"))
    UPLOAD_DIR = os.path.join(BASE_DIR, "instance", "uploads")
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024  # 12MB uploads
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Configurable business rules (also editable by admins at runtime via settings table)
    WEIGH_REMINDER_DAYS = 60          # flag animals not weighed within this many days
    WEIGHT_CHANGE_WARN_PCT = 30       # warn if a new weight differs from the last by more than this %
    UPCOMING_TASK_WINDOW_DAYS = 14
