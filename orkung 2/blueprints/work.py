"""Daily work: one sheet a day for every member of staff (attendance plus the
main job, block and output), extra job entries, the work log, and the staff
register with pay rates.

Attendance is what pay is built from. Once a month's pay run is approved,
that month's attendance and jobs are frozen so pay cannot drift afterwards.
"""
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import auth
import farm

bp = Blueprint("work", __name__, url_prefix="/work")


def _day():
    d = db.parse_date(request.values.get("d"))
    return (d or date.today()).isoformat()


# ---------------------------------------------------------------------------
# Daily sheet
# ---------------------------------------------------------------------------

@bp.route("/day", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def day():
    d = _day()
    locked = farm.month_locked(d)
    if request.method == "POST":
        if g.user["role"] not in auth.CROP_EDIT:
            abort(403)
        if locked:
            flash(f"{d[:7]} pay has been approved, so this day can no longer be changed.", "error")
            return redirect(url_for("work.day", d=d))
        if d > date.today().isoformat():
            flash("You cannot fill a sheet for a future date.", "error")
            return redirect(url_for("work.day"))
        saved = cleared = idle = 0
        for wid in request.form.getlist("wid", type=int):
            status = request.form.get(f"s_{wid}") or ""
            activity = request.form.get(f"a_{wid}") or ""
            if not status:
                if db.query("SELECT 1 FROM attendance WHERE worker_id=? AND work_date=?", (wid, d), one=True):
                    db.execute("DELETE FROM attendance WHERE worker_id=? AND work_date=?", (wid, d))
                    db.execute("DELETE FROM work_logs WHERE worker_id=? AND work_date=? AND source='sheet'", (wid, d))
                    cleared += 1
                continue
            db.execute("INSERT INTO attendance (worker_id, work_date, status, notes, created_by) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(worker_id, work_date) DO UPDATE SET status=excluded.status, notes=excluded.notes, "
                       "created_by=excluded.created_by, updated_at=datetime('now')",
                       (wid, d, status, request.form.get(f"n_{wid}") or None, g.user["id"]))
            db.execute("DELETE FROM work_logs WHERE worker_id=? AND work_date=? AND source='sheet'", (wid, d))
            if activity and status in ("present", "half"):
                db.execute("INSERT INTO work_logs (work_date, worker_id, activity, block_id, hours, quantity, unit, notes, source, created_by) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (d, wid, activity, request.form.get(f"b_{wid}", type=int) or None,
                            request.form.get(f"h_{wid}", type=float), request.form.get(f"q_{wid}", type=float),
                            request.form.get(f"u_{wid}") or None, request.form.get(f"n_{wid}") or None, "sheet", g.user["id"]))
            elif status in ("present", "half"):
                extra = db.query("SELECT 1 FROM work_logs WHERE worker_id=? AND work_date=?", (wid, d), one=True)
                if not extra:
                    idle += 1
            saved += 1
        db.audit(g.user, "update", "daily_sheet", None, f"Daily sheet {d}: {saved} marked, {cleared} cleared")
        msg = f"Sheet for {d} saved: {saved} people marked."
        if idle:
            msg += f" {idle} marked at work with no job recorded; add their job so the day is paid against real work."
        flash(msg, "success" if not idle else "info")
        return redirect(url_for("work.day", d=d))

    staff = list(farm.workers())
    ids = {w["id"] for w in staff}
    for w in db.query("SELECT w.* FROM workers w JOIN attendance a ON a.worker_id=w.id WHERE a.work_date=? AND w.active=0", (d,)):
        if w["id"] not in ids:
            staff.append(w)
    att = {r["worker_id"]: r for r in db.query("SELECT * FROM attendance WHERE work_date=?", (d,))}
    sheet = {r["worker_id"]: r for r in db.query("SELECT * FROM work_logs WHERE work_date=? AND source='sheet'", (d,))}
    extra = db.query("SELECT l.*, w.name, b.code AS block_code FROM work_logs l JOIN workers w ON w.id=l.worker_id "
                     "LEFT JOIN crop_blocks b ON b.id=l.block_id WHERE l.work_date=? AND l.source!='sheet' ORDER BY w.name", (d,))
    dd = date.fromisoformat(d)
    return render_template("work_day.html", d=d, day_name=dd.strftime("%A %d %B %Y"),
                           prev=(dd - timedelta(days=1)).isoformat(), next=(dd + timedelta(days=1)).isoformat(),
                           is_today=d == date.today().isoformat(), future=d > date.today().isoformat(),
                           staff=staff, att=att, sheet=sheet, extra=extra, locked=locked,
                           blocks=farm.blocks(), activities=farm.ACTIVITIES, units=farm.OUTPUT_UNITS,
                           statuses=farm.ATTENDANCE, can_edit=g.user["role"] in auth.CROP_EDIT)


