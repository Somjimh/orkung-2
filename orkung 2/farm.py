"""Shared rules for the crops, work, payroll, store and asset modules.

Money is in KES. Costs are worked out from the records, never typed twice:

* Labour cost of a day = a casual's daily rate (half for a half day), or a
  monthly worker's salary divided by the pay divisor (26 working days by
  default, changeable on the payroll page).
* That day cost is split across the jobs the person logged that day, in
  proportion to the hours given (equal shares when no hours were given).
  A paid day with no job logged is shown as "no job recorded" so idle paid
  time is visible.
* Input cost = what the store issued to a block, at the price paid.
"""
from datetime import date, timedelta
import calendar
import db

ACTIVITIES = [
    "Land preparation", "Nursery", "Planting / transplanting", "Weeding", "Irrigation",
    "Fertiliser / compost spreading", "Spraying", "Pruning / desuckering", "Harvesting / picking",
    "Grading / packing", "Drying (chillies)", "Compost making", "Fodder cutting / hay",
    "Livestock care", "Construction / repairs", "Security / guard", "Transport / delivery",
    "Sukoon Camp", "Store / office", "Other",
]

CROPS = ["Habanero", "Cayenne", "Banana", "Napier", "Super napier", "Lucerne", "Boma Rhodes",
         "Natural grass (hay)", "Green grams", "Cowpeas", "Other"]

OUTPUT_UNITS = ["kg", "crates", "bunches", "bales", "rows", "plants", "wheelbarrows", "trips", "litres"]

ATTENDANCE = [("present", "Present"), ("half", "Half day"), ("absent", "Absent (unpaid)"),
              ("leave", "Leave"), ("sick", "Sick"), ("off", "Day off")]
ATT_LABEL = dict(ATTENDANCE)

STORE_CATEGORIES = [("medicine", "Medicine"), ("vaccine", "Vaccine"), ("chemical", "Crop chemical"),
                    ("fertiliser", "Fertiliser / manure"), ("seed", "Seed / seedlings"), ("feed", "Feed"),
                    ("fuel", "Fuel / oil"), ("supply", "Supply / other")]
CROP_CATEGORIES = ("chemical", "fertiliser", "seed", "fuel", "supply", "feed")

ASSET_CATEGORIES = ["Pump / borehole", "Solar", "Tank", "Irrigation", "Tool", "Sprayer", "Vehicle / motorbike",
                    "Machine", "Building", "Fence", "Furniture / camp", "Electronics", "Other"]
ASSET_CONDITIONS = [("good", "Good"), ("fair", "Fair"), ("needs_repair", "Needs repair"), ("broken", "Broken")]
ASSET_STATUS = [("in_use", "In use"), ("in_store", "In store"), ("out", "Checked out"), ("lost", "Lost / missing"),
                ("disposed", "Disposed")]

ENTERPRISES = ["Chillies", "Bananas", "Livestock", "Sukoon Camp", "Honey", "Fodder & compost", "Farm"]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def this_month():
    return date.today().strftime("%Y-%m")


def month_bounds(period):
    """'2026-10' -> ('2026-10-01', '2026-10-31'). Bad input -> current month."""
    try:
        y, m = (int(x) for x in (period or "").split("-")[:2])
        first = date(y, m, 1)
    except (ValueError, TypeError):
        first = date.today().replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first.isoformat(), last.isoformat()


def shift_month(period, n):
    start, _ = month_bounds(period)
    d = date.fromisoformat(start)
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1).strftime("%Y-%m")


def month_label(period):
    start, _ = month_bounds(period)
    return date.fromisoformat(start).strftime("%B %Y")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def blocks(active_only=True):
    return db.query("SELECT * FROM crop_blocks" + (" WHERE active=1" if active_only else "") + " ORDER BY code")


def workers(active_only=True):
    return db.query("SELECT * FROM workers" + (" WHERE active=1" if active_only else "") + " ORDER BY name")


def active_plantings():
    return db.query("SELECT p.*, b.code AS block_code FROM plantings p JOIN crop_blocks b ON b.id=p.block_id "
                    "WHERE p.status NOT IN ('finished','failed') ORDER BY b.code, p.planted_date")


