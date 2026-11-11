import os
import csv
import uuid
import io
import shutil
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, current_app, send_file
from werkzeug.security import generate_password_hash
import db
import helpers
import auth

bp = Blueprint("admin", __name__, url_prefix="/admin")

IMPORT_TEMPLATE_HEADER = ["tag_id", "eid", "name", "species", "breed", "sex", "dob", "birth_type",
                          "birth_weight", "color_markings", "group", "site", "source", "notes"]


@bp.route("/users")
@auth.login_required
@auth.require_roles("admin")
def users():
    rows = db.query("SELECT u.*, w.name AS worker_name FROM users u LEFT JOIN workers w ON w.id=u.worker_id "
                    "ORDER BY u.active DESC, u.full_name")
    staff = db.query("SELECT id, name FROM workers WHERE active=1 ORDER BY name")
    return render_template("admin_users.html", users=rows, role_choices=auth.ROLES, staff=staff)


@bp.route("/users/<int:user_id>/staff", methods=["POST"])
@auth.login_required
@auth.require_roles("admin")
def link_staff(user_id):
    """Link a login to a staff member. A farm worker linked this way records only
    their own work on the Today page."""
    u = db.query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    if not u:
        abort(404)
    wid = request.form.get("worker_id", type=int) or None
    if wid and not db.query("SELECT 1 FROM workers WHERE id=?", (wid,), one=True):
        abort(400)
    if wid:
        other = db.query("SELECT username FROM users WHERE worker_id=? AND id!=?", (wid, user_id), one=True)
        if other:
            flash(f"That staff member is already linked to the login '{other['username']}'.", "error")
            return redirect(url_for("admin.users"))
    db.execute("UPDATE users SET worker_id=? WHERE id=?", (wid, user_id))
    w = db.query("SELECT name FROM workers WHERE id=?", (wid,), one=True) if wid else None
    db.audit(g.user, "update", "user", user_id, f"{u['username']} linked to staff member {w['name'] if w else '(none)'}")
    flash(f"{u['username']} is now linked to {w['name']}." if w else f"{u['username']} is no longer linked to a staff member.",
          "success")
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Logins for all staff in one go, with printable cards
# ---------------------------------------------------------------------------

_PW_WORDS = ["shamba", "jipe", "ngombe", "mbuzi", "kondoo", "nyuki", "ndizi", "pilipili", "maji", "jua"]


def _suggest_username(name, taken):
    import re
    base = re.sub(r"[^a-z]", "", (name or "").split()[0].lower()) or "staff"
    u, n = base, 2
    while u in taken:
        u, n = f"{base}{n}", n + 1
    taken.add(u)
    return u


def _new_password():
    import secrets
    return secrets.choice(_PW_WORDS) + str(secrets.randbelow(9000) + 1000)


@bp.route("/users/staff-logins", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles("admin")
def staff_logins():
    """Create a login for every staff member who has none, linked to their staff
    record, and print a card for each with the username and a first password."""
    linked = {r["worker_id"]: r for r in db.query("SELECT * FROM users WHERE worker_id IS NOT NULL")}
    staff = db.query("SELECT * FROM workers WHERE active=1 ORDER BY name")
    taken = {r["username"].lower() for r in db.query("SELECT username FROM users")}
    todo = []
    for w in staff:
        if w["id"] in linked:
            continue
        role = "manager" if "manager" in (w["job_title"] or "").lower() else "worker"
        todo.append(dict(w=w, username=_suggest_username(w["name"], taken), role=role))
    farmphone_exists = "farmphone" in taken

    if request.method == "GET":
        return render_template("admin_staff_logins.html", todo=todo, done=[(w, linked[w["id"]]) for w in staff if w["id"] in linked],
                               farmphone_exists=farmphone_exists)

    cards = []
    for t in todo:
        wid = t["w"]["id"]
        if not request.form.get(f"make_{wid}"):
            continue
        username = (request.form.get(f"username_{wid}") or t["username"]).strip().lower()
        role = request.form.get(f"role_{wid}") if request.form.get(f"role_{wid}") in ("worker", "manager", "storekeeper") else "worker"
        if db.query("SELECT 1 FROM users WHERE lower(username)=?", (username,), one=True):
            flash(f"Username '{username}' is already taken; {t['w']['name']} was skipped.", "error")
            continue
        pw = _new_password()
        uid = db.execute("INSERT INTO users (username, password_hash, full_name, role, worker_id) VALUES (?,?,?,?,?)",
                         (username, generate_password_hash(pw), t["w"]["name"], role, wid))
        db.audit(g.user, "create", "user", uid, f"Created staff login {username} ({role}) linked to {t['w']['name']}")
        cards.append(dict(name=t["w"]["name"], title=t["w"]["job_title"], username=username, password=pw, role=role))
    if request.form.get("make_farmphone") and not farmphone_exists:
        pw = _new_password()
        uid = db.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
                         ("farmphone", generate_password_hash(pw), "Shared farm phone", "worker"))
        db.audit(g.user, "create", "user", uid, "Created shared farm phone login")
        cards.append(dict(name="Shared farm phone", title="For anyone without a smartphone", username="farmphone",
                          password=pw, role="worker"))
    if not cards:
        flash("No logins were created.", "error")
        return redirect(url_for("admin.staff_logins"))
    return render_template("staff_login_cards.html", cards=cards, site=request.host_url.rstrip("/"))


