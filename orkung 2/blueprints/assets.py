"""Asset register: pumps, solar, tanks, tools, sprayers, vehicles and camp
equipment. Each asset has a number, a condition, a place, and a named person
who answers for it. Tools are checked out to a person and returned; every
asset should be seen and ticked as verified at least every 90 days.
"""
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import auth
import farm

bp = Blueprint("assets", __name__, url_prefix="/assets")

VERIFY_DAYS = 90
EVENT_LABELS = {"check_out": "Checked out", "return": "Returned", "verified": "Seen and verified", "repair": "Repaired",
                "service": "Serviced", "moved": "Moved", "condition": "Condition changed", "lost": "Reported lost",
                "found": "Found", "disposed": "Disposed / written off", "added": "Added to register"}
VIEW = tuple(r for r in auth.ROLES if r not in ("vet", "worker"))


def next_asset_no():
    row = db.query("SELECT asset_no FROM assets ORDER BY id DESC LIMIT 1", one=True)
    n = int(row["asset_no"].split("-")[-1]) + 1 if row and row["asset_no"].split("-")[-1].isdigit() else 1
    while db.query("SELECT 1 FROM assets WHERE asset_no=?", (f"AST-{n:04d}",), one=True):
        n += 1
    return f"AST-{n:04d}"


@bp.route("")
@auth.login_required
@auth.require_roles(*VIEW)
def index():
    status = request.args.get("status") or None
    category = request.args.get("category") or None
    where, args = ["1=1"], []
    if status:
        where.append("a.status=?"); args.append(status)
    else:
        where.append("a.status != 'disposed'")
    if category:
        where.append("a.category=?"); args.append(category)
    rows = db.query("SELECT a.*, w.name AS custodian FROM assets a LEFT JOIN workers w ON w.id=a.custodian_id "
                    f"WHERE {' AND '.join(where)} ORDER BY a.category, a.name", tuple(args))
    live = db.query("SELECT a.*, w.name AS custodian FROM assets a LEFT JOIN workers w ON w.id=a.custodian_id WHERE a.status != 'disposed'")
    stale_before = (date.today() - timedelta(days=VERIFY_DAYS)).isoformat()
    out = []
    for a in live:
        if a["status"] == "out":
            ev = db.query("SELECT event_date FROM asset_events WHERE asset_id=? AND event_type='check_out' ORDER BY event_date DESC, id DESC LIMIT 1",
                          (a["id"],), one=True)
            out.append((a, ev["event_date"] if ev else None))
    kpi = dict(count=sum(a["quantity"] or 1 for a in live), value=sum((a["cost"] or 0) for a in live),
               repair=sum(1 for a in live if a["condition"] in ("needs_repair", "broken")),
               lost=sum(1 for a in live if a["status"] == "lost"),
               stale=sum(1 for a in live if not a["last_verified"] or a["last_verified"] < stale_before))
    if request.args.get("export"):
        from blueprints.reports import _out
        return _out("Asset register", ["Asset no.", "Name", "Category", "Qty", "Serial", "Location", "Condition", "Status",
                                       "Answerable person", "Bought", "Cost (KES)", "Last verified"],
                    [(a["asset_no"], a["name"], a["category"], a["quantity"], a["serial_no"] or "", a["location"] or "",
                      a["condition"].replace("_", " "), a["status"].replace("_", " "), a["custodian"] or "",
                      a["purchase_date"] or "", a["cost"] or "", a["last_verified"] or "never") for a in rows],
                    "asset_register.csv", description="Every asset not disposed of, with who answers for it.")
    return render_template("assets_index.html", rows=rows, out=out, kpi=kpi, status=status, category=category,
                           categories=farm.ASSET_CATEGORIES, statuses=farm.ASSET_STATUS, conditions=farm.ASSET_CONDITIONS,
                           workers=farm.workers(), stale_before=stale_before, today=db.today_str(),
                           can_edit=g.user["role"] in auth.ASSET_EDIT, verify_days=VERIFY_DAYS)


@bp.route("/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.ASSET_EDIT)
def add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Give the asset a name.", "error")
        return redirect(url_for("assets.index"))
    no = next_asset_no()
    aid = db.execute("INSERT INTO assets (asset_no, name, category, serial_no, location, quantity, purchase_date, cost, condition, "
                     "status, custodian_id, last_verified, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (no, name, request.form.get("category") or "Other", request.form.get("serial_no") or None,
                      request.form.get("location") or None, request.form.get("quantity", type=int) or 1,
                      request.form.get("purchase_date") or None, request.form.get("cost", type=float),
                      request.form.get("condition") or "good", request.form.get("status") or "in_use",
                      request.form.get("custodian_id", type=int) or None, db.today_str(), request.form.get("notes") or None))
    db.execute("INSERT INTO asset_events (asset_id, event_date, event_type, worker_id, notes, created_by) VALUES (?,?,?,?,?,?)",
               (aid, db.today_str(), "added", request.form.get("custodian_id", type=int) or None, None, g.user["id"]))
    db.audit(g.user, "create", "asset", aid, f"Added asset {no} {name}")
    flash(f"Added as {no}. Write this number on the item (paint or tag).", "success")
    return redirect(url_for("assets.index"))


