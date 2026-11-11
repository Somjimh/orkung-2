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

ROLES = ["admin", "manager", "worker", "viewer", "vet", "storekeeper"]

ROLE_LABELS = {
    "admin": "Administrator",
    "manager": "Manager",
    "worker": "Farm worker",
    "viewer": "Viewer / Auditor",
    "vet": "Veterinary professional",
    "storekeeper": "Storekeeper",
}

# Roles allowed to edit/create general animal & operational records
STAFF = ("admin", "manager", "worker")
# Roles allowed to make management-level decisions (edit core identity, delete, config)
MANAGEMENT = ("admin", "manager")

# Crops, work, payroll, store and assets
CROP_EDIT = ("admin", "manager")                      # blocks, plantings, daily sheet, sales
HARVEST_EDIT = ("admin", "manager", "worker", "storekeeper")
PAY_VIEW = ("admin", "manager", "viewer")
PAY_EDIT = ("admin", "manager")                       # draft pay runs, advances, staff rates
PAY_APPROVE = ("admin",)                              # approve and lock a pay run
STORE_EDIT = ("admin", "manager", "worker", "vet", "storekeeper")
ASSET_EDIT = ("admin", "manager", "storekeeper")      # check out / return / verify / repair
DAILY_LOG = ("admin", "manager", "worker", "storekeeper")   # add to the Today log
DAILY_VIEW = DAILY_LOG + ("viewer",)


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
FARM = {"today", "crops", "work", "store", "assets", "milestones"}
NAV_SECTIONS = {
    "admin":   {"dashboard", "animals", "add_animal", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports", "admin", "payroll"} | FARM,
    "manager": {"dashboard", "animals", "add_animal", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports", "payroll"} | FARM,
    # Workers use the farm app on their phones; besides it they see only the
    # livestock records, never money (pay, costs, prices, sales, store value).
    "worker":  {"animals", "add_animal", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "today"},
    "viewer":  {"dashboard", "animals", "weights", "breeding", "births",
                "health", "groups", "movements", "tasks", "reports", "payroll"} | FARM,
    "vet":     {"dashboard", "animals", "health", "tasks", "reports", "store"},
    "storekeeper": {"dashboard", "today", "crops", "store", "assets", "milestones", "tasks", "reports"},
}


# Parts of the site a worker login may open. Everything else (dashboard, crops,
# costs, store prices, Daily Sheet, payroll, reports, admin) sends them back to
# their own jobs.
WORKER_BLUEPRINTS = {"farmapp", "auth", "today", "animals", "breeding", "groups", "health", "weights", "tasks"}
WORKER_ENDPOINTS = {"static", "manifest", "service_worker", "uploaded_file",
                    "crops.harvests", "crops.delete_harvest"}


def worker_may_open(endpoint, blueprint):
    if endpoint is None:
        return True
    return blueprint in WORKER_BLUEPRINTS or endpoint in WORKER_ENDPOINTS


def visible_sections(role):
    return NAV_SECTIONS.get(role, set())

