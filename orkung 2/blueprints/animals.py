import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from werkzeug.utils import secure_filename
from datetime import date
import db
import helpers
import auth

bp = Blueprint("animals", __name__, url_prefix="/animals")

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}


def _creates_parent_cycle(animal_id, proposed_parent_id):
    """Walk up the proposed parent's own ancestry to make sure the animal
    being edited does not appear in it (which would make it its own
    ancestor)."""
    if proposed_parent_id is None:
        return False
    if proposed_parent_id == animal_id:
        return True
    seen = set()
    current = proposed_parent_id
    depth = 0
    while current and depth < 30:
        if current in seen:
            break
        seen.add(current)
        row = db.query("SELECT sire_id, dam_id FROM animals WHERE id=?", (current,), one=True)
        if not row:
            break
        for p in (row["sire_id"], row["dam_id"]):
            if p == animal_id:
                return True
        # continue walking up via dam line (sufficient for cycle detection purposes)
        current = row["dam_id"] or row["sire_id"]
        depth += 1
    return False


def _find_active_tag_conflict(tag_id, exclude_id=None):
    if exclude_id:
        return db.query("SELECT * FROM animals WHERE tag_id=? AND status='active' AND id!=?", (tag_id, exclude_id), one=True)
    return db.query("SELECT * FROM animals WHERE tag_id=? AND status='active'", (tag_id,), one=True)


def _is_incomplete(a):
    return not a["breed_id"] or not a["dob"] or not a["current_group_id"] or not a["current_site_id"]


