from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import helpers
import auth

bp = Blueprint("health", __name__, url_prefix="/health")

# Roles allowed to record health/treatment data day to day
HEALTH_STAFF = ("admin", "manager", "worker", "vet")


@bp.route("")
@auth.login_required
def index():
    tab = request.args.get("tab", "observations")
    observations = db.query(
        "SELECT h.*, a.tag_id, a.record_no FROM health_observations h JOIN animals a ON a.id=h.animal_id "
        "ORDER BY h.observation_date DESC LIMIT 200")
    treatments = db.query(
        "SELECT t.*, a.tag_id, a.record_no, m.name AS medicine_catalog_name FROM treatments t "
        "JOIN animals a ON a.id=t.animal_id LEFT JOIN medicines m ON m.id=t.medicine_id ORDER BY t.start_date DESC LIMIT 200")
    withdrawal = db.query(
        "SELECT t.*, a.tag_id, a.record_no FROM treatments t JOIN animals a ON a.id=t.animal_id "
        "WHERE (date(t.meat_withdrawal_end) >= date('now') OR date(t.milk_withdrawal_end) >= date('now')) "
        "ORDER BY t.meat_withdrawal_end")
    medicines = db.query("SELECT * FROM medicines ORDER BY type, name")
    animals = db.query("SELECT a.*, sp.name AS species_name FROM animals a JOIN species sp ON sp.id=a.species_id "
                       "WHERE a.status='active' ORDER BY sp.name, a.tag_id")
    species_counts = {}
    for a in animals:
        species_counts[a["species_name"]] = species_counts.get(a["species_name"], 0) + 1
    stock_names = [r["name"] for r in db.query("SELECT name FROM stock_items WHERE active=1 ORDER BY name")]
    return render_template("health_index.html", tab=tab, observations=observations, treatments=treatments,
                           withdrawal=withdrawal, medicines=medicines, animals=animals,
                           species_counts=species_counts, groups=helpers.group_options(), stock_names=stock_names,
                           can_edit=g.user["role"] in HEALTH_STAFF, today=db.today_str())


