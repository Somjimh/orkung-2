"""Medicine & supplies stock: what is on the shelf, what it cost, what is
running low or expiring, and what each treatment used.

Quantities are held in a base unit per item (ml, g or pcs). Treatments recorded
on the site take stock off automatically (earliest expiry first) when the
medicine name matches a stock item and the dose unit fits the item's unit.
"""
from datetime import date, timedelta
import calendar
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import helpers
import auth

bp = Blueprint("stock", __name__, url_prefix="/stock")

STOCK_STAFF = ("admin", "manager", "worker", "vet")
EXPIRY_WARN_DAYS = 90

UNIT_MAP = {  # written unit -> (base unit, multiplier)
    "ml": ("ml", 1), "mls": ("ml", 1), "l": ("ml", 1000), "lt": ("ml", 1000), "ltr": ("ml", 1000),
    "litre": ("ml", 1000), "liter": ("ml", 1000), "litres": ("ml", 1000), "litr": ("ml", 1000),
    "g": ("g", 1), "gm": ("g", 1), "gms": ("g", 1), "grams": ("g", 1), "kg": ("g", 1000),
    "pcs": ("pcs", 1), "pc": ("pcs", 1), "piece": ("pcs", 1), "pieces": ("pcs", 1), "tablet": ("pcs", 1),
    "tablets": ("pcs", 1), "bolus": ("pcs", 1), "boluses": ("pcs", 1), "stick": ("pcs", 1), "sticks": ("pcs", 1),
    "dose": ("dose", 1), "doses": ("dose", 1), "vial": ("pcs", 1), "vials": ("pcs", 1),
}


def to_base(amount, unit):
    """(amount, written unit) -> (amount in base unit, base unit) or (None, None)."""
    if amount is None:
        return None, None
    u = (unit or "").strip().lower().rstrip(".")
    if u not in UNIT_MAP:
        return None, None
    base, mult = UNIT_MAP[u]
    return float(amount) * mult, base


def parse_amount(text):
    """'250ml' / '1 litr' / '25 g' / '10' -> (number, unit)."""
    t = (text or "").strip().lower().replace(",", ".")
    num = ""
    for ch in t:
        if ch.isdigit() or ch == ".":
            num += ch
        elif num:
            break
    unit = t[len(t.split(num, 1)[0]) + len(num):].strip() if num else ""
    try:
        return (float(num) if num else None), unit
    except ValueError:
        return None, unit


def parse_expiry(text):
    """'05/28', '5/2028', '2028-05', '2028-05-31' -> last day of that month as YYYY-MM-DD."""
    t = (text or "").strip()
    if not t or t in ("-", "—", "N/A", "n/a"):
        return None
    d = db.parse_date(t)
    if d:
        return d.isoformat()
    parts = t.replace("-", "/").replace(".", "/").split("/")
    try:
        if len(parts) == 2:
            a, b = parts
            if len(a) == 4:
                y, m = int(a), int(b)
            else:
                m, y = int(a), int(b)
                y = y + 2000 if y < 100 else y
            return date(y, m, calendar.monthrange(y, m)[1]).isoformat()
        if len(parts) == 3:
            dd, m, y = (int(x) for x in parts)
            y = y + 2000 if y < 100 else y
            return date(y, m, dd).isoformat()
    except ValueError:
        return None
    return None


def unit_cost(item, batch=None):
    """KES per base unit: the batch's own price if it has one, else the item's price."""
    price = (batch["price_per_pack"] if batch is not None and batch["price_per_pack"] else None) or item["price_per_pack"]
    if price and item["pack_size"]:
        return price / item["pack_size"]
    return None


def find_item(name):
    if not name:
        return None
    return db.query("SELECT * FROM stock_items WHERE lower(name)=lower(?) AND active=1", (name.strip(),), one=True)