@bp.route("/<int:aid>")
@auth.login_required
@auth.require_roles(*VIEW)
def detail(aid):
    a = db.query("SELECT a.*, w.name AS custodian FROM assets a LEFT JOIN workers w ON w.id=a.custodian_id WHERE a.id=?", (aid,), one=True)
    if not a:
        abort(404)
    events = db.query("SELECT e.*, w.name AS worker, u.full_name FROM asset_events e LEFT JOIN workers w ON w.id=e.worker_id "
                      "LEFT JOIN users u ON u.id=e.created_by WHERE e.asset_id=? ORDER BY e.event_date DESC, e.id DESC", (aid,))
    spend = sum(e["cost"] or 0 for e in events)
    return render_template("asset_detail.html", a=a, events=events, spend=spend, labels=EVENT_LABELS,
                           workers=farm.workers(), conditions=farm.ASSET_CONDITIONS, statuses=dict(farm.ASSET_STATUS),
                           categories=farm.ASSET_CATEGORIES, today=db.today_str(),
                           can_edit=g.user["role"] in auth.ASSET_EDIT, can_dispose=g.user["role"] in auth.MANAGEMENT)


@bp.route("/<int:aid>/event", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.ASSET_EDIT)
def event(aid):
    a = db.query("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    kind = request.form.get("event_type")
    when = request.form.get("event_date") or db.today_str()
    wid = request.form.get("worker_id", type=int) or None
    cost = request.form.get("cost", type=float)
    notes = request.form.get("notes") or None
    cond = request.form.get("condition") or None
    updates = {}
    if kind not in EVENT_LABELS or kind == "added":
        abort(400)
    if kind == "check_out":
        if not wid:
            flash("Say who is taking it.", "error")
            return redirect(url_for("assets.detail", aid=aid))
        updates = dict(status="out", custodian_id=wid)
    elif kind == "return":
        updates = dict(status="in_store")
        if cond:
            updates["condition"] = cond
    elif kind == "verified":
        updates = dict(last_verified=when)
        if a["status"] == "lost":
            updates["status"] = "in_use"
        if cond:
            updates["condition"] = cond
    elif kind in ("repair", "service"):
        updates = dict(condition=cond or ("good" if kind == "repair" else a["condition"]))
    elif kind == "moved":
        if not request.form.get("location"):
            flash("Enter the new location.", "error")
            return redirect(url_for("assets.detail", aid=aid))
        updates = dict(location=request.form.get("location"))
        notes = f"to {request.form.get('location')}" + (f" | {notes}" if notes else "")
        if wid:
            updates["custodian_id"] = wid
    elif kind == "condition":
        if not cond:
            flash("Choose the condition.", "error")
            return redirect(url_for("assets.detail", aid=aid))
        updates = dict(condition=cond)
    elif kind == "lost":
        updates = dict(status="lost")
    elif kind == "found":
        updates = dict(status="in_store", last_verified=when)
    elif kind == "disposed":
        if g.user["role"] not in auth.MANAGEMENT:
            abort(403)
        updates = dict(status="disposed")
    if kind in ("lost", "disposed") and not notes:
        flash("Write what happened in the notes.", "error")
        return redirect(url_for("assets.detail", aid=aid))
    db.execute("INSERT INTO asset_events (asset_id, event_date, event_type, worker_id, cost, notes, created_by) VALUES (?,?,?,?,?,?,?)",
               (aid, when, kind, wid, cost, notes, g.user["id"]))
    if updates:
        db.execute("UPDATE assets SET " + ", ".join(f"{k}=?" for k in updates) + " WHERE id=?", (*updates.values(), aid))
    db.audit(g.user, "update", "asset", aid, f"{a['asset_no']} {a['name']}: {EVENT_LABELS[kind]}" + (f" ({notes})" if notes else ""))
    flash(f"{EVENT_LABELS[kind]} recorded.", "success")
    return redirect(request.form.get("next") or url_for("assets.detail", aid=aid))


@bp.route("/<int:aid>/edit", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.ASSET_EDIT)
def edit(aid):
    a = db.query("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    db.execute("UPDATE assets SET name=?, category=?, serial_no=?, quantity=?, purchase_date=?, cost=?, custodian_id=?, notes=? WHERE id=?",
               (request.form.get("name") or a["name"], request.form.get("category") or a["category"],
                request.form.get("serial_no") or None, request.form.get("quantity", type=int) or 1,
                request.form.get("purchase_date") or None, request.form.get("cost", type=float),
                request.form.get("custodian_id", type=int) or None, request.form.get("notes") or None, aid))
    db.audit(g.user, "update", "asset", aid, f"Edited asset {a['asset_no']}")
    flash("Saved.", "success")
    return redirect(url_for("assets.detail", aid=aid))


@bp.route("/verify-sheet")
@auth.login_required
@auth.require_roles(*VIEW)
def verify_sheet():
    rows = db.query("SELECT a.*, w.name AS custodian FROM assets a LEFT JOIN workers w ON w.id=a.custodian_id "
                    "WHERE a.status != 'disposed' ORDER BY a.location, a.category, a.name")
    return render_template("asset_verify.html", rows=rows, today=db.today_str())