def setting(key, default):
    row = db.query("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if not row or row["value"] in (None, ""):
        return default
    try:
        return type(default)(row["value"])
    except (TypeError, ValueError):
        return default


def pay_divisor():
    """Working days a month used to turn a monthly salary into a day cost and an absence deduction."""
    return setting("payroll_divisor", 26.0) or 26.0


def day_cost(worker, status):
    share = {"present": 1.0, "half": 0.5}.get(status, 0.0)
    if not share:
        return 0.0
    if worker["pay_type"] == "daily":
        return (worker["daily_rate"] or 0) * share
    return (worker["monthly_salary"] or 0) / pay_divisor() * share


def month_locked(work_date):
    """True when the pay run covering this date has been approved: attendance is then frozen."""
    period = (work_date or "")[:7]
    run = db.query("SELECT status FROM pay_runs WHERE period=?", (period,), one=True)
    return bool(run and run["status"] in ("approved", "paid"))


# ---------------------------------------------------------------------------
# Crop on a block on a date
# ---------------------------------------------------------------------------

def crop_lookup():
    """Returns f(block_id, date, planting_id) -> crop name or None."""
    rows = db.query("SELECT id, block_id, crop, planted_date, end_date FROM plantings ORDER BY planted_date")
    by_id = {r["id"]: r["crop"] for r in rows}
    by_block = {}
    for r in rows:
        by_block.setdefault(r["block_id"], []).append(r)

    def f(block_id, when, planting_id=None):
        if planting_id and planting_id in by_id:
            return by_id[planting_id]
        if not block_id:
            return None
        best = None
        for r in by_block.get(block_id, []):
            if r["planted_date"] <= when and (not r["end_date"] or r["end_date"] >= when):
                best = r["crop"]
        return best
    return f


# ---------------------------------------------------------------------------
# Labour and input costs
# ---------------------------------------------------------------------------

def labour_rows(date_from, date_to):
    """Every paid worker-day in the range, split across that day's jobs.

    Returns a list of dicts: work_date, worker_id, worker, activity, block_id,
    block_code, planting_id, crop, quantity, unit, hours, cost, status.
    """
    crop_of = crop_lookup()
    att = db.query("SELECT a.*, w.name, w.pay_type, w.daily_rate, w.monthly_salary FROM attendance a "
                   "JOIN workers w ON w.id=a.worker_id WHERE a.work_date BETWEEN ? AND ?", (date_from, date_to))
    logs = db.query("SELECT l.*, b.code AS block_code FROM work_logs l LEFT JOIN crop_blocks b ON b.id=l.block_id "
                    "WHERE l.work_date BETWEEN ? AND ?", (date_from, date_to))
    by_day = {}
    for l in logs:
        by_day.setdefault((l["worker_id"], l["work_date"]), []).append(l)
    out = []
    for a in att:
        cost = day_cost(a, a["status"])
        jobs = by_day.pop((a["worker_id"], a["work_date"]), [])
        if a["status"] not in ("present", "half"):
            for j in jobs:  # a job logged on an unpaid day: kept, at no cost, so it can be checked
                out.append(dict(work_date=j["work_date"], worker_id=j["worker_id"], worker=a["name"],
                                activity=j["activity"], block_id=j["block_id"], block_code=j["block_code"],
                                planting_id=j["planting_id"], crop=crop_of(j["block_id"], j["work_date"], j["planting_id"]),
                                quantity=j["quantity"], unit=j["unit"], hours=j["hours"], cost=0.0,
                                status=a["status"]))
            continue
        if not jobs:
            out.append(dict(work_date=a["work_date"], worker_id=a["worker_id"], worker=a["name"],
                            activity="No job recorded", block_id=None, block_code=None, planting_id=None,
                            crop=None, quantity=None, unit=None, hours=None, cost=cost, status=a["status"]))
            continue
        hours = [j["hours"] or 0 for j in jobs]
        total_h = sum(hours)
        for j, h in zip(jobs, hours):
            share = (h / total_h) if total_h else 1 / len(jobs)
            out.append(dict(work_date=j["work_date"], worker_id=j["worker_id"], worker=a["name"],
                            activity=j["activity"], block_id=j["block_id"], block_code=j["block_code"],
                            planting_id=j["planting_id"], crop=crop_of(j["block_id"], j["work_date"], j["planting_id"]),
                            quantity=j["quantity"], unit=j["unit"], hours=j["hours"], cost=cost * share,
                            status=a["status"]))
    # Jobs logged on a day with no attendance mark: shown at zero cost so they are not lost.
    for (wid, d), jobs in by_day.items():
        w = db.query("SELECT name FROM workers WHERE id=?", (wid,), one=True)
        for j in jobs:
            out.append(dict(work_date=d, worker_id=wid, worker=w["name"] if w else "?", activity=j["activity"],
                            block_id=j["block_id"], block_code=j["block_code"], planting_id=j["planting_id"],
                            crop=crop_of(j["block_id"], d, j["planting_id"]), quantity=j["quantity"], unit=j["unit"],
                            hours=j["hours"], cost=0.0, status="no attendance"))
    out.sort(key=lambda r: (r["work_date"], r["worker"]))
    return out


def input_rows(date_from, date_to):
    """Store items issued out (move_type 'used') in the range, with cost and the block they went to."""
    crop_of = crop_lookup()
    rows = db.query(
        "SELECT m.*, i.name AS item, i.unit, i.category, b.code AS block_code, w.name AS worker "
        "FROM stock_movements m JOIN stock_items i ON i.id=m.item_id "
        "LEFT JOIN crop_blocks b ON b.id=m.block_id LEFT JOIN workers w ON w.id=m.worker_id "
        "WHERE m.move_type='used' AND date(m.move_date) BETWEEN ? AND ? ORDER BY m.move_date, m.id",
        (date_from, date_to))
    out = []
    for r in rows:
        qty = -(r["qty"] or 0)
        out.append(dict(move_date=r["move_date"][:10], item=r["item"], unit=r["unit"], category=r["category"],
                        qty=qty, cost=qty * (r["unit_cost"] or 0), priced=r["unit_cost"] is not None,
                        block_id=r["block_id"], block_code=r["block_code"], worker=r["worker"],
                        crop=crop_of(r["block_id"], r["move_date"][:10], r["planting_id"]),
                        treatment=r["treatment_id"] is not None, notes=r["notes"]))
    return out


def block_summary(date_from, date_to):
    """Per block: labour days and cost, inputs cost, harvest kg. Plus a 'Not on a block' row."""
    lab = labour_rows(date_from, date_to)
    inp = [r for r in input_rows(date_from, date_to) if r["category"] in CROP_CATEGORIES or r["block_id"]]
    harv = db.query("SELECT block_id, SUM(kg) kg FROM harvests WHERE harvest_date BETWEEN ? AND ? GROUP BY block_id",
                    (date_from, date_to))
    hk = {r["block_id"]: r["kg"] for r in harv}
    rows = {}
    for b in blocks(active_only=False):
        rows[b["id"]] = dict(block=b, labour=0.0, labour_days=0.0, inputs=0.0, kg=hk.get(b["id"], 0) or 0)
    general = dict(block=None, labour=0.0, labour_days=0.0, inputs=0.0, kg=hk.get(None, 0) or 0)
    for r in lab:
        tgt = rows.get(r["block_id"], general)
        tgt["labour"] += r["cost"]
    for r in inp:
        tgt = rows.get(r["block_id"], general)
        tgt["inputs"] += r["cost"]
    for v in list(rows.values()) + [general]:
        v["total"] = v["labour"] + v["inputs"]
        v["per_kg"] = (v["total"] / v["kg"]) if v["kg"] else None
    keep = [v for v in rows.values() if v["block"]["active"] or v["total"] or v["kg"]]
    return keep, general


def crop_summary(date_from, date_to):
    """Per crop: labour, inputs, harvest kg, sales value, cash received, margin."""
    lab = labour_rows(date_from, date_to)
    inp = input_rows(date_from, date_to)
    data = {}

    def row(c):
        return data.setdefault(c or "Not linked to a crop", dict(labour=0.0, inputs=0.0, kg=0.0, sold_kg=0.0,
                                                                  sales=0.0, received=0.0))
    for r in lab:
        if r["crop"] or r["block_id"]:
            row(r["crop"])["labour"] += r["cost"]
    for r in inp:
        if r["crop"] or r["block_id"]:
            row(r["crop"])["inputs"] += r["cost"]
    for h in db.query("SELECT crop, SUM(kg) kg FROM harvests WHERE harvest_date BETWEEN ? AND ? GROUP BY crop",
                      (date_from, date_to)):
        row(h["crop"])["kg"] += h["kg"] or 0
    for s in db.query("SELECT crop, SUM(kg) kg, SUM(kg*price_per_kg) v, SUM(amount_paid) p FROM crop_sales "
                      "WHERE sale_date BETWEEN ? AND ? GROUP BY crop", (date_from, date_to)):
        r = row(s["crop"])
        r["sold_kg"] += s["kg"] or 0
        r["sales"] += s["v"] or 0
        r["received"] += s["p"] or 0
    for v in data.values():
        v["cost"] = v["labour"] + v["inputs"]
        v["margin"] = v["sales"] - v["cost"]
    return dict(sorted(data.items(), key=lambda kv: (kv[0] == "Not linked to a crop", kv[0])))


def rag(due_date, status, today=None):
    """Milestone colour: green done, red overdue, amber due within 14 days or flagged at risk."""
    today = today or date.today().isoformat()
    if status == "done":
        return "green", "Done"
    if status == "dropped":
        return "gray", "Dropped"
    if due_date and due_date < today:
        return "red", "Overdue"
    if status == "at_risk":
        return "amber", "At risk"
    if not due_date:
        return "gray", "No date set"
    if due_date <=(date.fromisoformat(today) + timedelta(days=14)).isoformat():
        return "amber", "Due soon"
    return "green", "On track"


def fmt(n):
    return "—" if n is None else f"{n:,.0f}"
