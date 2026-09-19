from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from datetime import datetime
import auth
import db

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard.index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.authenticate(username, password)
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            db.execute("UPDATE users SET last_login=? WHERE id=?", (datetime.utcnow().isoformat(), user["id"]))
            db.audit(user, "login", "user", user["id"], f"{user['username']} logged in")
            nxt = request.args.get("next") or url_for("dashboard.index")
            return redirect(nxt)
        error = "Incorrect username or password."
        db.audit(None, "login_failed", "user", summary=f"failed login for '{username}'")
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    if g.user:
        db.audit(g.user, "logout", "user", g.user["id"], f"{g.user['username']} logged out")
    session.clear()
    return redirect(url_for("auth.login"))
