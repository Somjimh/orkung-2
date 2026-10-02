from flask import Blueprint, render_template, request, g
import db
import helpers
import auth

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _out(title, header, rows, filename, description=""):
    if request.args.get("export") in ("csv", "xlsx"):
        db.audit(g.user, "export", "report", summary=f"Exported report: {title}")
        if request.args.get("export") == "xlsx":
            return helpers.xlsx_response(filename.replace(".csv", ".xlsx"), header, rows, sheet_name=title[:31])
        return helpers.csv_response(filename, header, rows)
    return render_template("report_view.html", title=title, header=header, rows=rows, description=description,
                           filename=filename)


def _filters():
    return dict(
        species_id=request.args.get("species_id", type=int),
        group_id=request.args.get("group_id", type=int),
        site_id=request.args.get("site_id", type=int),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        status=request.args.get("status"),
    )


def _animal_where(f, alias="a"):
    where = ["1=1"]; args = []
    if f["species_id"]:
        where.append(f"{alias}.species_id=?"); args.append(f["species_id"])
    if f["group_id"]:
        where.append(f"{alias}.current_group_id=?"); args.append(f["group_id"])
    if f["site_id"]:
        where.append(f"{alias}.current_site_id=?"); args.append(f["site_id"])
    if f["status"]:
        where.append(f"{alias}.status=?"); args.append(f["status"])
    return where, args


@bp.route("")
@auth.login_required
def index():
    return render_template("reports_index.html")


@bp.route("/inventory")
@auth.login_required
def inventory():
    active = db.query("SELECT status, COUNT(*) c FROM animals GROUP BY status")
    by_species = db.query("SELECT sp.name, COUNT(*) c FROM animals a JOIN species sp ON sp.id=a.species_id WHERE a.status='active' GROUP BY sp.name")
    header = ["Category", "Count"]
    rows = [("Active " + r["status"] if False else r["status"].capitalize(), r["c"]) for r in active]
    rows += [(f"Active {r['name']}", r["c"]) for r in by_species]
    return _out("Current Livestock Inventory", header, rows, "inventory.csv")


@bp.route("/composition")
@auth.login_required
def composition():
    f = _filters(); where, args = _animal_where(f)
    where.append("a.status='active'")
    rows = db.query(
        f"SELECT sp.name species, b.name breed, a.sex, gr.name grp, s.name site, COUNT(*) c "
        f"FROM animals a JOIN species sp ON sp.id=a.species_id LEFT JOIN breeds b ON b.id=a.breed_id "
        f"LEFT JOIN groups_ gr ON gr.id=a.current_group_id LEFT JOIN sites s ON s.id=a.current_site_id "
        f"WHERE {' AND '.join(where)} GROUP BY sp.name, b.name, a.sex, gr.name, s.name ORDER BY sp.name, b.name", tuple(args))
    header = ["Species", "Breed", "Sex", "Group", "Site", "Count"]
    data = [(r["species"], r["breed"] or "", r["sex"], r["grp"] or "", r["site"] or "", r["c"]) for r in rows]
    return _out("Herd Composition", header, data, "herd_composition.csv")


@bp.route("/animals")
@auth.login_required
def export_animals():
    f = _filters(); where, args = _animal_where(f)
    rows = db.query(
        f"SELECT a.record_no, a.tag_id, a.eid, a.name, sp.name species, b.name breed, a.sex, a.dob, a.status, "
        f"gr.name grp, s.name site FROM animals a JOIN species sp ON sp.id=a.species_id LEFT JOIN breeds b ON b.id=a.breed_id "
        f"LEFT JOIN groups_ gr ON gr.id=a.current_group_id LEFT JOIN sites s ON s.id=a.current_site_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.record_no", tuple(args))
    header = ["Record #", "Tag", "EID", "Name", "Species", "Breed", "Sex", "DOB", "Status", "Group", "Site"]
    data = [(r["record_no"], r["tag_id"], r["eid"] or "", r["name"] or "", r["species"], r["breed"] or "",
             r["sex"], r["dob"] or "", r["status"], r["grp"] or "", r["site"] or "") for r in rows]
    return _out("Animal Register", header, data, "animal_register.csv")


@bp.route("/incomplete")
@auth.login_required
def incomplete():
    rows = db.query(
        "SELECT a.record_no, a.tag_id, sp.name species, a.breed_id, a.dob, a.current_group_id, a.current_site_id "
        "FROM animals a JOIN species sp ON sp.id=a.species_id WHERE a.status='active'")
    missing = []
    for r in rows:
        gaps = []
        if not r["breed_id"]: gaps.append("breed")
        if not r["dob"]: gaps.append("date of birth")
        if not r["current_group_id"]: gaps.append("group")
        if not r["current_site_id"]: gaps.append("site")
        if gaps:
            missing.append((r["record_no"], r["tag_id"], r["species"], ", ".join(gaps)))
    header = ["Record #", "Tag", "Species", "Missing fields"]
    return _out("Incomplete Records", header, missing, "incomplete_records.csv")


