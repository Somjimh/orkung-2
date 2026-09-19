from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, current_app
import db
import helpers
import auth

bp = Blueprint("weights", __name__, url_prefix="/weights")


def _pct_change(prev, new):
    if not prev:
        return None
    return abs(new - prev) / prev * 100.0


@bp.route("")
@auth.login_required
def index():
    group_id = request.args.get("group_id", type=int)
    species_id = request.args.get("species_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    where = ["1=1"]
    args = []
    if group_id:
        where.append("a.current_group_id=?"); args.append(group_id)
    if species_id:
        where.append("a.species_id=?"); args.append(species_id)
    if date_from:
        where.append("date(w.measured_on) >= date(?)"); args.append(date_from)
    if date_to:
        where.append("date(w.measured_on) <= date(?)"); args.append(date_to)

    rows = db.query(
        f"SELECT w.*, a.tag_id, a.name AS animal_name, a.record_no, sp.name AS species_name, u.full_name AS recorder "
        f"FROM weight_records w JOIN animals a ON a.id=w.animal_id LEFT JOIN species sp ON sp.id=a.species_id "
        f"LEFT JOIN users u ON u.id=w.created_by WHERE {' AND '.join(where)} ORDER BY w.measured_on DESC LIMIT 300",
        tuple(args))

    not_weighed_days = current_app.config["WEIGH_REMINDER_DAYS"]
    stale = db.query(
        "SELECT a.id, a.tag_id, a.name, a.record_no, MAX(w.measured_on) last_weighed "
        "FROM animals a LEFT JOIN weight_records w ON w.animal_id=a.id "
        "WHERE a.status='active' GROUP BY a.id "
        "HAVING last_weighed IS NULL OR julianday('now') - julianday(last_weighed) > ?",
        (not_weighed_days,))

    return render_template("weights_index.html", rows=rows, stale=stale,
                           groups=helpers.group_options(), species_list=helpers.species_options(),
                           f_group_id=group_id, f_species_id=species_id, f_date_from=date_from, f_date_to=date_to)


@bp.route("/export.csv")
@auth.login_required
def export_csv():
    group_id = request.args.get("group_id", type=int)
    where = ["1=1"]; args = []
    if group_id:
        where.append("a.current_group_id=?"); args.append(group_id)
    rows = db.query(
        f"SELECT a.record_no, a.tag_id, w.measured_on, w.weight_kg, w.method, w.body_condition_score, w.notes "
        f"FROM weight_records w JOIN animals a ON a.id=w.animal_id WHERE {' AND '.join(where)} ORDER BY w.measured_on",
        tuple(args))
    header = ["record_no", "tag_id", "measured_on", "weight_kg", "method", "body_condition_score", "notes"]
    data = [(r["record_no"], r["tag_id"], r["measured_on"], r["weight_kg"], r["method"], r["body_condition_score"], r["notes"]) for r in rows]
    db.audit(g.user, "export", "weight_record", summary="Exported weight records CSV")
    return helpers.csv_response("weight_records.csv", header, data)


@bp.route("/add/<int:animal_id>", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def add_individual(animal_id):
    animal = db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)
    if not animal:
        abort(404)
    last = db.query("SELECT * FROM weight_records WHERE animal_id=? ORDER BY measured_on DESC LIMIT 1", (animal_id,), one=True)
    warn = None
    if request.method == "POST":
        weight_kg = request.form.get("weight_kg", type=float)
        measured_on = request.form.get("measured_on") or db.today_str()
        method = request.form.get("method")
        bcs = request.form.get("body_condition_score", type=float)
        notes = request.form.get("notes")
        confirmed = request.form.get("confirm_unusual") == "on"

        if not weight_kg or weight_kg <= 0:
            flash("Enter a valid weight.", "error")
        else:
            pct = _pct_change(last["weight_kg"], weight_kg) if last else None
            threshold = current_app.config["WEIGHT_CHANGE_WARN_PCT"]
            if pct is not None and pct > threshold and not confirmed:
                warn = (f"This is a {pct:.0f}% change from the last recorded weight "
                        f"({last['weight_kg']} kg on {last['measured_on']}). Please double-check the measurement.")
            else:
                new_id = db.execute(
                    "INSERT INTO weight_records (animal_id, weight_kg, measured_on, method, body_condition_score, notes, "
                    "flagged_outlier, confirmed_by, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                    (animal_id, weight_kg, measured_on, method, bcs, notes,
                     1 if (pct is not None and pct > threshold) else 0,
                     g.user["id"] if (pct is not None and pct > threshold) else None, g.user["id"]))
                db.audit(g.user, "create", "weight_record", new_id,
                         f"Weight {weight_kg}kg recorded for {animal['record_no']} on {measured_on}")
                # Verify persistence before confirming success, per spec.
                saved = db.query("SELECT 1 FROM weight_records WHERE id=?", (new_id,), one=True)
                if saved:
                    flash(f"Weight saved: {weight_kg} kg on {measured_on}.", "success")
                else:
                    flash("Something went wrong saving this weight. Please try again.", "error")
                return redirect(url_for("animals.profile", animal_id=animal_id, tab="weights"))
    return render_template("weight_add.html", animal=animal, last=last, warn=warn, today=db.today_str())