@bp.route("/observation/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*HEALTH_STAFF)
def add_observation():
    animal_id = request.form.get("animal_id", type=int)
    if not animal_id:
        flash("Select an animal.", "error")
        return redirect(url_for("health.index"))
    new_id = db.execute(
        "INSERT INTO health_observations (animal_id, observation_date, symptoms, provisional_diagnosis, "
        "confirmed_diagnosis, notes, created_by) VALUES (?,?,?,?,?,?,?)",
        (animal_id, request.form.get("observation_date") or db.today_str(), request.form.get("symptoms"),
         request.form.get("provisional_diagnosis"), request.form.get("confirmed_diagnosis"),
         request.form.get("notes"), g.user["id"]))
    a = db.query("SELECT record_no FROM animals WHERE id=?", (animal_id,), one=True)
    db.audit(g.user, "create", "health_observation", new_id, f"Observation recorded for {a['record_no']}")
    flash("Observation saved. This is a record-keeping entry only -- it is not a diagnosis.", "success")
    return redirect(url_for("health.index", tab="observations"))


@bp.route("/treatment/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*HEALTH_STAFF)
def add_treatment():
    target = request.form.get("target", "one")
    if target == "one":
        ids = [request.form.get("animal_id", type=int)] if request.form.get("animal_id") else []
        label = None
    elif target in ("all_goats", "all_sheep", "all"):
        where = {"all_goats": "AND sp.name='Goat'", "all_sheep": "AND sp.name='Sheep'", "all": "AND sp.name IN ('Goat','Sheep')"}[target]
        ids = [r["id"] for r in db.query(f"SELECT a.id FROM animals a JOIN species sp ON sp.id=a.species_id WHERE a.status='active' {where}")]
        label = {"all_goats": "all goats", "all_sheep": "all sheep", "all": "all goats and sheep"}[target]
    elif target == "group":
        gid = request.form.get("group_id", type=int)
        ids = [r["id"] for r in db.query("SELECT id FROM animals WHERE status='active' AND current_group_id=?", (gid,))] if gid else []
        grp = db.query("SELECT name FROM groups_ WHERE id=?", (gid,), one=True) if gid else None
        label = f"group {grp['name']}" if grp else None
    else:  # pick several
        ids = [int(x) for x in request.form.getlist("animal_ids") if x.isdigit()]
        label = None
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        flash("Choose who gets the treatment: an animal, a whole species, a group, or tick some animals.", "error")
        return redirect(url_for("health.index", tab="treatments"))

    medicine_id = request.form.get("medicine_id", type=int) or None
    start_date = request.form.get("start_date") or db.today_str()
    meat_wd = request.form.get("meat_withdrawal_end") or None
    milk_wd = request.form.get("milk_withdrawal_end") or None

    if medicine_id and not (meat_wd or milk_wd):
        med = db.query("SELECT * FROM medicines WHERE id=?", (medicine_id,), one=True)
        if med:
            start = db.parse_date(start_date)
            if start:
                from datetime import timedelta
                if med["default_meat_withdrawal_days"]:
                    meat_wd = (start + timedelta(days=med["default_meat_withdrawal_days"])).isoformat()
                if med["default_milk_withdrawal_days"]:
                    milk_wd = (start + timedelta(days=med["default_milk_withdrawal_days"])).isoformat()

    from blueprints.stock import deduct_for_treatment
    med_name = request.form.get("medicine_name")
    if medicine_id and not med_name:
        m = db.query("SELECT name FROM medicines WHERE id=?", (medicine_id,), one=True)
        med_name = m["name"] if m else None
    stock_msgs = set()
    last = None
    for animal_id in ids:
        a = db.query("SELECT id, record_no, tag_id FROM animals WHERE id=?", (animal_id,), one=True)
        if not a:
            continue
        new_id = db.execute(
            "INSERT INTO treatments (animal_id, health_observation_id, treatment_type, medicine_id, medicine_name, dose, "
            "dose_unit, route, start_date, end_date, administered_by, veterinarian, meat_withdrawal_end, "
            "milk_withdrawal_end, follow_up_date, result, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (animal_id, request.form.get("health_observation_id", type=int) or None, request.form.get("treatment_type"),
             medicine_id, request.form.get("medicine_name") or None, request.form.get("dose"), request.form.get("dose_unit"),
             request.form.get("route"), start_date, request.form.get("end_date") or None,
             request.form.get("administered_by") or g.user["full_name"], request.form.get("veterinarian") or None,
             meat_wd, milk_wd, request.form.get("follow_up_date") or None, request.form.get("result") or None,
             request.form.get("notes"), g.user["id"]))
        db.audit(g.user, "create", "treatment", new_id, f"Treatment recorded for {a['record_no']}"
                 + (f" ({label})" if label else ""))
        msg = deduct_for_treatment(new_id, med_name, request.form.get("dose"), request.form.get("dose_unit"), start_date, g.user)
        if msg:
            stock_msgs.add(msg)
        last = a

    follow_up = request.form.get("follow_up_date")
    if follow_up and last:
        who = last["record_no"] if len(ids) == 1 else (label or f"{len(ids)} animals")
        db.execute("INSERT INTO tasks (task_type, title, description, related_entity_type, related_entity_id, due_date, created_by) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("follow_up", f"Follow-up treatment check: {who} ({start_date})", "Follow-up examination after treatment.",
                    "animal" if len(ids) == 1 else None, last["id"] if len(ids) == 1 else None, follow_up, g.user["id"]))
    for msg in sorted(stock_msgs):
        flash(msg, "error")
    flash("Treatment saved." if len(ids) == 1 else f"Treatment saved for {len(ids)} animals" + (f" ({label})." if label else "."), "success")
    return redirect(url_for("health.index", tab="treatments"))


@bp.route("/medicines/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def add_medicine():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Medicine/vaccine name is required.", "error")
        return redirect(url_for("health.index", tab="medicines"))
    new_id = db.execute(
        "INSERT INTO medicines (name, type, default_meat_withdrawal_days, default_milk_withdrawal_days, notes) VALUES (?,?,?,?,?)",
        (name, request.form.get("type", "medicine"), request.form.get("default_meat_withdrawal_days", type=int) or 0,
         request.form.get("default_milk_withdrawal_days", type=int) or 0, request.form.get("notes")))
    db.audit(g.user, "create", "medicine", new_id, f"Added medicine/vaccine catalog entry: {name}")
    flash("Medicine/vaccine added to catalog.", "success")
    return redirect(url_for("health.index", tab="medicines"))
