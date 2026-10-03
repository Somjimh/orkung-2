"""Crops: field blocks, plantings, harvests, sales, and what each block costs.

The crops dashboard answers, for a month: what did labour and inputs cost on
each block and crop, what came off it, what was sold and paid for, and where
paid time or stock is going without a record.
"""
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import auth
import farm

bp = Blueprint("crops", __name__, url_prefix="/crops")


def _month():
    m = request.args.get("month") or farm.this_month()
    start, end = farm.month_bounds(m)
    return start[:7], start, end


def phi_holds(on_date=None):
    """Blocks still inside a chemical's pre-harvest interval: {block_id: (safe_from_date, item_name)}."""
    on_date = on_date or date.today().isoformat()
    rows = db.query("SELECT m.block_id, m.move_date, i.name, i.phi_days FROM stock_movements m "
                    "JOIN stock_items i ON i.id=m.item_id WHERE m.block_id IS NOT NULL AND i.phi_days > 0 "
                    "AND m.move_type='used' AND date(m.move_date) <= date(?) AND date(m.move_date) >= date(?, '-120 day')",
                    (on_date, on_date))
    holds = {}
    for r in rows:
        safe = (date.fromisoformat(r["move_date"][:10]) + timedelta(days=r["phi_days"])).isoformat()
        if safe > on_date and (r["block_id"] not in holds or safe > holds[r["block_id"]][0]):
            holds[r["block_id"]] = (safe, r["name"])
    return holds


@bp.route("")
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet"}))
def index():
    period, start, end = _month()
    today = date.today().isoformat()
    block_rows, general = farm.block_summary(start, end)
    crops = farm.crop_summary(start, end)
    labour = farm.labour_rows(start, end)
    idle = [r for r in labour if r["activity"] == "No job recorded"]
    staff = farm.workers()
    att_today = db.query("SELECT status, COUNT(*) c FROM attendance WHERE work_date=? GROUP BY status", (today,))
    att_map = {r["status"]: r["c"] for r in att_today}
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    marked_y = db.query("SELECT COUNT(*) c FROM attendance WHERE work_date=?", (yesterday,), one=True)["c"]
    sales = db.query("SELECT COALESCE(SUM(kg*price_per_kg),0) v, COALESCE(SUM(amount_paid),0) p FROM crop_sales "
                     "WHERE sale_date BETWEEN ? AND ?", (start, end), one=True)
    owed = db.query("SELECT COALESCE(SUM(kg*price_per_kg - amount_paid),0) v FROM crop_sales", one=True)["v"]
    kg = db.query("SELECT COALESCE(SUM(kg),0) k FROM harvests WHERE harvest_date BETWEEN ? AND ?", (start, end), one=True)["k"]
    plantings = farm.active_plantings()
    holds = phi_holds()
    blk = {b["id"]: b for b in farm.blocks(active_only=False)}
    low = db.query("SELECT i.id, i.name, i.unit, i.reorder_level, COALESCE(SUM(b.qty),0) on_hand FROM stock_items i "
                   "LEFT JOIN stock_batches b ON b.item_id=i.id WHERE i.active=1 AND i.category IN "
                   f"({','.join('?'*len(farm.CROP_CATEGORIES))}) GROUP BY i.id "
                   "HAVING on_hand <= i.reorder_level", farm.CROP_CATEGORIES)
    ms = db.query("SELECT * FROM milestones WHERE status IN ('open','at_risk') AND due_date IS NOT NULL "
                  "AND due_date <= date('now','+14 day') ORDER BY due_date LIMIT 8")
    return render_template(
        "crops_index.html", period=period, label=farm.month_label(period),
        prev=farm.shift_month(period, -1), next=farm.shift_month(period, 1),
        block_rows=block_rows, general=general, crops=crops,
        labour_total=sum(r["cost"] for r in labour), labour_days=_days(labour),
        inputs_total=sum(r["inputs"] for r in block_rows) + general["inputs"],
        idle_cost=sum(r["cost"] for r in idle), idle_days=len(idle),
        staff=staff, att_map=att_map, today=today, yesterday=yesterday, marked_y=marked_y, sales=sales, owed=owed, kg=kg,
        plantings=plantings, holds=holds, blk=blk, low=low, milestones=[(m, farm.rag(m["due_date"], m["status"])) for m in ms],
        can_edit=g.user["role"] in auth.CROP_EDIT, crops_list=farm.CROPS, fmt=farm.fmt)