@bp.route("")
@auth.login_required
def list_view():
    q = request.args.get("q", "").strip()
    species_id = request.args.get("species_id", type=int)
    sex = request.args.get("sex")
    breed_id = request.args.get("breed_id", type=int)
    group_id = request.args.get("group_id", type=int)
    site_id = request.args.get("site_id", type=int)
    repro = request.args.get("reproductive_status")
    status = request.args.get("status", "active")
    incomplete = request.args.get("incomplete")

    where = ["1=1"]
    args = []
    if q:
        where.append("(a.tag_id LIKE ? OR a.name LIKE ? OR a.eid LIKE ? OR a.record_no LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like, like]
    if species_id:
        where.append("a.species_id=?"); args.append(species_id)
    if sex:
        where.append("a.sex=?"); args.append(sex)
    if breed_id:
        where.append("a.breed_id=?"); args.append(breed_id)
    if group_id:
        where.append("a.current_group_id=?"); args.append(group_id)
    if site_id:
        where.append("a.current_site_id=?"); args.append(site_id)
    if repro:
        where.append("a.reproductive_status=?"); args.append(repro)
    if status and status != "all":
        where.append("a.status=?"); args.append(status)

    rows = db.query(
        f"SELECT a.*, sp.name AS species_name, b.name AS breed_name, gr.name AS group_name, s.name AS site_name "
        f"FROM animals a LEFT JOIN species sp ON sp.id=a.species_id LEFT JOIN breeds b ON b.id=a.breed_id "
        f"LEFT JOIN groups_ gr ON gr.id=a.current_group_id LEFT JOIN sites s ON s.id=a.current_site_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.created_at DESC", tuple(args))

    if incomplete:
        rows = [r for r in rows if _is_incomplete(r)]

    return render_template("animals_list.html", animals=rows, q=q,
                            species_list=helpers.species_options(), breeds=helpers.breed_options(),
                            groups=helpers.group_options(), sites=helpers.site_options(),
                            f_species_id=species_id, f_sex=sex, f_breed_id=breed_id, f_group_id=group_id,
                            f_site_id=site_id, f_repro=repro, f_status=status)


def _animal_form_context(animal=None):
    return dict(
        animal=animal,
        species_list=helpers.species_options(),
        breeds=helpers.breed_options(),
        groups=helpers.group_options(),
        sites=helpers.site_options(),
        females=db.query("SELECT * FROM animals WHERE sex='female' AND status='active' ORDER BY tag_id"),
        males=db.query("SELECT * FROM animals WHERE sex='male' AND status='active' ORDER BY tag_id"),
    )


@bp.route("/add", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def add():
    if request.method == "POST":
        ok, animal_id = _save_animal(None)
        if ok:
            flash("Animal saved.", "success")
            return redirect(url_for("animals.profile", animal_id=animal_id))
        return render_template("animal_form.html", **_animal_form_context(), form=request.form, errors=g.get("form_errors", {}))
    return render_template("animal_form.html", **_animal_form_context(), form={}, errors={})


@bp.route("/<int:animal_id>/edit", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def edit(animal_id):
    animal = db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)
    if not animal:
        abort(404)
    if request.method == "POST":
        ok, _ = _save_animal(animal_id)
        if ok:
            flash("Animal updated.", "success")
            return redirect(url_for("animals.profile", animal_id=animal_id))
        return render_template("animal_form.html", **_animal_form_context(animal), form=request.form, errors=g.get("form_errors", {}))
    return render_template("animal_form.html", **_animal_form_context(animal), form=animal, errors={})


def _save_animal(animal_id):
    """Shared create/update handler with validation. Returns (ok, animal_id)."""
    f = request.form
    errors = {}

    tag_id = f.get("tag_id", "").strip()
    species_id = f.get("species_id", type=int)
    sex = f.get("sex")
    if not tag_id:
        errors["tag_id"] = "Ear-tag / ID number is required."
    if not species_id:
        errors["species_id"] = "Species is required."
    if sex not in ("female", "male"):
        errors["sex"] = "Sex is required."

    dob = f.get("dob") or None
    if dob and not db.parse_date(dob):
        errors["dob"] = "Enter a valid date."
    birth_weight = f.get("birth_weight") or None
    if birth_weight:
        try:
            birth_weight = float(birth_weight)
        except ValueError:
            errors["birth_weight"] = "Must be a number."

    sire_id = f.get("sire_id", type=int) or None
    dam_id = f.get("dam_id", type=int) or None
    if sire_id and animal_id and sire_id == animal_id:
        errors["sire_id"] = "An animal cannot be its own sire."
    if dam_id and animal_id and dam_id == animal_id:
        errors["dam_id"] = "An animal cannot be its own dam."
    if animal_id and sire_id and _creates_parent_cycle(animal_id, sire_id):
        errors["sire_id"] = "This would create an impossible (circular) parentage."
    if animal_id and dam_id and _creates_parent_cycle(animal_id, dam_id):
        errors["dam_id"] = "This would create an impossible (circular) parentage."
    if sire_id:
        sire = db.query("SELECT sex FROM animals WHERE id=?", (sire_id,), one=True)
        if sire and sire["sex"] != "male":
            errors["sire_id"] = "Selected sire is not recorded as male."
    if dam_id:
        dam = db.query("SELECT sex FROM animals WHERE id=?", (dam_id,), one=True)
        if dam and dam["sex"] != "female":
            errors["dam_id"] = "Selected dam is not recorded as female."

    override_dup = f.get("override_duplicate_tag") == "on" and g.user["role"] == "admin"
    conflict = _find_active_tag_conflict(tag_id, exclude_id=animal_id) if tag_id else None
    if conflict and not override_dup:
        errors["tag_id"] = (f"This tag is already in use by active animal {conflict['record_no']} "
                             f"({helpers.animal_label(conflict)}).")
        if g.user["role"] == "admin":
            errors["tag_id"] += " Tick the override box to resolve and save anyway."

    if errors:
        g.form_errors = errors
        return False, None

    photo_path = None
    file = request.files.get("photo")
    if file and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext in ALLOWED_IMAGE_EXT:
            fname = f"{uuid.uuid4().hex}.{ext}"
            from flask import current_app
            file.save(os.path.join(current_app.config["UPLOAD_DIR"], fname))
            photo_path = fname

    fields = dict(
        tag_id=tag_id, eid=f.get("eid") or None, name=f.get("name") or None,
        species_id=species_id, breed_id=f.get("breed_id", type=int) or None, sex=sex,
        dob=dob, dob_estimated=1 if f.get("dob_estimated") == "on" else 0,
        birth_type=f.get("birth_type") or None, birth_weight=birth_weight,
        color_markings=f.get("color_markings") or None,
        sire_id=sire_id, dam_id=dam_id,
        current_group_id=f.get("current_group_id", type=int) or None,
        current_site_id=f.get("current_site_id", type=int) or None,
        source=f.get("source") or "born_on_farm",
        acquisition_date=f.get("acquisition_date") or None,
        supplier=f.get("supplier") or None,
        reproductive_status=f.get("reproductive_status") or "not_breeding",
        notes=f.get("notes") or None,
    )

    if animal_id is None:
        record_no = db.next_record_no()
        cols = list(fields.keys()) + ["record_no", "photo_path", "created_by", "updated_by"]
        placeholders = ",".join("?" for _ in cols)
        values = list(fields.values()) + [record_no, photo_path, g.user["id"], g.user["id"]]
        new_id = db.execute(f"INSERT INTO animals ({','.join(cols)}) VALUES ({placeholders})", values)
        if fields["current_group_id"]:
            db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, created_by) VALUES (?,?,?,?)",
                       (new_id, fields["current_group_id"], db.today_str(), g.user["id"]))
        db.audit(g.user, "create", "animal", new_id, f"Added animal {record_no} ({tag_id})")
        if conflict and override_dup:
            db.audit(g.user, "duplicate_tag_override", "animal", new_id,
                     f"Admin override: tag {tag_id} also used by animal #{conflict['id']}")
        _maybe_generate_movement_event(new_id, None, fields["current_group_id"], fields["current_site_id"])
        return True, new_id
    else:
        before = db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)
        set_sql = ", ".join(f"{k}=?" for k in fields.keys())
        values = list(fields.values()) + [g.user["id"], animal_id]
        sql = f"UPDATE animals SET {set_sql}, updated_by=?, updated_at=datetime('now') WHERE id=?"
        # updated_at handled separately since datetime('now') isn't a bound param
        sql = f"UPDATE animals SET {set_sql}, updated_by=?, updated_at=datetime('now') WHERE id=?"
        db.execute(sql, values)
        if photo_path:
            db.execute("UPDATE animals SET photo_path=? WHERE id=?", (photo_path, animal_id))
        db.audit(g.user, "update", "animal", animal_id, f"Updated animal {before['record_no']}",
                 details={"before": dict(before), "after": fields})
        if conflict and override_dup:
            db.audit(g.user, "duplicate_tag_override", "animal", animal_id,
                     f"Admin override: tag {tag_id} also used by animal #{conflict['id']}")
        if before["current_group_id"] != fields["current_group_id"]:
            _maybe_generate_movement_event(animal_id, before["current_group_id"], fields["current_group_id"], fields["current_site_id"])
        return True, animal_id


