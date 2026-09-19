from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from datetime import date, timedelta
import db
import helpers
import auth

bp = Blueprint("breeding", __name__, url_prefix="/breeding")


@bp.route("")
@auth.login_required
def index():
    status = request.args.get("status", "active")
    where = ["1=1"]; args = []
    if status and status != "all":
        where.append("br.status=?"); args.append(status)
    rows = db.query(
        f"SELECT br.*, d.tag_id AS dam_tag, d.name AS dam_name, s.tag_id AS sire_tag "
        f"FROM breeding_records br JOIN animals d ON d.id=br.dam_id LEFT JOIN animals s ON s.id=br.sire_id "
        f"WHERE {' AND '.join(where)} ORDER BY br.service_date DESC", tuple(args))
    females = db.query("SELECT * FROM animals WHERE sex='female' AND status='active' ORDER BY tag_id")
    males = db.query("SELECT * FROM animals WHERE sex='male' AND status='active' ORDER BY tag_id")
    return render_template("breeding_index.html", rows=rows, females=females, males=males, status=status)


@bp.route("/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def add():
    dam_id = request.form.get("dam_id", type=int)
    if not dam_id:
        flash("Select a dam.", "error")
        return redirect(url_for("breeding.index"))
    sire_id = request.form.get("sire_id", type=int) or None
    sire_freeform = request.form.get("sire_freeform") or None
    method = request.form.get("method", "natural")
    heat_date = request.form.get("heat_observed_date") or None
    service_date = request.form.get("service_date") or db.today_str()

    new_id = db.execute(
        "INSERT INTO breeding_records (dam_id, sire_id, sire_freeform, method, heat_observed_date, service_date, "
        "pregnancy_result, status, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (dam_id, sire_id, sire_freeform, method, heat_date, service_date, "pending", "active",
         request.form.get("notes"), g.user["id"]))
    db.execute("UPDATE animals SET reproductive_status='open', updated_at=datetime('now') WHERE id=? AND reproductive_status NOT IN ('pregnant','lactating')", (dam_id,))
    dam = db.query("SELECT record_no FROM animals WHERE id=?", (dam_id,), one=True)
    db.audit(g.user, "create", "breeding_record", new_id, f"Breeding record for {dam['record_no']} ({method})")

    # Auto-reminder task for a pregnancy check ~35 days after service
    check_due = (db.parse_date(service_date) + timedelta(days=35)).isoformat()
    db.execute("INSERT INTO tasks (task_type, title, description, related_entity_type, related_entity_id, due_date, created_by) "
               "VALUES (?,?,?,?,?,?,?)",
              ("pregnancy_check", f"Pregnancy check due: {dam['record_no']}", "Confirm pregnancy following service.",
               "breeding_record", new_id, check_due, g.user["id"]))
    flash("Breeding record saved.", "success")
    return redirect(url_for("breeding.index"))