@bp.route("/users/add", methods=["POST"])
@auth.login_required
@auth.require_roles("admin")
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    full_name = request.form.get("full_name", "").strip()
    role = request.form.get("role")
    if not username or not password or not full_name or role not in auth.ROLES:
        flash("All fields are required and role must be valid.", "error")
        return redirect(url_for("admin.users"))
    exists = db.query("SELECT 1 FROM users WHERE username=?", (username,), one=True)
    if exists:
        flash("That username already exists.", "error")
        return redirect(url_for("admin.users"))
    wid = request.form.get("worker_id", type=int) or None
    if wid and db.query("SELECT 1 FROM users WHERE worker_id=?", (wid,), one=True):
        flash("That staff member already has a login linked to them.", "error")
        return redirect(url_for("admin.users"))
    new_id = db.execute("INSERT INTO users (username, password_hash, full_name, role, email, worker_id) VALUES (?,?,?,?,?,?)",
                        (username, generate_password_hash(password), full_name, role, request.form.get("email"), wid))
    db.audit(g.user, "create", "user", new_id, f"Created user {username} ({role})")
    flash("User created.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@auth.login_required
@auth.require_roles("admin")
def toggle_user(user_id):
    u = db.query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    if not u:
        abort(404)
    if u["id"] == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin.users"))
    new_active = 0 if u["active"] else 1
    db.execute("UPDATE users SET active=? WHERE id=?", (new_active, user_id))
    db.audit(g.user, "status_change", "user", user_id, f"User {u['username']} {'reactivated' if new_active else 'deactivated'}")
    flash("User updated.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@auth.login_required
@auth.require_roles("admin")
def reset_password(user_id):
    u = db.query("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    if not u:
        abort(404)
    new_pw = request.form.get("new_password", "").strip()
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin.users"))
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pw), user_id))
    db.audit(g.user, "update", "user", user_id, f"Password reset for {u['username']}")
    flash("Password reset.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/backup")
@auth.login_required
@auth.require_roles("admin")
def backup():
    src = current_app.config["DATABASE"]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    tmp_path = os.path.join(current_app.config["UPLOAD_DIR"], "..", f"orkung_backup_{ts}.db")
    tmp_path = os.path.abspath(tmp_path)
    shutil.copyfile(src, tmp_path)
    db.audit(g.user, "export", "backup", summary="Downloaded full database backup")
    return send_file(tmp_path, as_attachment=True, download_name=f"orkung_backup_{ts}.db")


@bp.route("/settings")
@auth.login_required
@auth.require_roles("admin")
def settings():
    species = db.query("SELECT * FROM species ORDER BY name")
    breeds = db.query("SELECT b.*, s.name AS species_name FROM breeds b JOIN species s ON s.id=b.species_id ORDER BY s.name, b.name")
    return render_template("admin_settings.html", species=species, breeds=breeds)


@bp.route("/settings/species/<int:species_id>/toggle", methods=["POST"])
@auth.login_required
@auth.require_roles("admin")
def toggle_species(species_id):
    s = db.query("SELECT * FROM species WHERE id=?", (species_id,), one=True)
    if not s:
        abort(404)
    new_val = 0 if s["enabled"] else 1
    db.execute("UPDATE species SET enabled=? WHERE id=?", (new_val, species_id))
    db.audit(g.user, "update", "species", species_id, f"{s['name']} {'enabled' if new_val else 'disabled'}")
    flash(f"{s['name']} {'enabled' if new_val else 'disabled'}.", "success")
    return redirect(url_for("admin.settings"))


@bp.route("/settings/breeds/add", methods=["POST"])
@auth.login_required
@auth.require_roles("admin")
def add_breed():
    species_id = request.form.get("species_id", type=int)
    name = request.form.get("name", "").strip()
    if not species_id or not name:
        flash("Species and breed name are required.", "error")
        return redirect(url_for("admin.settings"))
    db.execute("INSERT OR IGNORE INTO breeds (species_id, name) VALUES (?,?)", (species_id, name))
    db.audit(g.user, "create", "breed", summary=f"Added breed {name}")
    flash("Breed added.", "success")
    return redirect(url_for("admin.settings"))