@bp.route("/log/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def add_entry():
    d = _day()
    wid = request.form.get("worker_id", type=int)
    activity = request.form.get("activity")
    if not wid or not activity:
        flash("Choose the person and the job.", "error")
        return redirect(request.referrer or url_for("work.day", d=d))
    if farm.month_locked(d):
        flash("That month's pay has been approved; the day is frozen.", "error")
        return redirect(url_for("work.day", d=d))
    lid = db.execute("INSERT INTO work_logs (work_date, worker_id, activity, block_id, hours, quantity, unit, notes, source, created_by) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (d, wid, activity, request.form.get("block_id", type=int) or None, request.form.get("hours", type=float),
                      request.form.get("quantity", type=float), request.form.get("unit") or None,
                      request.form.get("notes") or None, "entry", g.user["id"]))
    db.audit(g.user, "create", "work_log", lid, f"Job {activity} for worker {wid} on {d}")
    flash("Job added.", "success")
    return redirect(url_for("work.day", d=d))


@bp.route("/log/<int:lid>/delete", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def delete_entry(lid):
    l = db.query("SELECT * FROM work_logs WHERE id=?", (lid,), one=True)
    if not l:
        abort(404)
    if farm.month_locked(l["work_date"]):
        flash("That month's pay has been approved; the day is frozen.", "error")
    else:
        db.execute("DELETE FROM work_logs WHERE id=?", (lid,))
        db.audit(g.user, "delete", "work_log", lid, f"Deleted job {l['activity']} {l['work_date']}", details=dict(l))
        flash("Job removed.", "success")
    return redirect(url_for("work.day", d=l["work_date"]))


@bp.route("/log")
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def log():
    m = request.args.get("month") or farm.this_month()
    start, end = farm.month_bounds(m)
    period = start[:7]
    wid = request.args.get("worker_id", type=int)
    bid = request.args.get("block_id", type=int)
    act = request.args.get("activity") or None
    rows = farm.labour_rows(start, end)
    if wid:
        rows = [r for r in rows if r["worker_id"] == wid]
    if bid:
        rows = [r for r in rows if r["block_id"] == bid]
    if act:
        rows = [r for r in rows if r["activity"] == act]
    by_act = {}
    for r in rows:
        v = by_act.setdefault(r["activity"], [0, 0.0])
        v[0] += 1
        v[1] += r["cost"]
    if request.args.get("export"):
        from blueprints.reports import _out
        return _out(f"Work log {farm.month_label(period)}",
                    ["Date", "Person", "Attendance", "Job", "Block", "Crop", "Hours", "Output", "Cost (KES)"],
                    [(r["work_date"], r["worker"], farm.ATT_LABEL.get(r["status"], r["status"]), r["activity"], r["block_code"] or "",
                      r["crop"] or "", r["hours"] or "", f"{r['quantity']:g} {r['unit'] or ''}".strip() if r["quantity"] else "",
                      round(r["cost"])) for r in rows], f"work_log_{period}.csv",
                    description="Each paid day split across the jobs logged that day. 'No job recorded' = paid time with no work logged.")
    return render_template("work_log.html", rows=rows, by_act=sorted(by_act.items(), key=lambda kv: -kv[1][1]),
                           total=sum(r["cost"] for r in rows), period=period, label=farm.month_label(period),
                           prev=farm.shift_month(period, -1), next=farm.shift_month(period, 1),
                           workers=farm.workers(active_only=False), blocks=farm.blocks(active_only=False),
                           activities=farm.ACTIVITIES + ["No job recorded"], wid=wid, bid=bid, act=act, fmt=farm.fmt)


# ---------------------------------------------------------------------------
# Staff register
# ---------------------------------------------------------------------------

def month_attendance(start, end):
    rows = db.query("SELECT worker_id, status, COUNT(*) c FROM attendance WHERE work_date BETWEEN ? AND ? "
                    "GROUP BY worker_id, status", (start, end))
    out = {}
    for r in rows:
        out.setdefault(r["worker_id"], {})[r["status"]] = r["c"]
    return out


def outstanding_advances(worker_id, as_of=None, exclude_run=None):
    as_of = as_of or "9999-12-31"
    given = db.query("SELECT COALESCE(SUM(amount),0) s FROM pay_advances WHERE worker_id=? AND advance_date<=?",
                     (worker_id, as_of), one=True)["s"]
    taken = db.query("SELECT COALESCE(SUM(l.advances),0) s FROM pay_lines l JOIN pay_runs r ON r.id=l.run_id "
                     "WHERE l.worker_id=? AND r.status IN ('approved','paid') AND r.id != ?",
                     (worker_id, exclude_run or 0), one=True)["s"]
    return round(given - taken, 2)


@bp.route("/staff", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def workers():
    if request.method == "POST":
        if g.user["role"] not in auth.PAY_EDIT:
            abort(403)
        name = request.form.get("name", "").strip()
        pay_type = request.form.get("pay_type")
        salary = request.form.get("monthly_salary", type=float)
        rate = request.form.get("daily_rate", type=float)
        if not name or pay_type not in ("monthly", "daily") or (pay_type == "monthly" and not salary) or (pay_type == "daily" and not rate):
            flash("Enter the name, how they are paid, and the monthly salary or the daily rate.", "error")
            return redirect(url_for("work.workers"))
        wid = db.execute("INSERT INTO workers (name, phone, job_title, pay_type, monthly_salary, daily_rate, start_date, notes) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (name, request.form.get("phone") or None, request.form.get("job_title") or None, pay_type,
                          salary if pay_type == "monthly" else None, rate if pay_type == "daily" else None,
                          request.form.get("start_date") or db.today_str(), request.form.get("notes") or None))
        db.audit(g.user, "create", "worker", wid, f"Added staff {name} ({pay_type}, {salary or rate})")
        flash(f"{name} added to the staff register.", "success")
        return redirect(url_for("work.workers"))
    period = farm.this_month()
    start, end = farm.month_bounds(period)
    show_all = request.args.get("all") == "1"
    staff = farm.workers(active_only=not show_all)
    att = month_attendance(start, end)
    adv = {w["id"]: outstanding_advances(w["id"]) for w in staff}
    return render_template("work_staff.html", staff=staff, att=att, adv=adv, label=farm.month_label(period),
                           show_all=show_all, can_edit=g.user["role"] in auth.PAY_EDIT, today=db.today_str(),
                           divisor=farm.pay_divisor())


@bp.route("/staff/<int:wid>", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def worker(wid):
    w = db.query("SELECT * FROM workers WHERE id=?", (wid,), one=True)
    if not w:
        abort(404)
    if request.method == "POST":
        if g.user["role"] not in auth.PAY_EDIT:
            abort(403)
        pay_type = request.form.get("pay_type") or w["pay_type"]
        fields = dict(name=request.form.get("name") or w["name"], phone=request.form.get("phone") or None,
                      job_title=request.form.get("job_title") or None, pay_type=pay_type,
                      monthly_salary=request.form.get("monthly_salary", type=float) if pay_type == "monthly" else None,
                      daily_rate=request.form.get("daily_rate", type=float) if pay_type == "daily" else None,
                      start_date=request.form.get("start_date") or None, end_date=request.form.get("end_date") or None,
                      notes=request.form.get("notes") or None, active=0 if request.form.get("end_date") else 1)
        changes = {k: (w[k], v) for k, v in fields.items() if w[k] != v}
        db.execute("UPDATE workers SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), wid))
        db.audit(g.user, "update", "worker", wid, f"Updated staff {w['name']}: " + ", ".join(changes) if changes else "no change",
                 details=changes)
        flash("Saved." + (" Pay rate changed: this is in the audit history." if {"monthly_salary", "daily_rate", "pay_type"} & set(changes) else ""),
              "success")
        return redirect(url_for("work.worker", wid=wid))
    m = request.args.get("month") or farm.this_month()
    start, end = farm.month_bounds(m)
    period = start[:7]
    days = {r["work_date"]: r for r in db.query("SELECT * FROM attendance WHERE worker_id=? AND work_date BETWEEN ? AND ?",
                                                 (wid, start, end))}
    jobs = {}
    for l in db.query("SELECT l.*, b.code FROM work_logs l LEFT JOIN crop_blocks b ON b.id=l.block_id "
                      "WHERE l.worker_id=? AND l.work_date BETWEEN ? AND ? ORDER BY l.id", (wid, start, end)):
        jobs.setdefault(l["work_date"], []).append(l)
    cal = []
    d = date.fromisoformat(start)
    while d.isoformat() <= end:
        cal.append((d, days.get(d.isoformat()), jobs.get(d.isoformat(), [])))
        d += timedelta(days=1)
    advances = db.query("SELECT * FROM pay_advances WHERE worker_id=? ORDER BY advance_date DESC LIMIT 50", (wid,))
    lines = db.query("SELECT l.*, r.period, r.status AS run_status FROM pay_lines l JOIN pay_runs r ON r.id=l.run_id "
                     "WHERE l.worker_id=? ORDER BY r.period DESC LIMIT 24", (wid,))
    assets = db.query("SELECT * FROM assets WHERE custodian_id=? AND status NOT IN ('disposed') ORDER BY name", (wid,))
    return render_template("work_worker.html", w=w, cal=cal, period=period, label=farm.month_label(period),
                           prev=farm.shift_month(period, -1), next=farm.shift_month(period, 1),
                           advances=advances, owed=outstanding_advances(wid), lines=lines, assets=assets,
                           att_label=farm.ATT_LABEL, can_edit=g.user["role"] in auth.PAY_EDIT)