def _days(labour):
    seen = {}
    for r in labour:
        if r["status"] in ("present", "half"):
            seen[(r["worker_id"], r["work_date"])] = 1.0 if r["status"] == "present" else 0.5
    return sum(seen.values())


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

@bp.route("/blocks/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def add_block():
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip() or code
    if not code:
        flash("Give the block a short code, e.g. D1.", "error")
        return redirect(url_for("crops.index"))
    if db.query("SELECT 1 FROM crop_blocks WHERE lower(code)=lower(?)", (code,), one=True):
        flash(f"Block {code} already exists.", "error")
        return redirect(url_for("crops.index"))
    bid = db.execute("INSERT INTO crop_blocks (code, name, area_acres, water_source, organic, notes) VALUES (?,?,?,?,?,?)",
                     (code, name, request.form.get("area_acres", type=float), request.form.get("water_source") or None,
                      1 if request.form.get("organic") else 0, request.form.get("notes") or None))
    db.audit(g.user, "create", "crop_block", bid, f"Added block {code}")
    flash(f"Block {code} added.", "success")
    return redirect(url_for("crops.block", block_id=bid))


@bp.route("/blocks/<int:block_id>")
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet"}))
def block(block_id):
    b = db.query("SELECT * FROM crop_blocks WHERE id=?", (block_id,), one=True)
    if not b:
        abort(404)
    period, start, end = _month()
    plantings = db.query("SELECT * FROM plantings WHERE block_id=? ORDER BY planted_date DESC", (block_id,))
    labour = [r for r in farm.labour_rows(start, end) if r["block_id"] == block_id]
    inputs = [r for r in farm.input_rows(start, end) if r["block_id"] == block_id]
    harvests = db.query("SELECT * FROM harvests WHERE block_id=? AND harvest_date BETWEEN ? AND ? ORDER BY harvest_date DESC",
                        (block_id, start, end))
    by_act = {}
    for r in labour:
        a = by_act.setdefault(r["activity"], dict(cost=0.0, entries=0, qty={}))
        a["cost"] += r["cost"]
        a["entries"] += 1
        if r["quantity"]:
            a["qty"][r["unit"] or ""] = a["qty"].get(r["unit"] or "", 0) + r["quantity"]
    kg = sum(h["kg"] for h in harvests)
    lab_cost = sum(r["cost"] for r in labour)
    inp_cost = sum(r["cost"] for r in inputs)
    return render_template("crop_block.html", b=b, period=period, label=farm.month_label(period),
                           prev=farm.shift_month(period, -1), next=farm.shift_month(period, 1),
                           plantings=plantings, labour=labour, inputs=inputs, harvests=harvests, by_act=by_act,
                           kg=kg, lab_cost=lab_cost, inp_cost=inp_cost, hold=phi_holds().get(block_id),
                           can_edit=g.user["role"] in auth.CROP_EDIT, crops_list=farm.CROPS, today=db.today_str(),
                           fmt=farm.fmt)


@bp.route("/blocks/<int:block_id>/edit", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def edit_block(block_id):
    b = db.query("SELECT * FROM crop_blocks WHERE id=?", (block_id,), one=True)
    if not b:
        abort(404)
    db.execute("UPDATE crop_blocks SET name=?, area_acres=?, water_source=?, organic=?, notes=?, active=? WHERE id=?",
               (request.form.get("name") or b["name"], request.form.get("area_acres", type=float),
                request.form.get("water_source") or None, 1 if request.form.get("organic") else 0,
                request.form.get("notes") or None, 0 if request.form.get("retired") else 1, block_id))
    db.audit(g.user, "update", "crop_block", block_id, f"Updated block {b['code']}")
    flash("Block saved.", "success")
    return redirect(url_for("crops.block", block_id=block_id))


# ---------------------------------------------------------------------------
# Plantings
# ---------------------------------------------------------------------------

@bp.route("/plantings/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def add_planting():
    block_id = request.form.get("block_id", type=int)
    crop = request.form.get("crop", "").strip()
    planted = request.form.get("planted_date")
    if not block_id or not crop or not db.parse_date(planted):
        flash("Block, crop and planting date are needed.", "error")
        return redirect(request.referrer or url_for("crops.index"))
    pid = db.execute(
        "INSERT INTO plantings (block_id, crop, variety, area_acres, plant_count, planted_date, first_harvest_date, status, notes, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (block_id, crop, request.form.get("variety") or None, request.form.get("area_acres", type=float),
         request.form.get("plant_count", type=int), planted, request.form.get("first_harvest_date") or None,
         request.form.get("status") or "growing", request.form.get("notes") or None, g.user["id"]))
    db.audit(g.user, "create", "planting", pid, f"Planting {crop} on block {block_id} {planted}")
    flash(f"{crop} planting recorded.", "success")
    return redirect(url_for("crops.block", block_id=block_id))


@bp.route("/plantings/<int:planting_id>/update", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def update_planting(planting_id):
    p = db.query("SELECT * FROM plantings WHERE id=?", (planting_id,), one=True)
    if not p:
        abort(404)
    status = request.form.get("status") or p["status"]
    end_date = request.form.get("end_date") or (db.today_str() if status in ("finished", "failed") and not p["end_date"] else p["end_date"])
    db.execute("UPDATE plantings SET status=?, end_date=?, plant_count=?, first_harvest_date=?, notes=? WHERE id=?",
               (status, end_date, request.form.get("plant_count", type=int) or p["plant_count"],
                request.form.get("first_harvest_date") or p["first_harvest_date"],
                request.form.get("notes") if request.form.get("notes") is not None else p["notes"], planting_id))
    db.audit(g.user, "update", "planting", planting_id, f"{p['crop']}: {p['status']} -> {status}")
    flash("Planting updated.", "success")
    return redirect(url_for("crops.block", block_id=p["block_id"]))


# ---------------------------------------------------------------------------
# Harvests
# ---------------------------------------------------------------------------

@bp.route("/harvests", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet"}))
def harvests():
    if request.method == "POST":
        if g.user["role"] not in auth.HARVEST_EDIT:
            abort(403)
        when = request.form.get("harvest_date") or db.today_str()
        block_id = request.form.get("block_id", type=int)
        kg = request.form.get("kg", type=float)
        crop = request.form.get("crop") or (farm.crop_lookup()(block_id, when) if block_id else None)
        if not kg or kg <= 0 or not crop:
            flash("Enter the crop and the kilos weighed.", "error")
            return redirect(url_for("crops.harvests"))
        hold = phi_holds(when).get(block_id) if block_id else None
        if hold and not request.form.get("confirm_phi"):
            flash(f"Not saved: this block was sprayed with {hold[1]} and should not be harvested before {hold[0]}. "
                  f"If the produce will not be sold or eaten, tick the confirm box and save again.", "error")
            return redirect(url_for("crops.harvests"))
        hid = db.execute("INSERT INTO harvests (harvest_date, block_id, crop, grade, kg, notes, created_by) VALUES (?,?,?,?,?,?,?)",
                         (when, block_id, crop, request.form.get("grade") or None, kg,
                          ((request.form.get("notes") or "") + (f" | harvested inside pre-harvest interval ({hold[1]})" if hold else "")).strip(" |") or None,
                          g.user["id"]))
        db.audit(g.user, "create", "harvest", hid, f"Harvest {kg:g} kg {crop}")
        flash(f"Harvest saved: {kg:g} kg {crop}.", "success")
        return redirect(url_for("crops.harvests"))
    period, start, end = _month()
    rows = db.query("SELECT h.*, b.code AS block_code, u.full_name FROM harvests h LEFT JOIN crop_blocks b ON b.id=h.block_id "
                    "LEFT JOIN users u ON u.id=h.created_by WHERE h.harvest_date BETWEEN ? AND ? "
                    "ORDER BY h.harvest_date DESC, h.id DESC", (start, end))
    totals = db.query("SELECT crop, grade, SUM(kg) kg FROM harvests WHERE harvest_date BETWEEN ? AND ? GROUP BY crop, grade ORDER BY crop",
                      (start, end))
    if request.args.get("export"):
        from blueprints.reports import _out
        return _out(f"Harvests {farm.month_label(period)}", ["Date", "Block", "Crop", "Grade", "Kg", "Notes", "Recorded by"],
                    [(r["harvest_date"], r["block_code"] or "", r["crop"], r["grade"] or "", r["kg"], r["notes"] or "",
                      r["full_name"] or "") for r in rows], f"harvests_{period}.csv")
    return render_template("crop_harvests.html", rows=rows, totals=totals, period=period, label=farm.month_label(period),
                           prev=farm.shift_month(period, -1), next=farm.shift_month(period, 1),
                           blocks=farm.blocks(), crops_list=farm.CROPS, today=db.today_str(),
                           can_edit=g.user["role"] in auth.HARVEST_EDIT, can_delete=g.user["role"] in auth.CROP_EDIT,
                           holds=phi_holds())


@bp.route("/harvests/<int:hid>/delete", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def delete_harvest(hid):
    h = db.query("SELECT * FROM harvests WHERE id=?", (hid,), one=True)
    if not h:
        abort(404)
    db.execute("DELETE FROM harvests WHERE id=?", (hid,))
    db.audit(g.user, "delete", "harvest", hid, f"Deleted harvest {h['harvest_date']} {h['kg']:g} kg {h['crop']}",
             details=dict(h))
    flash("Harvest entry deleted (kept in the audit history).", "success")
    return redirect(url_for("crops.harvests", month=h["harvest_date"][:7]))


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

@bp.route("/sales", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet", "worker"}))
def sales():
    if request.method == "POST":
        if g.user["role"] not in auth.CROP_EDIT:
            abort(403)
        kg = request.form.get("kg", type=float)
        price = request.form.get("price_per_kg", type=float)
        crop = request.form.get("crop")
        if not kg or price is None or not crop:
            flash("Crop, kilos and price per kg are needed.", "error")
            return redirect(url_for("crops.sales"))
        paid = request.form.get("amount_paid", type=float) or 0
        when = request.form.get("sale_date") or db.today_str()
        sid = db.execute("INSERT INTO crop_sales (sale_date, crop, grade, kg, price_per_kg, buyer, reference, amount_paid, paid_date, notes, created_by) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (when, crop, request.form.get("grade") or None, kg, price, request.form.get("buyer") or None,
                          request.form.get("reference") or None, paid, when if paid else None,
                          request.form.get("notes") or None, g.user["id"]))
        db.audit(g.user, "create", "crop_sale", sid, f"Sale {kg:g} kg {crop} @ {price:g} to {request.form.get('buyer')}")
        flash(f"Sale saved: KES {kg * price:,.0f}.", "success")
        return redirect(url_for("crops.sales"))
    period, start, end = _month()
    rows = db.query("SELECT * FROM crop_sales WHERE sale_date BETWEEN ? AND ? ORDER BY sale_date DESC, id DESC", (start, end))
    unpaid = db.query("SELECT * FROM crop_sales WHERE amount_paid < kg*price_per_kg - 0.5 ORDER BY sale_date")
    if request.args.get("export"):
        from blueprints.reports import _out
        return _out(f"Crop sales {farm.month_label(period)}",
                    ["Date", "Item", "Grade", "Kg", "Price per kg (KES)", "Value (KES)", "Buyer", "Reference", "Paid (KES)", "Balance (KES)"],
                    [(r["sale_date"], r["crop"], r["grade"] or "", r["kg"], r["price_per_kg"], round(r["kg"] * r["price_per_kg"]),
                      r["buyer"] or "", r["reference"] or "", r["amount_paid"], round(r["kg"] * r["price_per_kg"] - r["amount_paid"]))
                     for r in rows], f"crop_sales_{period}.csv",
                    description="Every sale with item, grade, kilos and price per kilo.")
    return render_template("crop_sales.html", rows=rows, unpaid=unpaid,
                           total_kg=sum(r["kg"] for r in rows), total_value=sum(r["kg"] * r["price_per_kg"] for r in rows),
                           total_paid=sum(r["amount_paid"] for r in rows), period=period, label=farm.month_label(period),
                           prev=farm.shift_month(period, -1), next=farm.shift_month(period, 1),
                           crops_list=farm.CROPS, today=db.today_str(), can_edit=g.user["role"] in auth.CROP_EDIT)


@bp.route("/sales/<int:sid>/payment", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.CROP_EDIT)
def sale_payment(sid):
    s = db.query("SELECT * FROM crop_sales WHERE id=?", (sid,), one=True)
    if not s:
        abort(404)
    amt = request.form.get("amount", type=float) or 0
    if amt <= 0:
        flash("Enter the amount received.", "error")
        return redirect(url_for("crops.sales"))
    note = f"received {amt:,.0f} on {request.form.get('paid_date') or db.today_str()}" + \
           (f" ref {request.form.get('ref')}" if request.form.get("ref") else "")
    db.execute("UPDATE crop_sales SET amount_paid=amount_paid+?, paid_date=?, notes=trim(COALESCE(notes,'') || ' | ' || ?, ' |') WHERE id=?",
               (amt, request.form.get("paid_date") or db.today_str(), note, sid))
    db.audit(g.user, "update", "crop_sale", sid, f"Payment {amt:,.0f} on sale {sid}")
    flash("Payment recorded.", "success")
    return redirect(url_for("crops.sales", month=s["sale_date"][:7]))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@bp.route("/report/blocks")
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet"}))
def report_blocks():
    period, start, end = _month()
    rows, general = farm.block_summary(start, end)
    data = [(r["block"]["code"], r["block"]["name"], r["block"]["area_acres"] or "", round(r["labour"]), round(r["inputs"]),
             round(r["total"]), r["kg"], round(r["per_kg"], 1) if r["per_kg"] else "") for r in rows]
    data.append(("—", "Not on a block (general farm work)", "", round(general["labour"]), round(general["inputs"]),
                 round(general["total"]), general["kg"], ""))
    from blueprints.reports import _out
    return _out(f"Block costs {farm.month_label(period)}",
                ["Block", "Name", "Acres", "Labour (KES)", "Inputs (KES)", "Total (KES)", "Harvest (kg)", "Cost per kg"],
                data, f"block_costs_{period}.csv",
                description="Labour is each person's day cost split across the jobs logged that day. Inputs are store issues "
                            "at the price paid. Pre-tax, new cash only.")


@bp.route("/report/crops")
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet", "worker"}))
def report_crops():
    period, start, end = _month()
    data = [(c, round(v["labour"]), round(v["inputs"]), round(v["cost"]), v["kg"], v["sold_kg"], round(v["sales"]),
             round(v["received"]), round(v["margin"])) for c, v in farm.crop_summary(start, end).items()]
    from blueprints.reports import _out
    return _out(f"Crop margins {farm.month_label(period)}",
                ["Crop", "Labour (KES)", "Inputs (KES)", "Cost (KES)", "Harvested (kg)", "Sold (kg)", "Sales (KES)",
                 "Received (KES)", "Sales less cost (KES)"], data, f"crop_margins_{period}.csv",
                description="Costs follow the crop growing on the block on the day of the work or issue.")


@bp.route("/report/inputs")
@auth.login_required
@auth.require_roles(*(set(auth.ROLES) - {"vet"}))
def report_inputs():
    period, start, end = _month()
    rows = farm.input_rows(start, end)
    from blueprints.reports import _out
    return _out(f"Store issues {farm.month_label(period)}",
                ["Date", "Item", "Category", "Quantity", "Cost (KES)", "Block", "Crop", "Issued to", "Notes"],
                [(r["move_date"], r["item"], r["category"], f"{r['qty']:g} {r['unit']}", round(r["cost"]) if r["priced"] else "no price",
                  r["block_code"] or ("animal treatment" if r["treatment"] else ""), r["crop"] or "", r["worker"] or "",
                  r["notes"] or "") for r in rows], f"store_issues_{period}.csv",
                description="Everything that left the store in the month, who took it and where it went.")