# ---------------------------------------------------------------------------
# CSV import (preview -> validate -> commit)
# ---------------------------------------------------------------------------

def _import_dir():
    d = os.path.join(current_app.config["UPLOAD_DIR"], "..", "imports")
    os.makedirs(d, exist_ok=True)
    return os.path.abspath(d)


@bp.route("/import", methods=["GET"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def import_form():
    return render_template("admin_import.html", header=IMPORT_TEMPLATE_HEADER)


@bp.route("/import/template.csv")
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def import_template():
    example = [("E101", "", "", "Goat", "Boer", "female", "2024-02-10", "single", "3.1", "white/brown", "Breeding Does/Ewes", "Home Farm", "born_on_farm", "")]
    return helpers.csv_response("animal_import_template.csv", IMPORT_TEMPLATE_HEADER, example)


@bp.route("/import/preview", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def import_preview():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("admin.import_form"))

    token = uuid.uuid4().hex
    path = os.path.join(_import_dir(), f"{token}.csv")
    file.save(path)

    species_by_name = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM species")}
    breeds_by_key = {(r["species_id"], r["name"].lower()): r["id"] for r in db.query("SELECT * FROM breeds")}
    groups_by_name = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM groups_")}
    sites_by_name = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM sites")}
    existing_tags = {r["tag_id"] for r in db.query("SELECT tag_id FROM animals WHERE status='active'")}

    rows_out = []
    seen_in_file = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, raw in enumerate(reader, start=2):
            errs = []
            tag = (raw.get("tag_id") or "").strip()
            species = (raw.get("species") or "").strip()
            sex = (raw.get("sex") or "").strip().lower()
            if not tag:
                errs.append("missing tag_id")
            elif tag in existing_tags:
                errs.append("tag already exists in register")
            elif tag in seen_in_file:
                errs.append("duplicate tag within this file")
            if not species or species.lower() not in species_by_name:
                errs.append("unknown species")
            if sex not in ("male", "female"):
                errs.append("sex must be 'male' or 'female'")
            if tag:
                seen_in_file.add(tag)
            rows_out.append(dict(line=i, data=raw, errors=errs, ok=not errs))

    valid_count = sum(1 for r in rows_out if r["ok"])
    return render_template("admin_import_preview.html", rows=rows_out, token=token,
                           valid_count=valid_count, total=len(rows_out))


@bp.route("/import/commit", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def import_commit():
    token = request.form.get("token", "")
    path = os.path.join(_import_dir(), f"{token}.csv")
    if not os.path.exists(path):
        flash("Import session expired. Please upload the file again.", "error")
        return redirect(url_for("admin.import_form"))

    species_by_name = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM species")}
    breeds_by_key = {(r["species_id"], r["name"].lower()): r["id"] for r in db.query("SELECT * FROM breeds")}
    groups_by_name = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM groups_")}
    sites_by_name = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM sites")}
    existing_tags = {r["tag_id"] for r in db.query("SELECT tag_id FROM animals WHERE status='active'")}

    created = 0
    skipped = 0
    seen = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            tag = (raw.get("tag_id") or "").strip()
            species = (raw.get("species") or "").strip().lower()
            sex = (raw.get("sex") or "").strip().lower()
            if not tag or tag in existing_tags or tag in seen or species not in species_by_name or sex not in ("male", "female"):
                skipped += 1
                continue
            seen.add(tag)
            species_id = species_by_name[species]
            breed_id = breeds_by_key.get((species_id, (raw.get("breed") or "").strip().lower()))
            group_id = groups_by_name.get((raw.get("group") or "").strip().lower())
            site_id = sites_by_name.get((raw.get("site") or "").strip().lower())
            record_no = db.next_record_no()
            new_id = db.execute(
                "INSERT INTO animals (record_no, tag_id, eid, name, species_id, breed_id, sex, dob, birth_type, "
                "birth_weight, color_markings, current_group_id, current_site_id, source, notes, created_by, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_no, tag, raw.get("eid") or None, raw.get("name") or None, species_id, breed_id, sex,
                 raw.get("dob") or None, raw.get("birth_type") or None,
                 float(raw["birth_weight"]) if raw.get("birth_weight") else None,
                 raw.get("color_markings") or None, group_id, site_id, raw.get("source") or "purchased",
                 raw.get("notes") or None, g.user["id"], g.user["id"]))
            db.audit(g.user, "import", "animal", new_id, f"Imported animal {record_no} ({tag}) from CSV")
            created += 1

    os.remove(path)
    db.audit(g.user, "import", "animal", summary=f"CSV import: {created} created, {skipped} skipped")
    flash(f"Import complete: {created} animal(s) created, {skipped} skipped.", "success")
    return redirect(url_for("animals.list_view"))
