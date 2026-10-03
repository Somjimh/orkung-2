"""Milestones: the reset plan's gates and targets, each with a date, an owner
and a red / amber / green status, so slippage shows early."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import auth
import farm

bp = Blueprint("milestones", __name__, url_prefix="/milestones")

VIEW = tuple(r for r in auth.ROLES if r not in ("vet", "worker"))

# From the Orkung reset plans (chilli, banana, livestock, Sukoon campsite, compost, five-year plan).
# Dates are the ones written in those plans; None where the plan sets no date (set one on the page).
PLAN = [
    ("farm-pump-test", "Farm", "24-hour pump test at 6 m³ an hour or better", "Shared water gate for chillies and bananas.", None),
    ("farm-water-retest", "Farm", "Borehole water retest (EC, sodium, chloride) and soil test (EC, pH, ESP)", "Source Two was 3.18 dS/m in Aug 2024.", None),
    ("farm-certifier", "Farm", "Certifier's written answer: conventional chilli block, livestock feeds and manure, camp", "Needed for livestock gate 0 (before Jan 2027).", "2026-12-31"),
    ("farm-owners", "Farm", "One accountable person named per enterprise; monthly sales report agreed", "Decision 5 of the five-year plan.", None),
    ("fc-weigh-day", "Livestock", "Herd count and weigh day", "Check weights, pen space, fodder acres.", "2026-10-04"),
    ("fc-compost-first", "Fodder & compost", "First weekly compost out (heap H1)", "Heap H1 started 1 Oct 2026.", "2026-12-02"),
    ("fc-compost-sample", "Fodder & compost", "Compost sample to Cropnuts (EC, N, P, K) before spreading", None, None),
    ("fc-drinking-water", "Livestock", "Move livestock drinking water to the borehole over 10 days", "Keep spring water for sick animals. Saves about KES 360,000 a year.", None),
    ("ls-gate0", "Livestock", "Gate 0: written goat buyer at KES 380/kg or more; three weaner suppliers at KES 320/kg or less", "Plus certifier answer and water and fodder plan. Before release 1.", "2026-12-31"),
    ("ls-release1", "Livestock", "Release 1: KES 2.0M after gate 0", None, "2027-01-31"),
    ("ls-gate1", "Livestock", "Gate 1: Eid pilot of about 30 goats: 150 g/day or more, FCR 7.0 or less, loss 3% or less, price KES 400/kg or more", "Eid al-Adha predicted 17 May 2027.", "2027-05-17"),
    ("ls-gate2", "Livestock", "Gate 2 (month 9, about 50 a month): margin KES 2,500 a goat and KES 1,500 a sheep", "Date assumes the start in January 2027.", "2027-09-30"),
    ("ls-gate3", "Livestock", "Gate 3 (month 12): two quarters at plan; decide the 150-doe nucleus", "Date assumes the start in January 2027.", "2027-12-31"),
    ("ch-buyer", "Chillies", "Habanero order in writing: volume, KES 125/kg, payment days, pack, collection day", "Gate 2.", None),
    ("ch-cayenne-buyers", "Chillies", "Two or more written buyers for about 120 kg a month of dried cayenne at KES 300/kg or more", "Cayenne only with a written buyer.", None),
    ("ch-salt-trial", "Chillies", "Paired salt trial: 100 habanero + 100 cayenne on borehole water vs 50 each on fresh water (pass at 85%)", "Gate 1.", None),
    ("ch-agronomist", "Chillies", "Agronomist signs the salt and feed plan", "Gate 1.", None),
    ("ch-month2", "Chillies", "Month 2 after planting: 95% of plants alive", "Gate 4.", None),
    ("ch-month12", "Chillies", "Month 12: habanero run-rate 5 t an acre a year, buyer paid on time; scale-up decision", "Gate 5.", None),
    ("ba-fusarium", "Bananas", "Fusarium check: Hassan's variety, location, lab confirmation; clean-plant plan", "Before plantlets are ordered.", None),
    ("ba-buyer", "Bananas", "Danish buyer's price and volume in writing, or two buyers at about 200 kg a week at KES 30/kg", "Gate 2.", None),
    ("ba-trial-row", "Bananas", "Trial row of 50 plants on borehole water: only light leaf-edge burn after eight weeks", "Gate 1.", None),
    ("ba-decision", "Bananas", "Banana go / no-go (start of year 2)", "Bananas from Y2 after the Fusarium and water gates.", "2027-10-01"),
    ("sk-stage0", "Sukoon Camp", "Stage 0 validation approved (USD 8,960)", None, None),
    ("sk-gate0", "Sukoon Camp", "Gate 0: tested drinking water, organic position, fixed-price quotes within about $155k, demand evidence", "Releases site works.", "2026-11-13"),
    ("sk-gate1", "Sukoon Camp", "Gate 1: releases buildings and pitches", None, "2027-02-26"),
    ("sk-gate2", "Sukoon Camp", "Gate 2: releases tents, vehicle, stock and opening", None, "2027-06-04"),
    ("sk-open", "Sukoon Camp", "Sukoon Camp opens", "Can slip to 1 October 2027.", "2027-07-01"),
    ("sk-gate3", "Sukoon Camp", "Gate 3 review (month 9 of trading)", "Scorecards at months 3, 6 and 9.", "2028-03-31"),
    ("ho-scope", "Honey", "Honey scoped (year 1); pilot in year 2", None, "2027-09-30"),
]


@bp.route("")
@auth.login_required
@auth.require_roles(*VIEW)
def index():
    ent = request.args.get("enterprise") or None
    show_done = request.args.get("done") == "1"
    where, args = ["1=1"], []
    if ent:
        where.append("enterprise=?"); args.append(ent)
    if not show_done:
        where.append("status IN ('open','at_risk')")
    rows = db.query(f"SELECT * FROM milestones WHERE {' AND '.join(where)} "
                    "ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date, enterprise", tuple(args))
    items = [(m, farm.rag(m["due_date"], m["status"])) for m in rows]
    allm = db.query("SELECT due_date, status FROM milestones")
    counts = {"red": 0, "amber": 0, "green": 0, "gray": 0, "done": 0}
    for m in allm:
        c, label = farm.rag(m["due_date"], m["status"])
        if m["status"] == "done":
            counts["done"] += 1
        elif m["status"] != "dropped":
            counts[c] += 1
    loaded = db.query("SELECT COUNT(*) c FROM milestones WHERE plan_key IS NOT NULL", one=True)["c"]
    return render_template("milestones.html", items=items, counts=counts, ent=ent, show_done=show_done,
                           enterprises=farm.ENTERPRISES, loaded=loaded, plan_size=len(PLAN), today=db.today_str(),
                           can_edit=g.user["role"] in auth.CROP_EDIT)


@bp.route("/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def add():
    title = request.form.get("title", "").strip()
    if not title:
        flash("Write the milestone.", "error")
        return redirect(url_for("milestones.index"))
    mid = db.execute("INSERT INTO milestones (enterprise, title, detail, due_date, owner, created_by) VALUES (?,?,?,?,?,?)",
                     (request.form.get("enterprise") or "Farm", title, request.form.get("detail") or None,
                      request.form.get("due_date") or None, request.form.get("owner") or None, g.user["id"]))
    db.audit(g.user, "create", "milestone", mid, f"Milestone: {title}")
    flash("Milestone added.", "success")
    return redirect(url_for("milestones.index"))


@bp.route("/<int:mid>", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def update(mid):
    m = db.query("SELECT * FROM milestones WHERE id=?", (mid,), one=True)
    if not m:
        abort(404)
    status = request.form.get("status") or m["status"]
    due = request.form.get("due_date") if "due_date" in request.form else m["due_date"]
    done_date = (request.form.get("done_date") or db.today_str()) if status == "done" else None
    note = request.form.get("note") or None
    detail = m["detail"]
    if note:
        detail = ((detail + "\n") if detail else "") + f"{db.today_str()}: {note}"
    db.execute("UPDATE milestones SET status=?, due_date=?, owner=?, done_date=?, detail=?, updated_at=datetime('now') WHERE id=?",
               (status, due or None, request.form.get("owner") if "owner" in request.form else m["owner"], done_date, detail, mid))
    changes = []
    if status != m["status"]:
        changes.append(f"status {m['status']} -> {status}")
    if (due or None) != m["due_date"]:
        changes.append(f"due {m['due_date']} -> {due}")
    db.audit(g.user, "update", "milestone", mid, f"{m['title'][:60]}: " + (", ".join(changes) or "note added"))
    flash("Milestone updated.", "success")
    return redirect(url_for("milestones.index", enterprise=request.form.get("ent") or None))


@bp.route("/load-plan", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def load_plan():
    n = 0
    for key, ent, title, detail, due in PLAN:
        if not db.query("SELECT 1 FROM milestones WHERE plan_key=?", (key,), one=True):
            db.execute("INSERT INTO milestones (enterprise, title, detail, due_date, plan_key, created_by) VALUES (?,?,?,?,?,?)",
                       (ent, title, detail, due, key, g.user["id"]))
            n += 1
    db.audit(g.user, "import", "milestone", None, f"Loaded {n} milestones from the reset plan")
    flash(f"{n} milestones loaded from the reset plan." + (" Set dates on the ones without a date." if n else ""), "success")
    return redirect(url_for("milestones.index"))
