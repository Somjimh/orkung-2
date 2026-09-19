"""Database access layer for Orkung Livestock Manager.

Uses Python's built-in sqlite3 module only (no external DB driver needed).
Provides a per-request connection (Flask 'g'), row-dict access, and small
helpers used across all blueprints: audit logging, record-number generation,
date math, etc.
"""
import sqlite3
import json
import os
from datetime import datetime, date
from flask import g, current_app


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app):
    app.teardown_appcontext(close_db)


def init_db(app):
    """Create the database file + schema if it does not already exist."""
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    fresh = not os.path.exists(app.config["DATABASE"])
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.execute("PRAGMA foreign_keys = ON")
    with app.open_resource("schema.sql") as f:
        conn.executescript(f.read().decode("utf8"))
    conn.commit()
    conn.close()
    return fresh


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def executemany(sql, seq_of_args):
    db = get_db()
    cur = db.executemany(sql, seq_of_args)
    db.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit(user, action, entity_type, entity_id=None, summary="", details=None):
    """Write an audit-log entry. `user` may be a sqlite3.Row / dict / None."""
    uid = user["id"] if user else None
    uname = user["username"] if user else "system"
    execute(
        "INSERT INTO audit_log (user_id, username, action, entity_type, entity_id, summary, details) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, uname, action, entity_type, entity_id, summary,
         json.dumps(details, default=str) if details is not None else None),
    )


# ---------------------------------------------------------------------------
# ID / record-number helpers
# ---------------------------------------------------------------------------

def next_record_no():
    row = query("SELECT COUNT(*) AS c FROM animals", one=True)
    n = (row["c"] or 0) + 1
    while True:
        candidate = f"ORK-{n:06d}"
        exists = query("SELECT 1 FROM animals WHERE record_no=?", (candidate,), one=True)
        if not exists:
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def today_str():
    return date.today().isoformat()


def days_between(d1, d2):
    a, b = parse_date(d1), parse_date(d2)
    if not a or not b:
        return None
    return (b - a).days


def age_display(dob, estimated=False):
    d = parse_date(dob)
    if not d:
        return "Unknown"
    today = date.today()
    days = (today - d).days
    if days < 0:
        return "Unknown"
    years = days // 365
    months = (days % 365) // 30
    suffix = " (est.)" if estimated else ""
    if years >= 1:
        return f"{years}y {months}m{suffix}"
    if days >= 30:
        return f"{months}m{suffix}"
    return f"{days}d{suffix}"
