"""Payroll: gross pay built from the daily attendance, advances recovered from
pay, and a draft -> approved -> paid run each month.

Rules (gross only; PAYE, SHIF, NSSF and Housing Levy are left to the accountant):

* Monthly staff: salary for the month (pro-rated by calendar days if they
  started or left in the month), less salary / divisor for each day marked
  "Absent (unpaid)". Sick, leave and days off are paid.
* Daily staff: daily rate x (present days + half x half days). Nothing for
  sick, leave, absent or off.
* Advances: everything owed is taken from this month's pay by default, up to
  the pay itself. The manager can lower it to spread the recovery; the rest
  carries forward.
* A manager prepares the draft; only an administrator approves it. Approval
  freezes the month's attendance and jobs. Payment date and M-Pesa reference
  are recorded per person.
"""
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import auth
import farm
from blueprints.work import outstanding_advances

bp = Blueprint("payroll", __name__, url_prefix="/payroll")


def _run(period):
    run = db.query("SELECT * FROM pay_runs WHERE period=?", (period,), one=True)
    if not run:
        abort(404)
    return run


def _staff_for(start, end):
    return db.query("SELECT DISTINCT w.* FROM workers w LEFT JOIN attendance a ON a.worker_id=w.id AND a.work_date BETWEEN ? AND ? "
                    "WHERE (COALESCE(w.start_date,'0000') <= ? AND (w.end_date IS NULL OR w.end_date >= ?)) OR a.id IS NOT NULL "
                    "ORDER BY w.name", (start, end, end, start))


def compute_line(w, start, end):
    att = {r["status"]: r["c"] for r in db.query(
        "SELECT status, COUNT(*) c FROM attendance WHERE worker_id=? AND work_date BETWEEN ? AND ? GROUP BY status",
        (w["id"], start, end))}
    present = att.get("present", 0) + 0.5 * att.get("half", 0)
    paid_leave = att.get("sick", 0) + att.get("leave", 0)
    absent = att.get("absent", 0) + 0.5 * att.get("half", 0) * (1 if w["pay_type"] == "monthly" else 0)
    if w["pay_type"] == "monthly":
        rate = w["monthly_salary"] or 0
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
        s = max(d0, db.parse_date(w["start_date"]) or d0)
        e = min(d1, db.parse_date(w["end_date"]) or d1)
        employed = max((e - s).days + 1, 0)
        basic = rate * employed / ((d1 - d0).days + 1)
        absence = round(absent * rate / farm.pay_divisor(), 2)
    else:
        rate = w["daily_rate"] or 0
        basic = rate * present
        absence = 0.0
        absent = att.get("absent", 0)
    return dict(pay_type=w["pay_type"], rate=rate, days_present=present, days_paid_leave=paid_leave,
                days_absent=absent, basic=round(basic, 2), absence_deduction=absence, marked=sum(att.values()))


def _finish(line):
    gross = round(line["basic"] - line["absence_deduction"] + (line["additions"] or 0), 2)
    net = round(gross - (line["advances"] or 0) - (line["other_deductions"] or 0), 2)
    return gross, net


def build_lines(run, keep=True):
    start, end = farm.month_bounds(run["period"])
    existing = {l["worker_id"]: l for l in db.query("SELECT * FROM pay_lines WHERE run_id=?", (run["id"],))}
    seen = set()
    for w in _staff_for(start, end):
        c = compute_line(w, start, end)
        old = existing.get(w["id"])
        additions = old["additions"] if old and keep else 0
        other = old["other_deductions"] if old and keep else 0
        notes = old["notes"] if old and keep else None
        if old and keep:
            adv = old["advances"]
        else:
            owed = outstanding_advances(w["id"], end, exclude_run=run["id"])
            adv = max(min(owed, c["basic"] - c["absence_deduction"] + additions - other), 0)
        line = dict(c, additions=additions, other_deductions=other, advances=round(adv, 2))
        gross, net = _finish(line)
        if old:
            db.execute("UPDATE pay_lines SET pay_type=?, rate=?, days_present=?, days_paid_leave=?, days_absent=?, basic=?, "
                       "absence_deduction=?, additions=?, advances=?, other_deductions=?, gross=?, net=?, notes=? WHERE id=?",
                       (c["pay_type"], c["rate"], c["days_present"], c["days_paid_leave"], c["days_absent"], c["basic"],
                        c["absence_deduction"], additions, line["advances"], other, gross, net, notes, old["id"]))
        else:
            db.execute("INSERT INTO pay_lines (run_id, worker_id, pay_type, rate, days_present, days_paid_leave, days_absent, "
                       "basic, absence_deduction, additions, advances, other_deductions, gross, net, notes) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run["id"], w["id"], c["pay_type"], c["rate"], c["days_present"], c["days_paid_leave"],
                        c["days_absent"], c["basic"], c["absence_deduction"], additions, line["advances"], other,
                        gross, net, notes))
        seen.add(w["id"])
    for wid, old in existing.items():
        if wid not in seen:
            db.execute("DELETE FROM pay_lines WHERE id=?", (old["id"],))