@bp.route("/bulk", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def bulk():
    group_id = request.args.get("group_id", type=int) or request.form.get("group_id", type=int)
    animals = []
    if group_id:
        animals = db.query(
            "SELECT a.*, (SELECT weight_kg FROM weight_records w WHERE w.animal_id=a.id ORDER BY measured_on DESC LIMIT 1) AS last_weight, "
            "(SELECT measured_on FROM weight_records w WHERE w.animal_id=a.id ORDER BY measured_on DESC LIMIT 1) AS last_date "
            "FROM animals a WHERE a.current_group_id=? AND a.status='active' ORDER BY a.tag_id", (group_id,))

    if request.method == "POST" and request.form.get("submit_bulk"):
        measured_on = request.form.get("measured_on") or db.today_str()
        saved_count = 0
        skipped = []
        threshold = current_app.config["WEIGHT_CHANGE_WARN_PCT"]
        for key in request.form:
            if key.startswith("weight_"):
                aid = int(key.split("_", 1)[1])
                val = request.form.get(key)
                if not val:
                    continue
                try:
                    weight_kg = float(val)
                except ValueError:
                    continue
                last = db.query("SELECT weight_kg, measured_on FROM weight_records WHERE animal_id=? ORDER BY measured_on DESC LIMIT 1", (aid,), one=True)
                pct = _pct_change(last["weight_kg"], weight_kg) if last else None
                outlier = pct is not None and pct > threshold
                confirmed_all = request.form.get("confirm_all_unusual") == "on"
                if outlier and not confirmed_all:
                    a = db.query("SELECT tag_id FROM animals WHERE id=?", (aid,), one=True)
                    skipped.append(f"{a['tag_id']} ({pct:.0f}% change)")
                    continue
                new_id = db.execute(
                    "INSERT INTO weight_records (animal_id, weight_kg, measured_on, method, notes, flagged_outlier, confirmed_by, created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (aid, weight_kg, measured_on, request.form.get("method"), None,
                     1 if outlier else 0, g.user["id"] if outlier else None, g.user["id"]))
                db.audit(g.user, "create", "weight_record", new_id, f"Bulk weight {weight_kg}kg for animal #{aid} on {measured_on}")
                saved_count += 1
        if skipped:
            flash("Saved " + str(saved_count) + " weight(s). These were unusually different from the last reading and were "
                  "NOT saved -- tick 'confirm unusual changes' and resubmit if they are correct: " + "; ".join(skipped), "error")
        else:
            flash(f"Saved {saved_count} weight record(s) for the group.", "success")
        return redirect(url_for("weights.bulk", group_id=group_id))

    return render_template("weight_bulk.html", animals=animals, groups=helpers.group_options(), group_id=group_id, today=db.today_str())