def take_out(item, qty, when, move_type="used", treatment_id=None, notes=None, user=None):
    """Remove qty (base units) from an item's batches, earliest expiry first."""
    remaining = qty
    cost = unit_cost(item)
    total_cost = 0.0
    batches = db.query("SELECT * FROM stock_batches WHERE item_id=? AND qty>0 "
                       "ORDER BY CASE WHEN expiry IS NULL THEN 1 ELSE 0 END, expiry, id", (item["id"],))
    for b in batches:
        if remaining <= 0:
            break
        part = min(b["qty"], remaining)
        bcost = unit_cost(item, b)
        db.execute("UPDATE stock_batches SET qty=qty-? WHERE id=?", (part, b["id"]))
        db.execute("INSERT INTO stock_movements (item_id, batch_id, move_date, move_type, qty, unit_cost, treatment_id, notes, created_by) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (item["id"], b["id"], when, move_type, -part, bcost, treatment_id, notes,
                                                   user["id"] if user else None))
        total_cost += part * (bcost or 0)
        remaining -= part
    if remaining > 1e-9:  # not enough on the shelf: record it so the shortfall shows
        last = db.query("SELECT id FROM stock_batches WHERE item_id=? ORDER BY id DESC LIMIT 1", (item["id"],), one=True)
        if last:
            db.execute("UPDATE stock_batches SET qty=qty-? WHERE id=?", (remaining, last["id"]))
        db.execute("INSERT INTO stock_movements (item_id, batch_id, move_date, move_type, qty, unit_cost, treatment_id, notes, created_by) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (item["id"], last["id"] if last else None, when, move_type, -remaining, cost,
                                                   treatment_id, ((notes or "") + " | more used than was in stock: please count").strip(" |"),
                                                   user["id"] if user else None))
        total_cost += remaining * (cost or 0)
    return total_cost or None


def deduct_for_treatment(treatment_id, medicine_name, dose, dose_unit, when, user):
    """Called after a treatment is saved. Returns a short message or None."""
    item = find_item(medicine_name)
    if not item:
        return None
    try:
        amount = float(str(dose).replace(",", ".")) if dose not in (None, "") else None
    except ValueError:
        amount = None
    if amount is None or amount <= 0:
        return f"Stock not updated for {item['name']}: no dose entered."
    qty, base = to_base(amount, dose_unit or item["unit"])
    if qty is None or base != item["unit"]:
        return f"Stock not updated for {item['name']}: dose unit '{dose_unit}' does not match stock unit '{item['unit']}'."
    take_out(item, qty, when, "used", treatment_id, "Used in treatment", user)
    return None


def _summary():
    items = db.query("SELECT * FROM stock_items WHERE active=1 ORDER BY name")
    today = date.today()
    warn = (today + timedelta(days=EXPIRY_WARN_DAYS)).isoformat()
    out = []
    for it in items:
        bs = db.query("SELECT * FROM stock_batches WHERE item_id=? ORDER BY expiry", (it["id"],))
        on_hand = sum(b["qty"] for b in bs)
        live = [b for b in bs if b["qty"] > 0]
        first_exp = min((b["expiry"] for b in live if b["expiry"]), default=None)
        cost = unit_cost(it)
        flags = []
        if on_hand < -1e-9:
            flags.append(("red", "Count needed"))
        elif on_hand <= 1e-9:
            flags.append(("red", "Out of stock"))
        elif it["reorder_level"] and on_hand < it["reorder_level"]:
            flags.append(("amber", "Low"))
        if any(b["expiry"] and b["expiry"] < today.isoformat() for b in live):
            flags.append(("red", "Expired stock"))
        elif first_exp and first_exp <= warn:
            flags.append(("amber", "Expires soon"))
        if any((b["condition"] or "").lower().startswith(("damag", "expir")) for b in live):
            flags.append(("red", "Damaged"))
        if cost is None and not any(b["price_per_pack"] for b in bs):
            flags.append(("gray", "No price"))
        used90 = db.query("SELECT -SUM(qty) s FROM stock_movements WHERE item_id=? AND move_type='used' "
                          "AND date(move_date) >= date('now','-90 day')", (it["id"],), one=True)["s"] or 0
        out.append(dict(item=it, on_hand=on_hand, packs=(on_hand / it["pack_size"]) if it["pack_size"] else None,
                        value=(sum(b["qty"] * (unit_cost(it, b) or 0) for b in live) if live and any(unit_cost(it, b) for b in live) else None), first_exp=first_exp,
                        flags=flags, used90=used90, batches=len(live)))
    return out


@bp.route("")
@auth.login_required
def index():
    rows = _summary()
    total = sum(r["value"] or 0 for r in rows)
    unpriced = sum(1 for r in rows if r["item"]["price_per_pack"] is None)
    labels = lambda r: {f[1] for f in r["flags"]}
    n_low = sum(1 for r in rows if labels(r) & {"Low", "Out of stock", "Count needed"})
    n_exp = sum(1 for r in rows if labels(r) & {"Expired stock", "Expires soon"})
    return render_template("stock_index.html", rows=rows, total=total, unpriced=unpriced, n_low=n_low, n_exp=n_exp,
                           warn_days=EXPIRY_WARN_DAYS,
                           can_edit=g.user["role"] in STOCK_STAFF, can_manage=g.user["role"] in auth.MANAGEMENT,
                           today=db.today_str())