def _maybe_generate_movement_event(animal_id, from_group_id, to_group_id, to_site_id):
    if to_group_id and from_group_id != to_group_id:
        db.execute("UPDATE group_memberships SET end_date=? WHERE animal_id=? AND group_id=? AND end_date IS NULL",
                   (db.today_str(), animal_id, from_group_id))
        db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, created_by) VALUES (?,?,?,?)",
                   (animal_id, to_group_id, db.today_str(), g.user["id"]))
        db.execute(
            "INSERT INTO animal_events (animal_id, event_type, event_date, from_group_id, to_group_id, to_site_id, created_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (animal_id, "group_move", db.today_str(), from_group_id, to_group_id, to_site_id, g.user["id"]))


@bp.route("/<int:animal_id>")
@auth.login_required
def profile(animal_id):
    a = db.query(
        "SELECT a.*, sp.name AS species_name, b.name AS breed_name, gr.name AS group_name, s.name AS site_name "
        "FROM animals a LEFT JOIN species sp ON sp.id=a.species_id LEFT JOIN breeds b ON b.id=a.breed_id "
        "LEFT JOIN groups_ gr ON gr.id=a.current_group_id LEFT JOIN sites s ON s.id=a.current_site_id "
        "WHERE a.id=?", (animal_id,), one=True)
    if not a:
        abort(404)
    sire = db.query("SELECT * FROM animals WHERE id=?", (a["sire_id"],), one=True) if a["sire_id"] else None
    dam = db.query("SELECT * FROM animals WHERE id=?", (a["dam_id"],), one=True) if a["dam_id"] else None
    offspring = db.query("SELECT * FROM animals WHERE sire_id=? OR dam_id=? ORDER BY dob", (animal_id, animal_id))

    weights = db.query(
        "SELECT w.*, u.full_name AS recorder_name FROM weight_records w LEFT JOIN users u ON u.id=w.created_by "
        "WHERE w.animal_id=? ORDER BY w.measured_on", (animal_id,))
    weight_with_change = []
    prev = None
    for w in weights:
        change = None
        adg = None
        if prev:
            change = round(w["weight_kg"] - prev["weight_kg"], 2)
            days = db.days_between(prev["measured_on"], w["measured_on"])
            if days:
                adg = round((w["weight_kg"] - prev["weight_kg"]) / days, 3)
        weight_with_change.append(dict(row=w, change=change, adg=adg))
        prev = w
    weight_chart = helpers.line_chart_svg([(w["measured_on"][5:], w["weight_kg"]) for w in weights], color="#1c6238")

    breeding = db.query("SELECT * FROM breeding_records WHERE dam_id=? ORDER BY service_date DESC", (animal_id,))
    births = db.query("SELECT * FROM birth_records WHERE dam_id=? ORDER BY birth_date DESC", (animal_id,))

    health_obs = db.query("SELECT * FROM health_observations WHERE animal_id=? ORDER BY observation_date DESC", (animal_id,))
    treatments = db.query("SELECT t.*, m.name AS medicine_catalog_name FROM treatments t LEFT JOIN medicines m ON m.id=t.medicine_id "
                          "WHERE t.animal_id=? ORDER BY t.start_date DESC", (animal_id,))

    events = db.query("SELECT * FROM animal_events WHERE animal_id=? ORDER BY event_date DESC, id DESC", (animal_id,))
    group_history = db.query(
        "SELECT gm.*, g.name AS group_name FROM group_memberships gm LEFT JOIN groups_ g ON g.id=gm.group_id "
        "WHERE gm.animal_id=? ORDER BY gm.start_date DESC", (animal_id,))

    attachments = db.query("SELECT * FROM attachments WHERE entity_type='animal' AND entity_id=? ORDER BY uploaded_at DESC", (animal_id,))
    comments = db.query("SELECT c.*, u.full_name FROM comments c LEFT JOIN users u ON u.id=c.created_by WHERE c.animal_id=? ORDER BY c.created_at DESC", (animal_id,))

    audit_rows = db.query("SELECT * FROM audit_log WHERE entity_type='animal' AND entity_id=? ORDER BY ts DESC LIMIT 50", (animal_id,))

    # Chronological timeline (merge several sources)
    timeline = []
    for w in weights:
        timeline.append(dict(date=w["measured_on"], title=f"Weight recorded: {w['weight_kg']} kg", kind="weight"))
    for e in events:
        timeline.append(dict(date=e["event_date"], title=e["event_type"].replace("_", " ").title(), kind="event"))
    for t in treatments:
        timeline.append(dict(date=t["start_date"], title=f"Treatment: {t['treatment_type'] or t['medicine_name'] or 'recorded'}", kind="health"))
    for h in health_obs:
        timeline.append(dict(date=h["observation_date"], title=f"Health observation: {h['symptoms'] or ''}"[:80], kind="health"))
    for b in births:
        timeline.append(dict(date=b["birth_date"], title=f"Gave birth ({b['born_alive']} alive)", kind="birth"))
    if a["dob"]:
        timeline.append(dict(date=a["dob"], title="Born" if a["source"] == "born_on_farm" else "Date of birth", kind="birth"))
    if a["acquisition_date"]:
        timeline.append(dict(date=a["acquisition_date"], title=f"{a['source'].replace('_',' ').title()}", kind="event"))
    timeline.sort(key=lambda x: x["date"] or "", reverse=True)

    tab = request.args.get("tab", "overview")
    return render_template("animal_profile.html", a=a, sire=sire, dam=dam, offspring=offspring,
                           weights=weight_with_change, weight_chart=weight_chart,
                           breeding=breeding, births=births, health_obs=health_obs, treatments=treatments,
                           events=events, group_history=group_history, attachments=attachments,
                           comments=comments, audit_rows=audit_rows, timeline=timeline, tab=tab,
                           today_str=db.today_str())


@bp.route("/<int:animal_id>/status", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def change_status(animal_id):
    animal = db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)
    if not animal:
        abort(404)
    new_status = request.form.get("status")
    event_date = request.form.get("event_date") or db.today_str()
    notes = request.form.get("notes")
    valid = {"active", "sold", "transferred", "slaughtered", "missing", "deceased"}
    if new_status not in valid:
        flash("Invalid status.", "error")
        return redirect(url_for("animals.profile", animal_id=animal_id))

    db.execute("UPDATE animals SET status=?, status_date=?, status_notes=?, updated_by=?, updated_at=datetime('now') WHERE id=?",
              (new_status, event_date, notes, g.user["id"], animal_id))
    event_map = {"sold": "sale", "transferred": "transfer", "slaughtered": "slaughter",
                 "missing": "missing", "deceased": "death", "active": "return"}
    db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, notes, created_by) VALUES (?,?,?,?,?)",
              (animal_id, event_map.get(new_status, "status_change"), event_date, notes, g.user["id"]))
    db.audit(g.user, "status_change", "animal", animal_id,
             f"{animal['record_no']} status changed {animal['status']} -> {new_status}",
             details={"from": animal["status"], "to": new_status})
    flash(f"Status updated to {new_status}. Full history is preserved.", "success")
    return redirect(url_for("animals.profile", animal_id=animal_id))