@bp.route("")
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def index():
    runs = db.query("SELECT r.*, COUNT(l.id) n, COALESCE(SUM(l.gross),0) gross, COALESCE(SUM(l.net),0) net, "
                    "COALESCE(SUM(l.advances),0) adv, SUM(CASE WHEN l.paid_date IS NOT NULL THEN 1 ELSE 0 END) paid_n "
                    "FROM pay_runs r LEFT JOIN pay_lines l ON l.run_id=r.id GROUP BY r.id ORDER BY r.period DESC")
    staff = farm.workers(active_only=False)
    owed = [(w, outstanding_advances(w["id"])) for w in staff]
    owed = [(w, o) for w, o in owed if abs(o) > 0.5]
    advances = db.query("SELECT a.*, w.name FROM pay_advances a JOIN workers w ON w.id=a.worker_id "
                        "ORDER BY a.advance_date DESC, a.id DESC LIMIT 40")
    have = {r["period"] for r in runs}
    suggest = farm.this_month()
    if suggest in have:
        suggest = farm.shift_month(suggest, 1)
    return render_template("payroll_index.html", runs=runs, owed=owed, advances=advances, staff=farm.workers(),
                           suggest=suggest, last_month=farm.shift_month(farm.this_month(), -1), have=have,
                           divisor=farm.pay_divisor(), today=db.today_str(),
                           can_edit=g.user["role"] in auth.PAY_EDIT, can_approve=g.user["role"] in auth.PAY_APPROVE)


@bp.route("/advance", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_EDIT)
def add_advance():
    wid = request.form.get("worker_id", type=int)
    amt = request.form.get("amount", type=float)
    when = request.form.get("advance_date") or db.today_str()
    if not wid or not amt or amt <= 0:
        flash("Choose the person and the amount.", "error")
        return redirect(url_for("payroll.index"))
    aid = db.execute("INSERT INTO pay_advances (worker_id, advance_date, amount, reason, paid_by, created_by) VALUES (?,?,?,?,?,?)",
                     (wid, when, amt, request.form.get("reason") or None, request.form.get("paid_by") or None, g.user["id"]))
    db.audit(g.user, "create", "pay_advance", aid, f"Advance KES {amt:,.0f} to worker {wid} on {when}")
    flash(f"Advance of KES {amt:,.0f} recorded. It will come off the next pay run.", "success")
    return redirect(url_for("payroll.index"))