@bp.route("/report")
@auth.login_required
def report():
    rows = _summary()
    header = ["Item", "Pack size", "On hand", "Packs (approx.)", "Price per pack (KES)", "Value (KES)",
              "Earliest expiry", "Used last 90 days", "Alerts"]
    data = [(r["item"]["name"], f"{r['item']['pack_size']:g} {r['item']['unit']}", f"{r['on_hand']:g} {r['item']['unit']}",
             f"{r['packs']:.1f}" if r["packs"] is not None else "", f"{r['item']['price_per_pack']:,.0f}" if r["item"]["price_per_pack"] else "",
             f"{r['value']:,.0f}" if r["value"] is not None else "", r["first_exp"] or "", f"{r['used90']:g} {r['item']['unit']}",
             ", ".join(f[1] for f in r["flags"])) for r in rows]
    total = sum(r["value"] or 0 for r in rows)
    from blueprints.reports import _out
    return _out("Medicine Stock", header, data, "medicine_stock.csv",
                description=f"Total stock value KES {total:,.0f} (priced items only). Alerts: low, out of stock, expiring within "
                            f"{EXPIRY_WARN_DAYS} days, expired, damaged, no price.")


@bp.route("/usage")
@auth.login_required
def usage():
    rows = db.query(
        "SELECT m.move_date, i.name, i.unit, -m.qty AS qty, m.unit_cost, a.tag_id, sp.name AS species, t.treatment_type "
        "FROM stock_movements m JOIN stock_items i ON i.id=m.item_id LEFT JOIN treatments t ON t.id=m.treatment_id "
        "LEFT JOIN animals a ON a.id=t.animal_id LEFT JOIN species sp ON sp.id=a.species_id "
        "WHERE m.move_type='used' ORDER BY m.move_date DESC, i.name")
    header = ["Date", "Item", "Used", "Cost (KES)", "Animal", "Treatment"]
    data = [(r["move_date"][:10], r["name"], f"{r['qty']:g} {r['unit']}",
             f"{r['qty'] * r['unit_cost']:,.0f}" if r["unit_cost"] else "",
             f"{r['species'] or ''} {r['tag_id'] or ''}".strip(), r["treatment_type"] or "") for r in rows]
    total = sum((r["qty"] * r["unit_cost"]) for r in rows if r["unit_cost"])
    from blueprints.reports import _out
    return _out("Medicine Usage & Cost", header, data, "medicine_usage.csv",
                description=f"Every quantity taken off stock by a treatment. Total cost KES {total:,.0f} (priced items only).")


@bp.route("/<int:item_id>")
@auth.login_required
def item(item_id):
    it = db.query("SELECT * FROM stock_items WHERE id=?", (item_id,), one=True)
    if not it:
        abort(404)
    batches = db.query("SELECT * FROM stock_batches WHERE item_id=? ORDER BY qty<=0, expiry, id", (item_id,))
    moves = db.query(
        "SELECT m.*, b.batch_no, u.full_name, a.tag_id, sp.name AS species FROM stock_movements m "
        "LEFT JOIN stock_batches b ON b.id=m.batch_id LEFT JOIN users u ON u.id=m.created_by "
        "LEFT JOIN treatments t ON t.id=m.treatment_id LEFT JOIN animals a ON a.id=t.animal_id "
        "LEFT JOIN species sp ON sp.id=a.species_id WHERE m.item_id=? ORDER BY m.move_date DESC, m.id DESC LIMIT 300", (item_id,))
    medicines = db.query("SELECT id, name FROM medicines ORDER BY name")
    return render_template("stock_item.html", it=it, batches=batches, moves=moves, medicines=medicines,
                           cost=unit_cost(it), on_hand=sum(b["qty"] for b in batches),
                           value=sum(b["qty"] * (unit_cost(it, b) or 0) for b in batches if b["qty"] > 0),
                           can_edit=g.user["role"] in STOCK_STAFF, can_manage=g.user["role"] in auth.MANAGEMENT,
                           today=db.today_str())