@bp.route("/<int:animal_id>/tag-replace", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def tag_replace(animal_id):
    animal = db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)
    if not animal:
        abort(404)
    new_tag = request.form.get("new_tag", "").strip()
    if not new_tag:
        flash("New tag number is required.", "error")
        return redirect(url_for("animals.profile", animal_id=animal_id))
    conflict = _find_active_tag_conflict(new_tag, exclude_id=animal_id)
    if conflict and g.user["role"] != "admin":
        flash(f"That tag is already in use by {conflict['record_no']}.", "error")
        return redirect(url_for("animals.profile", animal_id=animal_id))
    db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, old_tag, new_tag, created_by) VALUES (?,?,?,?,?,?)",
              (animal_id, "tag_replacement", db.today_str(), animal["tag_id"], new_tag, g.user["id"]))
    db.execute("UPDATE animals SET tag_id=?, updated_by=?, updated_at=datetime('now') WHERE id=?", (new_tag, g.user["id"], animal_id))
    db.audit(g.user, "update", "animal", animal_id, f"Tag replaced {animal['tag_id']} -> {new_tag}")
    flash("Tag replacement recorded.", "success")
    return redirect(url_for("animals.profile", animal_id=animal_id))


@bp.route("/<int:animal_id>/comment", methods=["POST"])
@auth.login_required
def add_comment(animal_id):
    body = request.form.get("body", "").strip()
    if body:
        db.execute("INSERT INTO comments (animal_id, body, created_by) VALUES (?,?,?)", (animal_id, body, g.user["id"]))
        db.audit(g.user, "create", "comment", animal_id, "Added comment/observation")
    return redirect(url_for("animals.profile", animal_id=animal_id, tab="timeline"))