@bp.route("/weights")
@auth.login_required
def weight_history():
    rows = db.query(
        "SELECT a.record_no, a.tag_id, "
        "(SELECT weight_kg FROM weight_records w2 WHERE w2.animal_id=a.id ORDER BY measured_on ASC LIMIT 1) first_w, "
        "(SELECT measured_on FROM weight_records w2 WHERE w2.animal_id=a.id ORDER BY measured_on ASC LIMIT 1) first_d, "
        "(SELECT weight_kg FROM weight_records w2 WHERE w2.animal_id=a.id ORDER BY measured_on DESC LIMIT 1) last_w, "
        "(SELECT measured_on FROM weight_records w2 WHERE w2.animal_id=a.id ORDER BY measured_on DESC LIMIT 1) last_d "
        "FROM animals a WHERE a.status='active'")
    header = ["Record #", "Tag", "First weight", "First date", "Latest weight", "Latest date", "Total gain (kg)", "ADG (kg/day)"]
    data = []
    for r in rows:
        if r["first_w"] is None:
            continue
        gain = round(r["last_w"] - r["first_w"], 2) if r["last_w"] is not None else None
        days = db.days_between(r["first_d"], r["last_d"])
        adg = round(gain / days, 3) if (gain is not None and days) else None
        data.append((r["record_no"], r["tag_id"], r["first_w"], r["first_d"], r["last_w"], r["last_d"], gain, adg))
    return _out("Weight History & Growth", header, data, "weight_growth.csv")


@bp.route("/breeding")
@auth.login_required
def breeding_performance():
    rows = db.query(
        "SELECT d.tag_id dam_tag, br.method, br.service_date, br.pregnancy_result, br.expected_birth_date, br.status "
        "FROM breeding_records br JOIN animals d ON d.id=br.dam_id ORDER BY br.service_date DESC")
    total = len(rows)
    pregnant = sum(1 for r in rows if r["pregnancy_result"] == "pregnant")
    header = ["Dam", "Method", "Service date", "Pregnancy result", "Expected birth", "Status"]
    data = [(r["dam_tag"], r["method"], r["service_date"], r["pregnancy_result"] or "pending", r["expected_birth_date"] or "", r["status"]) for r in rows]
    desc = f"{total} service(s) recorded; {pregnant} confirmed pregnant ({round(pregnant/total*100,1) if total else 0}%)."
    return _out("Breeding Performance", header, data, "breeding_performance.csv", description=desc)


@bp.route("/births")
@auth.login_required
def births_report():
    rows = db.query(
        "SELECT d.tag_id dam_tag, b.birth_date, b.total_born, b.born_alive, b.stillborn, b.weaning_date "
        "FROM birth_records b JOIN animals d ON d.id=b.dam_id ORDER BY b.birth_date DESC")
    header = ["Dam", "Birth date", "Total born", "Born alive", "Stillborn", "Weaning date"]
    data = [(r["dam_tag"], r["birth_date"], r["total_born"], r["born_alive"], r["stillborn"], r["weaning_date"] or "") for r in rows]
    return _out("Births & Offspring", header, data, "births.csv")


@bp.route("/mortality")
@auth.login_required
def mortality():
    rows = db.query("SELECT record_no, tag_id, species_id, status_date, status_notes FROM animals WHERE status='deceased' ORDER BY status_date DESC")
    header = ["Record #", "Tag", "Date", "Notes"]
    data = [(r["record_no"], r["tag_id"], r["status_date"] or "", r["status_notes"] or "") for r in rows]
    return _out("Mortality", header, data, "mortality.csv")


@bp.route("/movements-report")
@auth.login_required
def movements_report():
    sales = db.query("SELECT record_no, tag_id, status_date, status_notes FROM animals WHERE status IN ('sold','transferred','slaughtered') ORDER BY status_date DESC")
    purchases = db.query("SELECT record_no, tag_id, acquisition_date, supplier FROM animals WHERE source='purchased' ORDER BY acquisition_date DESC")
    header = ["Type", "Record #", "Tag", "Date", "Detail"]
    data = [("Sale/Transfer/Slaughter", r["record_no"], r["tag_id"], r["status_date"] or "", r["status_notes"] or "") for r in sales]
    data += [("Purchase", r["record_no"], r["tag_id"], r["acquisition_date"] or "", r["supplier"] or "") for r in purchases]
    return _out("Purchases, Transfers & Sales", header, data, "movements_report.csv")


