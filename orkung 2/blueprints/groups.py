from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import helpers
import auth

bp = Blueprint("groups", __name__, url_prefix="/groups")


@bp.route("")
@auth.login_required
def index():
    groups = db.query(
        "SELECT g.*, s.name AS site_name, "
        "(SELECT COUNT(*) FROM animals a WHERE a.current_group_id=g.id AND a.status='active') AS animal_count "
        "FROM groups_ g LEFT JOIN sites s ON s.id=g.site_id ORDER BY g.active DESC, g.name")
    sites = db.query(
        "SELECT s.*, (SELECT COUNT(*) FROM animals a WHERE a.current_site_id=s.id AND a.status='active') AS animal_count "
        "FROM sites s ORDER BY s.active DESC, s.name")
    return render_template("groups_index.html", groups=groups, sites=sites)


@bp.route("/<int:group_id>")
@auth.login_required
def detail(group_id):
    group = db.query("SELECT g.*, s.name AS site_name FROM groups_ g LEFT JOIN sites s ON s.id=g.site_id WHERE g.id=?", (group_id,), one=True)
    if not group:
        abort(404)
    current_members = db.query(
        "SELECT a.* FROM animals a WHERE a.current_group_id=? AND a.status='active' ORDER BY a.tag_id", (group_id,))
    history = db.query(
        "SELECT gm.*, a.tag_id, a.name AS animal_name FROM group_memberships gm JOIN animals a ON a.id=gm.animal_id "
        "WHERE gm.group_id=? ORDER BY gm.start_date DESC LIMIT 200", (group_id,))

    weights = []
    for m in current_members:
        last = db.query("SELECT weight_kg FROM weight_records WHERE animal_id=? ORDER BY measured_on DESC LIMIT 1", (m["id"],), one=True)
        if last:
            weights.append(last["weight_kg"])
    avg_weight = round(sum(weights) / len(weights), 1) if weights else None

    open_treatments = db.query(
        "SELECT COUNT(*) c FROM treatments t JOIN animals a ON a.id=t.animal_id WHERE a.current_group_id=? "
        "AND (date(t.meat_withdrawal_end) >= date('now') OR date(t.milk_withdrawal_end) >= date('now'))", (group_id,), one=True)["c"]
    pregnant = sum(1 for m in current_members if m["reproductive_status"] == "pregnant")

    return render_template("group_detail.html", group=group, current_members=current_members, history=history,
                           avg_weight=avg_weight, open_treatments=open_treatments, pregnant=pregnant)


@bp.route("/add-group", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def add_group():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Group name is required.", "error")
        return redirect(url_for("groups.index"))
    new_id = db.execute("INSERT INTO groups_ (name, site_id, description) VALUES (?,?,?)",
                        (name, request.form.get("site_id", type=int) or None, request.form.get("description")))
    db.audit(g.user, "create", "group", new_id, f"Created group {name}")
    flash("Group created.", "success")
    return redirect(url_for("groups.index"))


@bp.route("/add-site", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def add_site():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Site name is required.", "error")
        return redirect(url_for("groups.index"))
    new_id = db.execute("INSERT INTO sites (name, description) VALUES (?,?)", (name, request.form.get("description")))
    db.audit(g.user, "create", "site", new_id, f"Created site {name}")
    flash("Site created.", "success")
    return redirect(url_for("groups.index"))


@bp.route("/movements")
@auth.login_required
def movements():
    events = db.query(
        "SELECT e.*, a.tag_id, a.record_no, fg.name AS from_group_name, tg.name AS to_group_name, "
        "fs.name AS from_site_name, ts.name AS to_site_name "
        "FROM animal_events e JOIN animals a ON a.id=e.animal_id "
        "LEFT JOIN groups_ fg ON fg.id=e.from_group_id LEFT JOIN groups_ tg ON tg.id=e.to_group_id "
        "LEFT JOIN sites fs ON fs.id=e.from_site_id LEFT JOIN sites ts ON ts.id=e.to_site_id "
        "WHERE e.event_type IN ('group_move','transfer') ORDER BY e.event_date DESC, e.id DESC LIMIT 200")
    animals = db.query(
        "SELECT a.*, gr.name AS group_name FROM animals a LEFT JOIN groups_ gr ON gr.id=a.current_group_id "
        "WHERE a.status='active' ORDER BY a.tag_id")
    return render_template("movements.html", events=events, animals=animals,
                           groups=helpers.group_options(), sites=helpers.site_options())


@bp.route("/move", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def move_animals():
    animal_ids = request.form.getlist("animal_ids", type=int)
    to_group_id = request.form.get("to_group_id", type=int) or None
    to_site_id = request.form.get("to_site_id", type=int) or None
    move_date = request.form.get("move_date") or db.today_str()
    notes = request.form.get("notes")

    if not animal_ids or (not to_group_id and not to_site_id):
        flash("Select at least one animal and a destination group or site.", "error")
        return redirect(url_for("groups.movements"))

    moved = 0
    for aid in animal_ids:
        a = db.query("SELECT * FROM animals WHERE id=?", (aid,), one=True)
        if not a:
            continue
        from_group_id, from_site_id = a["current_group_id"], a["current_site_id"]
        new_group = to_group_id or from_group_id
        new_site = to_site_id or from_site_id
        if from_group_id != new_group:
            db.execute("UPDATE group_memberships SET end_date=? WHERE animal_id=? AND group_id=? AND end_date IS NULL",
                      (move_date, aid, from_group_id))
            db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, created_by) VALUES (?,?,?,?)",
                      (aid, new_group, move_date, g.user["id"]))
        db.execute("UPDATE animals SET current_group_id=?, current_site_id=?, updated_by=?, updated_at=datetime('now') WHERE id=?",
                  (new_group, new_site, g.user["id"], aid))
        db.execute(
            "INSERT INTO animal_events (animal_id, event_type, event_date, from_group_id, to_group_id, from_site_id, "
            "to_site_id, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, "transfer" if (from_site_id != new_site and from_site_id) else "group_move", move_date,
             from_group_id, new_group, from_site_id, new_site, notes, g.user["id"]))
        db.audit(g.user, "update", "animal", aid, f"Moved {a['record_no']} to group/site")
        moved += 1

    flash(f"Moved {moved} animal(s).", "success")
    return redirect(url_for("groups.movements"))