@bp.route("/<int:animal_id>/attachment", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.STAFF)
def add_attachment(animal_id):
    file = request.files.get("file")
    if file and file.filename:
        from flask import current_app
        fname = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
        file.save(os.path.join(current_app.config["UPLOAD_DIR"], fname))
        db.execute("INSERT INTO attachments (entity_type, entity_id, filename, stored_path, notes, uploaded_by) VALUES (?,?,?,?,?,?)",
                  ("animal", animal_id, file.filename, fname, request.form.get("notes"), g.user["id"]))
        db.audit(g.user, "create", "attachment", animal_id, f"Uploaded attachment {file.filename}")
        flash("Attachment uploaded.", "success")
    return redirect(url_for("animals.profile", animal_id=animal_id, tab="documents"))


@bp.route("/<int:animal_id>/history.csv")
@auth.login_required
def export_history_csv(animal_id):
    a = db.query("SELECT * FROM animals WHERE id=?", (animal_id,), one=True)
    if not a:
        abort(404)
    rows = [("record_type", "date", "detail")]
    for w in db.query("SELECT * FROM weight_records WHERE animal_id=? ORDER BY measured_on", (animal_id,)):
        rows.append(("weight", w["measured_on"], f"{w['weight_kg']} kg"))
    for e in db.query("SELECT * FROM animal_events WHERE animal_id=? ORDER BY event_date", (animal_id,)):
        rows.append(("event", e["event_date"], e["event_type"]))
    for h in db.query("SELECT * FROM health_observations WHERE animal_id=? ORDER BY observation_date", (animal_id,)):
        rows.append(("health_observation", h["observation_date"], h["symptoms"] or ""))
    for t in db.query("SELECT * FROM treatments WHERE animal_id=? ORDER BY start_date", (animal_id,)):
        rows.append(("treatment", t["start_date"], t["treatment_type"] or t["medicine_name"] or ""))
    for b in db.query("SELECT * FROM birth_records WHERE dam_id=? ORDER BY birth_date", (animal_id,)):
        rows.append(("birth", b["birth_date"], f"{b['born_alive']} born alive"))
    db.audit(g.user, "export", "animal", animal_id, f"Exported history for {a['record_no']}")
    return helpers.csv_response(f"{a['record_no']}_history.csv", rows[0], rows[1:])
