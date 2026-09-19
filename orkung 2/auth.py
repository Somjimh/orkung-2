"""Authentication and server-side role-based access control.

Every protected route in every blueprint is wrapped with `login_required`
and, where relevant, `require_roles(...)`. This is enforced in the request
handlers themselves (not just hidden in the UI), so a user who forges a
request cannot bypass permissions.
"""
from functools import wraps
from flask import session, redirect, url_for, request, g, abort, flash
from werkzeug.security import check_password_hash
import db

ROLES = ["admin", "manager", "worker", "viewer", "vet"]

ROLE_LABELS = {
    "admin": "Administrator",
    "manager": "Manager",
    "worker": "Farm worker",
    "viewer": "Viewer / Auditor",
    "vet": "Veterinary professional",
}

# Roles allowed to edit/create general animal & operational records
STAFF = ("admin", "manager", "worker")
# Roles allowed to make management-level decisions (edit core identity, delete, config)
MANAGEMENT = ("admin", "manager")


def load_logged_in_user():
    uid = session.get("user_id")
    if uid is None:
        g.user = None
    else:
        g.user = db.query("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True)
        if g.user is None:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def require_roles(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.path))
            if g.user["role"] not in roles:
                db.audit(g.user, "delete_attempt" if request.method == "DELETE" else "access_denied",
                          "permission", summary=f"{g.user['username']} denied {request.method} {request.path}")
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def authenticate(username, password):
    user = db.query("SELECT * FROM users WHERE username=? AND active=1", (username,), one=True)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def can_edit_animals():
    return g.user and g.user["role"] in STAFF


def can_manage():
    return g.user and g.user["role"] in MANAGEMENT


# Which top navigation sections each role may see. Routes still enforce
# their own permissions server-side -- this only controls what is *offered*
# in the navigation, per role, so people aren't shown links to pages they
# can't use.
NAV_SECTIONS = {
    "admin":   {"dashboard", "animals", "add_animal", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports", "admin"},
    "manager": {"dashboard", "animals", "add_animal", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports"},
    "worker":  {"dashboard", "animals", "add_animal", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports"},
    "viewer":  {"dashboard", "animals", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports"},
    "vet":     {"dashboard", "animals", "health", "tasks", "reports"},
}


def visible_sections(role):
    return NAV_SECTIONS.get(role, set())