@bp.route("/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def add_item():
    name = request.form.get("name", "").strip()
    size, unit = parse_amount(request.form.get("pack_size"))
    size_b, base = to_base(size, unit or request.form.get("unit"))
    if not name or not size_b:
        flash("Enter a name and a pack size with its unit, e.g. 100 ml, 1 L, 25 g, 10 pcs.", "error")
        return redirect(url_for("stock.index"))
    if find_item(name):
        flash("An item with that name already exists.", "error")
        return redirect(url_for("stock.index"))
    new_id = db.execute("INSERT INTO stock_items (name, category, pack_size, unit, price_per_pack, reorder_level, notes) VALUES (?,?,?,?,?,?,?)",
                        (name, request.form.get("category", "medicine"), size_b, base,
                         request.form.get("price_per_pack", type=float), (request.form.get("reorder_packs", type=float) or 0) * size_b,
                         request.form.get("notes")))
    db.audit(g.user, "create", "stock_item", new_id, f"Added stock item {name}")
    flash("Item added. Now record a purchase or count to put stock on it.", "success")
    return redirect(url_for("stock.item", item_id=new_id))


@bp.route("/<int:item_id>/edit", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def edit_item(item_id):
    it = db.query("SELECT * FROM stock_items WHERE id=?", (item_id,), one=True)
    if not it:
        abort(404)
    price = request.form.get("price_per_pack", type=float)
    reorder = request.form.get("reorder_packs", type=float)
    db.execute("UPDATE stock_items SET price_per_pack=?, reorder_level=?, category=?, medicine_id=?, notes=? WHERE id=?",
               (price, (reorder or 0) * it["pack_size"], request.form.get("category") or it["category"],
                request.form.get("medicine_id", type=int) or None, request.form.get("notes"), item_id))
    db.audit(g.user, "update", "stock_item", item_id, f"Updated {it['name']}: price {price}, reorder {reorder} packs")
    flash("Saved.", "success")
    return redirect(url_for("stock.item", item_id=item_id))


@bp.route("/<int:item_id>/move", methods=["POST"])
@auth.login_required
@auth.require_roles(*STOCK_STAFF)
def move(item_id):
    it = db.query("SELECT * FROM stock_items WHERE id=?", (item_id,), one=True)
    if not it:
        abort(404)
    kind = request.form.get("kind")
    when = request.form.get("move_date") or db.today_str()
    notes = request.form.get("notes") or None
    packs = request.form.get("packs", type=float) or 0
    loose = request.form.get("loose", type=float) or 0
    qty = packs * it["pack_size"] + loose
    if kind == "purchase":
        if qty <= 0:
            flash("Enter how many packs (and/or loose amount) came in.", "error")
            return redirect(url_for("stock.item", item_id=item_id))
        price = request.form.get("price_per_pack", type=float)
        if price:
            db.execute("UPDATE stock_items SET price_per_pack=? WHERE id=?", (price, item_id))
            it = db.query("SELECT * FROM stock_items WHERE id=?", (item_id,), one=True)
        bid = db.execute("INSERT INTO stock_batches (item_id, batch_no, expiry, location, condition, qty, price_per_pack) VALUES (?,?,?,?,?,?,?)",
                         (item_id, request.form.get("batch_no") or None, parse_expiry(request.form.get("expiry")),
                          request.form.get("location") or None, "Good", qty, price or it["price_per_pack"]))
        b = db.query("SELECT * FROM stock_batches WHERE id=?", (bid,), one=True)
        db.execute("INSERT INTO stock_movements (item_id, batch_id, move_date, move_type, qty, unit_cost, notes, created_by) VALUES (?,?,?,?,?,?,?,?)",
                   (item_id, bid, when, "purchase", qty, unit_cost(it, b), notes or request.form.get("supplier"), g.user["id"]))
        db.audit(g.user, "create", "stock_movement", item_id, f"Purchase of {qty:g} {it['unit']} {it['name']}")
        flash(f"Purchase recorded: {qty:g} {it['unit']}.", "success")
    elif kind in ("used", "disposed"):
        if qty <= 0:
            flash("Enter the amount.", "error")
            return redirect(url_for("stock.item", item_id=item_id))
        take_out(it, qty, when, kind, None, notes, g.user)
        db.audit(g.user, "create", "stock_movement", item_id, f"{kind.capitalize()} {qty:g} {it['unit']} {it['name']}")
        flash(f"{'Use' if kind == 'used' else 'Disposal'} recorded: {qty:g} {it['unit']}.", "success")
    elif kind == "count":
        bid = request.form.get("batch_id", type=int)
        b = db.query("SELECT * FROM stock_batches WHERE id=? AND item_id=?", (bid, item_id), one=True)
        if not b:
            flash("Choose the batch you counted.", "error")
            return redirect(url_for("stock.item", item_id=item_id))
        diff = qty - b["qty"]
        db.execute("UPDATE stock_batches SET qty=? WHERE id=?", (qty, bid))
        db.execute("INSERT INTO stock_movements (item_id, batch_id, move_date, move_type, qty, unit_cost, notes, created_by) VALUES (?,?,?,?,?,?,?,?)",
                   (item_id, bid, when, "adjust", diff, unit_cost(it, b), (notes or "") + f" | counted {qty:g} {it['unit']}", g.user["id"]))
        db.audit(g.user, "update", "stock_batch", bid, f"Count {it['name']} batch {b['batch_no']}: {b['qty']:g} -> {qty:g}")
        flash(f"Count saved. Difference {diff:+g} {it['unit']}.", "success")
    return redirect(url_for("stock.item", item_id=item_id))
