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
    # small in-place upgrades for databases created by an earlier version
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_batches)")}
    if cols and "price_per_pack" not in cols:
        conn.execute("ALTER TABLE stock_batches ADD COLUMN price_per_pack REAL")
    _add_columns(conn, "stock_items", [("organic_ok", "INTEGER"), ("phi_days", "INTEGER"),
                                       ("active_ingredient", "TEXT")])
    _add_columns(conn, "stock_movements", [("block_id", "INTEGER REFERENCES crop_blocks(id)"),
                                           ("planting_id", "INTEGER REFERENCES plantings(id)"),
                                           ("worker_id", "INTEGER REFERENCES workers(id)"),
                                           ("rate_note", "TEXT")])
    conn.commit()
    conn.close()
    if not fresh:
        try:
            _upgrade_user_roles(app.config["DATABASE"])
        except Exception as exc:  # never stop the site starting; the old roles keep working
            app.logger.error("Storekeeper role upgrade skipped: %s", exc)
    return fresh


def _add_columns(conn, table, columns):
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not have:
        return
    for name, decl in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _upgrade_user_roles(path):
    """Older databases only allow five roles. Rebuild the users table (same rows,
    same ids) so the storekeeper role can be saved. A copy of the whole database
    file is kept next to it first, so nothing can be lost."""
    import shutil
    conn = sqlite3.connect(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not sql or "storekeeper" in sql[0]:
        conn.close()
        return
    backup = f"{path}.before-roles-{datetime.now():%Y%m%d-%H%M%S}.bak"
    conn.close()
    shutil.copy2(path, backup)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        baseline = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        conn.execute("BEGIN")
        conn.execute("""CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT,
            role TEXT NOT NULL CHECK(role IN ('admin','manager','worker','viewer','vet','storekeeper')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_login TEXT)""")
        conn.execute("INSERT INTO users_new (id, username, password_hash, full_name, email, role, active, created_at, last_login) "
                     "SELECT id, username, password_hash, full_name, email, role, active, created_at, last_login FROM users")
        before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        after = conn.execute("SELECT COUNT(*) FROM users_new").fetchone()[0]
        if before != after:
            raise RuntimeError("user copy count mismatch")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if len(problems) > baseline:
            raise RuntimeError(f"foreign key problems after users upgrade: {problems[:5]}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


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