@bp.route("/advance/<int:aid>/delete", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_APPROVE)
def delete_advance(aid):
    a = db.query("SELECT * FROM pay_advances WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    db.execute("DELETE FROM pay_advances WHERE id=?", (aid,))
    db.audit(g.user, "delete", "pay_advance", aid, f"Deleted advance KES {a['amount']:,.0f} of {a['advance_date']}", details=dict(a))
    flash("Advance deleted (kept in the audit history).", "success")
    return redirect(url_for("payroll.index"))


@bp.route("/settings", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_APPROVE)
def settings():
    v = request.form.get("divisor", type=float)
    if not v or v < 20 or v > 31:
        flash("Working days a month must be between 20 and 31.", "error")
    else:
        db.execute("INSERT INTO settings (key, value) VALUES ('payroll_divisor', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(v),))
        db.audit(g.user, "update", "setting", None, f"Payroll working days a month set to {v:g}")
        flash(f"Working days a month set to {v:g}. Recalculate any draft pay run to apply it.", "success")
    return redirect(url_for("payroll.index"))


@bp.route("/run", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_EDIT)
def create_run():
    start, _ = farm.month_bounds(request.form.get("period"))
    period = start[:7]
    if db.query("SELECT 1 FROM pay_runs WHERE period=?", (period,), one=True):
        return redirect(url_for("payroll.run", period=period))
    rid = db.execute("INSERT INTO pay_runs (period, created_by) VALUES (?,?)", (period, g.user["id"]))
    build_lines(_run(period), keep=False)
    db.audit(g.user, "create", "pay_run", rid, f"Draft pay run {period}")
    flash(f"Draft pay run for {farm.month_label(period)} prepared from the daily sheets. Check it, then ask the administrator to approve.", "success")
    return redirect(url_for("payroll.run", period=period))


@bp.route("/run/<period>")
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def run(period):
    r = _run(period)
    start, end = farm.month_bounds(period)
    lines = db.query("SELECT l.*, w.name, w.phone, w.job_title FROM pay_lines l JOIN workers w ON w.id=l.worker_id "
                     "WHERE l.run_id=? ORDER BY w.name", (r["id"],))
    last = min(date.fromisoformat(end), date.today())
    days_in = max((last - date.fromisoformat(start)).days + 1, 0)  # days so far: future days are not "unmarked"
    marked = {}
    for x in db.query("SELECT worker_id, COUNT(*) c FROM attendance WHERE work_date BETWEEN ? AND ? GROUP BY worker_id", (start, end)):
        marked[x["worker_id"]] = x["c"]
    tot = {k: sum(l[k] for l in lines) for k in ("basic", "absence_deduction", "additions", "advances", "other_deductions", "gross", "net")}
    if request.args.get("export"):
        from blueprints.reports import _out
        return _out(f"Payroll {farm.month_label(period)}",
                    ["Name", "Phone", "Pay type", "Rate", "Days worked", "Paid sick/leave", "Unpaid absent", "Basic",
                     "Absence deduction", "Additions", "Gross", "Advances", "Other deductions", "Net to pay", "Paid on", "Payment ref", "Notes"],
                    [(l["name"], l["phone"] or "", l["pay_type"], l["rate"], l["days_present"], l["days_paid_leave"], l["days_absent"],
                      round(l["basic"]), round(l["absence_deduction"]), round(l["additions"]), round(l["gross"]), round(l["advances"]),
                      round(l["other_deductions"]), round(l["net"]), l["paid_date"] or "", l["payment_ref"] or "", l["notes"] or "")
                     for l in lines], f"payroll_{period}.csv",
                    description=f"Status: {r['status']}. Gross pay only; statutory deductions are done by the accountant.")
    approver = db.query("SELECT full_name FROM users WHERE id=?", (r["approved_by"],), one=True) if r["approved_by"] else None
    return render_template("payroll_run.html", r=r, lines=lines, tot=tot, label=farm.month_label(period), days_in=days_in,
                           marked=marked, approver=approver, today=db.today_str(), divisor=farm.pay_divisor(),
                           can_edit=g.user["role"] in auth.PAY_EDIT and r["status"] == "draft",
                           can_approve=g.user["role"] in auth.PAY_APPROVE,
                           can_pay=g.user["role"] in auth.PAY_EDIT and r["status"] in ("approved", "paid"))


@bp.route("/run/<period>/recalc", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_EDIT)
def recalc(period):
    r = _run(period)
    if r["status"] != "draft":
        flash("Only a draft can be recalculated.", "error")
    else:
        build_lines(r, keep=request.form.get("reset") != "1")
        db.audit(g.user, "update", "pay_run", r["id"], f"Recalculated pay run {period}")
        flash("Recalculated from the latest daily sheets.", "success")
    return redirect(url_for("payroll.run", period=period))


@bp.route("/run/<period>/line/<int:lid>", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_EDIT)
def edit_line(period, lid):
    r = _run(period)
    l = db.query("SELECT * FROM pay_lines WHERE id=? AND run_id=?", (lid, r["id"]), one=True)
    if not l:
        abort(404)
    if r["status"] != "draft":
        flash("This pay run is approved and locked.", "error")
        return redirect(url_for("payroll.run", period=period))
    _, end = farm.month_bounds(period)
    owed = outstanding_advances(l["worker_id"], end, exclude_run=r["id"])
    adv = request.form.get("advances", type=float) or 0
    if adv > owed + 0.5:
        flash(f"Only KES {owed:,.0f} is owed in advances; the deduction was set to that.", "info")
        adv = max(owed, 0)
    line = dict(l)
    line.update(additions=request.form.get("additions", type=float) or 0, advances=adv,
                other_deductions=request.form.get("other_deductions", type=float) or 0)
    gross, net = _finish(line)
    if net < 0:
        flash("Net pay cannot be below zero; lower the advance or the other deductions.", "error")
        return redirect(url_for("payroll.run", period=period))
    db.execute("UPDATE pay_lines SET additions=?, advances=?, other_deductions=?, gross=?, net=?, notes=? WHERE id=?",
               (line["additions"], adv, line["other_deductions"], gross, net, request.form.get("notes") or None, lid))
    db.audit(g.user, "update", "pay_line", lid, f"Pay {period} worker {l['worker_id']}: additions {line['additions']:g}, "
             f"advances {adv:g}, other {line['other_deductions']:g}, net {net:g}")
    flash("Line saved.", "success")
    return redirect(url_for("payroll.run", period=period))


@bp.route("/run/<period>/approve", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_APPROVE)
def approve(period):
    r = _run(period)
    if r["status"] != "draft":
        flash("Already approved.", "info")
        return redirect(url_for("payroll.run", period=period))
    build_lines(r, keep=True)  # make sure it matches the sheets at the moment of approval
    db.execute("UPDATE pay_runs SET status='approved', approved_by=?, approved_at=datetime('now') WHERE id=?", (g.user["id"], r["id"]))
    tot = db.query("SELECT COALESCE(SUM(net),0) n FROM pay_lines WHERE run_id=?", (r["id"],), one=True)["n"]
    db.audit(g.user, "status_change", "pay_run", r["id"], f"Approved pay run {period}: net KES {tot:,.0f}")
    flash(f"Approved. Net to pay KES {tot:,.0f}. {period} attendance is now frozen.", "success")
    return redirect(url_for("payroll.run", period=period))


@bp.route("/run/<period>/reopen", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_APPROVE)
def reopen(period):
    r = _run(period)
    if db.query("SELECT 1 FROM pay_lines WHERE run_id=? AND paid_date IS NOT NULL", (r["id"],), one=True):
        flash("Some people have already been paid, so this run cannot be reopened. Put any correction in next month's run.", "error")
    else:
        db.execute("UPDATE pay_runs SET status='draft', approved_by=NULL, approved_at=NULL WHERE id=?", (r["id"],))
        db.audit(g.user, "status_change", "pay_run", r["id"], f"Reopened pay run {period} for correction")
        flash("Reopened as a draft.", "success")
    return redirect(url_for("payroll.run", period=period))


@bp.route("/run/<period>/paid", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.PAY_EDIT)
def mark_paid(period):
    r = _run(period)
    if r["status"] == "draft":
        flash("Approve the run before recording payments.", "error")
        return redirect(url_for("payroll.run", period=period))
    when = request.form.get("paid_date") or db.today_str()
    ids = request.form.getlist("line_id", type=int)
    n = 0
    for lid in ids:
        ref = request.form.get(f"ref_{lid}") or request.form.get("ref") or None
        n += 1
        db.execute("UPDATE pay_lines SET paid_date=?, payment_ref=? WHERE id=? AND run_id=? AND paid_date IS NULL",
                   (when, ref, lid, r["id"]))
    left = db.query("SELECT COUNT(*) c FROM pay_lines WHERE run_id=? AND paid_date IS NULL", (r["id"],), one=True)["c"]
    if not left:
        db.execute("UPDATE pay_runs SET status='paid', paid_at=datetime('now') WHERE id=?", (r["id"],))
    db.audit(g.user, "update", "pay_run", r["id"], f"Payments recorded for {n} people, {period}, on {when}")
    flash(f"Payment recorded for {n} people." + ("" if left else " Everyone in this run is now paid."), "success")
    return redirect(url_for("payroll.run", period=period))


@bp.route("/run/<period>/slip/<int:wid>")
@auth.login_required
@auth.require_roles(*auth.PAY_VIEW)
def slip(period, wid):
    r = _run(period)
    l = db.query("SELECT l.*, w.name, w.phone, w.job_title FROM pay_lines l JOIN workers w ON w.id=l.worker_id "
                 "WHERE l.run_id=? AND l.worker_id=?", (r["id"], wid), one=True)
    if not l:
        abort(404)
    _, end = farm.month_bounds(period)
    owed_after = outstanding_advances(wid, end, exclude_run=r["id"]) - l["advances"]
    return render_template("payroll_slip.html", r=r, l=l, label=farm.month_label(period), owed_after=owed_after,
                           divisor=farm.pay_divisor())