@bp.route("/<int:breeding_id>/check", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def pregnancy_check(breeding_id):
    br = db.query("SELECT * FROM breeding_records WHERE id=?", (breeding_id,), one=True)
    if not br:
        abort(404)
    result = request.form.get("pregnancy_result")
    check_date = request.form.get("pregnancy_check_date") or db.today_str()
    expected = request.form.get("expected_birth_date") or None
    db.execute("UPDATE breeding_records SET pregnancy_result=?, pregnancy_check_date=?, expected_birth_date=?, "
              "updated_at=datetime('now') WHERE id=?", (result, check_date, expected, breeding_id))
    if result == "pregnant":
        db.execute("UPDATE animals SET reproductive_status='pregnant' WHERE id=?", (br["dam_id"],))
        if expected:
            db.execute("INSERT INTO tasks (task_type, title, description, related_entity_type, related_entity_id, due_date, created_by) "
                      "VALUES (?,?,?,?,?,?,?)",
                      ("expected_birth", f"Expected birth: animal #{br['dam_id']}", "Prepare for kidding/lambing.",
                       "breeding_record", breeding_id, expected, g.user["id"]))
    elif result == "not_pregnant":
        db.execute("UPDATE animals SET reproductive_status='open' WHERE id=?", (br["dam_id"],))
        db.execute("UPDATE breeding_records SET status='not_pregnant' WHERE id=?", (breeding_id,))
    db.audit(g.user, "update", "breeding_record", breeding_id, f"Pregnancy check recorded: {result}")
    flash("Pregnancy check recorded.", "success")
    return redirect(url_for("breeding.index"))


@bp.route("/<int:breeding_id>/loss", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def record_loss(breeding_id):
    br = db.query("SELECT * FROM breeding_records WHERE id=?", (breeding_id,), one=True)
    if not br:
        abort(404)
    loss_date = request.form.get("loss_date") or db.today_str()
    notes = request.form.get("loss_notes")
    db.execute("UPDATE breeding_records SET status='lost', loss_date=?, loss_notes=?, updated_at=datetime('now') WHERE id=?",
              (loss_date, notes, breeding_id))
    db.execute("UPDATE animals SET reproductive_status='open' WHERE id=?", (br["dam_id"],))
    db.audit(g.user, "update", "breeding_record", breeding_id, f"Pregnancy loss recorded: {notes or ''}")
    flash("Pregnancy loss / abortion recorded.", "success")
    return redirect(url_for("breeding.index"))


@bp.route("/births")
@auth.login_required
def births():
    rows = db.query(
        "SELECT br.*, d.tag_id AS dam_tag, d.name AS dam_name FROM birth_records br "
        "JOIN animals d ON d.id=br.dam_id ORDER BY br.birth_date DESC")
    offspring_map = {}
    for r in rows:
        offspring_map[r["id"]] = db.query(
            "SELECT a.* FROM birth_offspring bo JOIN animals a ON a.id=bo.animal_id WHERE bo.birth_record_id=?", (r["id"],))
    pregnancies = db.query(
        "SELECT br.*, d.tag_id AS dam_tag, d.name AS dam_name FROM breeding_records br JOIN animals d ON d.id=br.dam_id "
        "WHERE br.status='active' AND br.pregnancy_result='pregnant' ORDER BY br.expected_birth_date")
    females = db.query("SELECT * FROM animals WHERE sex='female' AND status='active' ORDER BY tag_id")
    return render_template("births_index.html", rows=rows, offspring_map=offspring_map, pregnancies=pregnancies,
                           females=females, species_list=helpers.species_options(), breeds=helpers.breed_options(),
                           groups=helpers.group_options(), sites=helpers.site_options())


@bp.route("/births/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def add_birth():
    dam_id = request.form.get("dam_id", type=int)
    if not dam_id:
        flash("Select the dam.", "error")
        return redirect(url_for("breeding.births"))
    dam = db.query("SELECT * FROM animals WHERE id=?", (dam_id,), one=True)
    breeding_id = request.form.get("breeding_id", type=int) or None
    sire_id = None
    if breeding_id:
        br = db.query("SELECT sire_id FROM breeding_records WHERE id=?", (breeding_id,), one=True)
        sire_id = br["sire_id"] if br else None
    birth_date = request.form.get("birth_date") or db.today_str()
    born_alive = request.form.get("born_alive", type=int) or 0
    stillborn = request.form.get("stillborn", type=int) or 0
    total_born = born_alive + stillborn

    birth_id = db.execute(
        "INSERT INTO birth_records (breeding_id, dam_id, sire_id, birth_date, total_born, born_alive, stillborn, "
        "colostrum_notes, mothering_notes, weaning_date, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (breeding_id, dam_id, sire_id, birth_date, total_born, born_alive, stillborn,
         request.form.get("colostrum_notes"), request.form.get("mothering_notes"),
         request.form.get("weaning_date") or None, request.form.get("notes"), g.user["id"]))

    if breeding_id:
        db.execute("UPDATE breeding_records SET status='completed' WHERE id=?", (breeding_id,))
    db.execute("UPDATE animals SET reproductive_status='lactating', updated_at=datetime('now') WHERE id=?", (dam_id,))

    created = 0
    idx = 0
    while request.form.get(f"offspring_sex_{idx}") is not None:
        sex = request.form.get(f"offspring_sex_{idx}")
        tag = request.form.get(f"offspring_tag_{idx}", "").strip()
        name = request.form.get(f"offspring_name_{idx}") or None
        bweight = request.form.get(f"offspring_weight_{idx}", type=float)
        if sex in ("male", "female"):
            record_no = db.next_record_no()
            if not tag:
                tag = record_no
            birth_type = {1: "single", 2: "twin", 3: "triplet"}.get(total_born, "other" if total_born > 3 else "single")
            new_animal_id = db.execute(
                "INSERT INTO animals (record_no, tag_id, name, species_id, breed_id, sex, dob, birth_type, birth_weight, "
                "sire_id, dam_id, current_group_id, current_site_id, source, reproductive_status, status, "
                "is_demo, created_by, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_no, tag, name, dam["species_id"], dam["breed_id"], sex, birth_date, birth_type, bweight,
                 sire_id, dam_id, dam["current_group_id"], dam["current_site_id"], "born_on_farm", "young_stock",
                 "active", dam["is_demo"], g.user["id"], g.user["id"]))
            db.execute("INSERT INTO birth_offspring (birth_record_id, animal_id) VALUES (?,?)", (birth_id, new_animal_id))
            db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, notes, created_by) VALUES (?,?,?,?,?)",
                      (new_animal_id, "birth", birth_date, f"Born to dam #{dam_id}", g.user["id"]))
            if dam["current_group_id"]:
                db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, created_by) VALUES (?,?,?,?)",
                          (new_animal_id, dam["current_group_id"], birth_date, g.user["id"]))
            db.audit(g.user, "create", "animal", new_animal_id, f"Offspring {record_no} created from birth record, linked to dam #{dam_id}/sire #{sire_id}")
            created += 1
        idx += 1

    db.audit(g.user, "create", "birth_record", birth_id, f"Birth recorded for dam #{dam_id}: {born_alive} alive, {stillborn} stillborn, {created} offspring records created")
    flash(f"Birth recorded. {created} offspring record(s) created and linked to the dam/sire.", "success")
    return redirect(url_for("breeding.births"))