@bp.route("/health-history")
@auth.login_required
def health_history():
    rows = db.query(
        "SELECT a.record_no, a.tag_id, t.start_date, t.treatment_type, t.medicine_name, m.name catalog_name, t.result "
        "FROM treatments t JOIN animals a ON a.id=t.animal_id LEFT JOIN medicines m ON m.id=t.medicine_id ORDER BY t.start_date DESC")
    header = ["Record #", "Tag", "Date", "Treatment", "Medicine", "Result"]
    data = [(r["record_no"], r["tag_id"], r["start_date"], r["treatment_type"] or "", r["catalog_name"] or r["medicine_name"] or "", r["result"] or "") for r in rows]
    return _out("Health & Treatment History", header, data, "health_history.csv")


@bp.route("/vaccinations")
@auth.login_required
def vaccinations():
    rows = db.query(
        "SELECT a.record_no, a.tag_id, t.start_date, m.name FROM treatments t JOIN animals a ON a.id=t.animal_id "
        "JOIN medicines m ON m.id=t.medicine_id WHERE m.type='vaccine' ORDER BY t.start_date DESC")
    header = ["Record #", "Tag", "Date", "Vaccine"]
    data = [(r["record_no"], r["tag_id"], r["start_date"], r["name"]) for r in rows]
    return _out("Vaccination Records", header, data, "vaccinations.csv")


@bp.route("/withdrawal")
@auth.login_required
def withdrawal():
    rows = db.query(
        "SELECT a.record_no, a.tag_id, t.start_date, t.meat_withdrawal_end, t.milk_withdrawal_end "
        "FROM treatments t JOIN animals a ON a.id=t.animal_id "
        "WHERE t.meat_withdrawal_end IS NOT NULL OR t.milk_withdrawal_end IS NOT NULL ORDER BY t.start_date DESC")
    header = ["Record #", "Tag", "Treatment date", "Meat withdrawal ends", "Milk withdrawal ends"]
    data = [(r["record_no"], r["tag_id"], r["start_date"], r["meat_withdrawal_end"] or "", r["milk_withdrawal_end"] or "") for r in rows]
    return _out("Medicine Withdrawal Records", header, data, "withdrawal.csv")


@bp.route("/group-membership")
@auth.login_required
def group_membership():
    rows = db.query(
        "SELECT g.name grp, a.record_no, a.tag_id, gm.start_date, gm.end_date FROM group_memberships gm "
        "JOIN groups_ g ON g.id=gm.group_id JOIN animals a ON a.id=gm.animal_id ORDER BY g.name, gm.start_date DESC")
    header = ["Group", "Record #", "Tag", "From", "To"]
    data = [(r["grp"], r["record_no"], r["tag_id"], r["start_date"], r["end_date"] or "current") for r in rows]
    return _out("Group Membership", header, data, "group_membership.csv")


@bp.route("/by-site")
@auth.login_required
def by_site():
    rows = db.query(
        "SELECT s.name site, a.record_no, a.tag_id, sp.name species, a.status FROM animals a "
        "LEFT JOIN sites s ON s.id=a.current_site_id JOIN species sp ON sp.id=a.species_id ORDER BY s.name, a.record_no")
    header = ["Site", "Record #", "Tag", "Species", "Status"]
    data = [(r["site"] or "Unassigned", r["record_no"], r["tag_id"], r["species"], r["status"]) for r in rows]
    return _out("Animals by Site", header, data, "by_site.csv")


@bp.route("/audit")
@auth.login_required
@auth.require_roles("admin", "viewer")
def audit_report():
    rows = db.query("SELECT ts, username, action, entity_type, entity_id, summary FROM audit_log ORDER BY ts DESC LIMIT 1000")
    header = ["Timestamp", "User", "Action", "Entity", "Entity ID", "Summary"]
    data = [(r["ts"], r["username"], r["action"], r["entity_type"], r["entity_id"], r["summary"] or "") for r in rows]
    return _out("User Activity & Audit History", header, data, "audit_history.csv")


@bp.route("/tasks-report")
@auth.login_required
def tasks_report():
    status = request.args.get("status", "pending")
    where, args = ("1=1", ()) if status == "all" else ("t.status=?", (status,))
    rows = db.query(
        "SELECT t.*, u.full_name AS assignee FROM tasks t LEFT JOIN users u ON u.id=t.assigned_to "
        f"WHERE {where} ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, t.due_date, t.title",
        args)
    today = db.today_str()
    header = ["Priority", "Due", "Task", "Details", "Status", "Assigned to", "Done"]
    data = [(r["priority"].capitalize(),
             r["due_date"] + (" (overdue)" if r["status"] == "pending" and r["due_date"] < today else ""),
             r["title"], r["description"] or "", r["status"].capitalize(), r["assignee"] or "", "")
            for r in rows]
    label = "all tasks" if status == "all" else f"{status} tasks"
    return _out("Tasks & Alerts", header, data, "tasks_alerts.csv",
                description=f"Showing {label}, most urgent first. The 'Done' column is left blank for ticking on a printed copy.")
