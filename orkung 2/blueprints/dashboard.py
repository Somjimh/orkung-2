from flask import Blueprint, render_template, request, g
from datetime import date, timedelta
import calendar
import db
import helpers
import auth

bp = Blueprint("dashboard", __name__)


def _date_range_default():
    today = date.today()
    start = today - timedelta(days=30)
    return start.isoformat(), today.isoformat()


@bp.route("/")
@auth.login_required
def index():
    site_id = request.args.get("site_id", type=int)
    species_id = request.args.get("species_id", type=int)
    group_id = request.args.get("group_id", type=int)
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    if not date_from or not date_to:
        date_from, date_to = _date_range_default()

    where = ["1=1"]
    args = []
    if site_id:
        where.append("a.current_site_id=?"); args.append(site_id)
    if species_id:
        where.append("a.species_id=?"); args.append(species_id)
    if group_id:
        where.append("a.current_group_id=?"); args.append(group_id)
    where_sql = " AND ".join(where)

    animals = db.query(
        f"SELECT a.*, sp.name AS species_name, b.name AS breed_name, gr.name AS group_name "
        f"FROM animals a LEFT JOIN species sp ON sp.id=a.species_id "
        f"LEFT JOIN breeds b ON b.id=a.breed_id LEFT JOIN groups_ gr ON gr.id=a.current_group_id "
        f"WHERE {where_sql}", tuple(args))

    active = [a for a in animals if a["status"] == "active"]
    today = date.today()

    def age_days(a):
        d = db.parse_date(a["dob"])
        return (today - d).days if d else None

    young_stock = [a for a in active if (age_days(a) is not None and age_days(a) < 180)]
    breeding_animals = [a for a in active if a["reproductive_status"] in ("open", "pregnant", "lactating", "breeding")]
    pregnant = [a for a in active if a["reproductive_status"] == "pregnant"]
    females = [a for a in active if a["sex"] == "female"]
    males = [a for a in active if a["sex"] == "male"]

    by_species = {}
    for a in active:
        by_species.setdefault(a["species_name"] or "Unspecified", 0)
        by_species[a["species_name"] or "Unspecified"] += 1

    by_sex = {"Female": len(females), "Male": len(males)}

    by_breed = {}
    for a in active:
        by_breed.setdefault(a["breed_name"] or "Unrecorded", 0)
        by_breed[a["breed_name"] or "Unrecorded"] += 1

    by_group = {}
    for a in active:
        by_group.setdefault(a["group_name"] or "Unassigned", 0)
        by_group[a["group_name"] or "Unassigned"] += 1

    def age_class(a):
        d = age_days(a)
        if d is None:
            return "Unknown"
        if d < 180:
            return "Kid/Lamb (<6m)"
        if d < 365:
            return "Weaner/Grower (6-12m)"
        return "Adult (>12m)"

    by_age_class = {}
    for a in active:
        c = age_class(a)
        by_age_class.setdefault(c, 0)
        by_age_class[c] += 1

    # Births / status changes in the selected period
    births_in_period = db.query(
        "SELECT COUNT(*) c FROM birth_records WHERE date(birth_date) BETWEEN date(?) AND date(?)",
        (date_from, date_to), one=True)["c"]

    status_events = db.query(
        f"SELECT status, COUNT(*) c FROM animals a WHERE status != 'active' "
        f"AND date(status_date) BETWEEN date(?) AND date(?) AND {where_sql} GROUP BY status",
        tuple([date_from, date_to] + args))
    status_counts = {r["status"]: r["c"] for r in status_events}

    purchases_in_period = db.query(
        f"SELECT COUNT(*) c FROM animals a WHERE source='purchased' AND date(acquisition_date) BETWEEN date(?) AND date(?) AND {where_sql}",
        tuple([date_from, date_to] + args), one=True)["c"]

    # Treatments / vaccinations / expected births due
    window = 14
    due_edge = (today + timedelta(days=window)).isoformat()
    treatments_due = db.query(
        "SELECT COUNT(*) c FROM treatments WHERE follow_up_date IS NOT NULL AND date(follow_up_date) <= date(?)",
        (due_edge,), one=True)["c"]
    vaccinations_due = db.query(
        "SELECT COUNT(*) c FROM tasks WHERE task_type='vaccination' AND status='pending' AND date(due_date) <= date(?)",
        (due_edge,), one=True)["c"]
    expected_births = db.query(
        "SELECT COUNT(*) c FROM breeding_records WHERE status='active' AND pregnancy_result='pregnant' "
        "AND expected_birth_date IS NOT NULL AND date(expected_birth_date) <= date(?)",
        (due_edge,), one=True)["c"]

    # Weighing gaps + weight loss
    weigh_reminder_days = 60
    not_weighed = []
    losing_weight = []
    avg_weight_change = []
    for a in active:
        recs = db.query("SELECT * FROM weight_records WHERE animal_id=? ORDER BY measured_on", (a["id"],))
        if not recs:
            not_weighed.append(a)
            continue
        last = recs[-1]
        gap = db.days_between(last["measured_on"], today.isoformat())
        if gap is not None and gap > weigh_reminder_days:
            not_weighed.append(a)
        if len(recs) >= 2:
            prev = recs[-2]
            change = last["weight_kg"] - prev["weight_kg"]
            avg_weight_change.append(change)
            if change < 0:
                losing_weight.append(a)

    avg_change = round(sum(avg_weight_change) / len(avg_weight_change), 2) if avg_weight_change else None
    avg_current_weight = None
    latest_weights = []
    for a in active:
        last = db.query("SELECT weight_kg FROM weight_records WHERE animal_id=? ORDER BY measured_on DESC LIMIT 1", (a["id"],), one=True)
        if last:
            latest_weights.append(last["weight_kg"])
    if latest_weights:
        avg_current_weight = round(sum(latest_weights) / len(latest_weights), 1)

    # Incomplete records: missing tag, breed, dob/age, sex is required so skip, source
    incomplete = [a for a in active if not a["breed_id"] or not a["dob"] or not a["current_group_id"] or not a["current_site_id"]]

    recently_added = db.query(
        f"SELECT a.*, sp.name AS species_name FROM animals a LEFT JOIN species sp ON sp.id=a.species_id "
        f"WHERE {where_sql} ORDER BY a.created_at DESC LIMIT 8", tuple(args))

    upcoming_tasks = db.query(
        "SELECT t.*, u.full_name AS assignee FROM tasks t LEFT JOIN users u ON u.id=t.assigned_to "
        "WHERE t.status='pending' AND date(t.due_date) <= date(?) ORDER BY t.due_date LIMIT 10",
        (due_edge,))

    alerts = []
    for a in active:
        if a["status"] == "active":
            pass
    withdrawal_animals = db.query(
        "SELECT DISTINCT a.id, a.tag_id, a.name, MAX(t.meat_withdrawal_end) mw, MAX(t.milk_withdrawal_end) mlw "
        "FROM treatments t JOIN animals a ON a.id=t.animal_id "
        "WHERE (date(t.meat_withdrawal_end) >= date('now') OR date(t.milk_withdrawal_end) >= date('now')) "
        "GROUP BY a.id"
    )

    # Population-over-time (last 12 months)
    pop_points = []
    for i in range(11, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12; y -= 1
        month_end = date(y, m, calendar.monthrange(y, m)[1])
        cnt = 0
        for a in animals:
            entered = db.parse_date(a["dob"]) or db.parse_date(a["acquisition_date"]) or db.parse_date(a["created_at"])
            if entered is None or entered > month_end:
                continue
            exited = db.parse_date(a["status_date"]) if a["status"] != "active" else None
            if exited is not None and exited <= month_end:
                continue
            cnt += 1
        pop_points.append((f"{calendar.month_abbr[m]}", cnt))

    # Weight trend (avg recorded weight per month, last 12 months)
    weight_rows = db.query(
        "SELECT measured_on, weight_kg FROM weight_records w JOIN animals a ON a.id=w.animal_id WHERE 1=1"
    )
    monthly = {}
    for r in weight_rows:
        d = db.parse_date(r["measured_on"])
        if not d:
            continue
        key = (d.year, d.month)
        monthly.setdefault(key, []).append(r["weight_kg"])
    weight_points = []
    for i in range(11, -1, -1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12; y -= 1
        vals = monthly.get((y, m), [])
        avg = round(sum(vals) / len(vals), 1) if vals else 0
        weight_points.append((f"{calendar.month_abbr[m]}", avg))

    pop_chart = helpers.line_chart_svg(pop_points, color="#1c6238")
    weight_chart = helpers.line_chart_svg(weight_points, color="#b8763a")

    ctx = dict(
        total_active=len(active),
        by_species=by_species, by_sex=by_sex, by_breed=by_breed, by_group=by_group, by_age_class=by_age_class,
        young_stock_count=len(young_stock), breeding_count=len(breeding_animals), pregnant_count=len(pregnant),
        females_count=len(females), males_count=len(males),
        births_in_period=births_in_period, status_counts=status_counts, purchases_in_period=purchases_in_period,
        treatments_due=treatments_due, vaccinations_due=vaccinations_due, expected_births=expected_births,
        not_weighed=not_weighed, losing_weight=losing_weight, avg_change=avg_change, avg_current_weight=avg_current_weight,
        incomplete=incomplete, recently_added=recently_added, upcoming_tasks=upcoming_tasks,
        withdrawal_animals=withdrawal_animals,
        pop_chart=pop_chart, weight_chart=weight_chart,
        sites=helpers.site_options(), species_list=helpers.species_options(), groups=helpers.group_options(),
        f_site_id=site_id, f_species_id=species_id, f_group_id=group_id, f_date_from=date_from, f_date_to=date_to,
        today=today.isoformat(),
    )
    return render_template("dashboard.html", **ctx)
