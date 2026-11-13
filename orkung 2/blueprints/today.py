"""Today: one screen for everything done on the farm in a day.

Four parts, each filled in as the work happens:

* Jobs      -- who did what, where and how much (work_logs, the same table the
               daily sheet and payroll use, so pay follows real work).
* Feeding   -- what each herd or group was given (feed_logs). Feed taken from
               the store comes off the shelf at the store price.
* Spraying  -- product, block, rate and pest (stock_movements 'used' with a
               block, exactly like a store issue), so the waiting period before
               harvest is worked out and shown.
* Generator -- hour meter start and stop, diesel added, what it powered
               (generator_runs), plus services and repairs (asset_events) with
               a reminder when the next service is due.

Who may enter what:
* A farm worker whose login is linked to a staff member records their own work
  only; the name is fixed to them.
* Any other login allowed to use the log (a supervisor on the farm phone, the
  manager, the storekeeper) picks the person for each entry.
* People can remove their own entries on the day they made them; the manager
  and admin can remove any entry. Entries that took stock off the shelf are
  corrected in the store instead, so the shelf never drifts.
"""
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import auth
import farm
from blueprints.stock import take_out, to_base

bp = Blueprint("today", __name__, url_prefix="/today")

SPRAY_CATEGORIES = ("chemical", "fertiliser")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _day():
    d = db.parse_date(request.values.get("d"))
    return (d or date.today()).isoformat()


def _linked_worker():
    """The staff member this login belongs to, if it is linked to one."""
    u = g.user
    wid = u["worker_id"] if "worker_id" in u.keys() else None
    if not wid:
        return None
    return db.query("SELECT * FROM workers WHERE id=?", (wid,), one=True)


def _self_only():
    """A farm worker with a linked staff record only records their own work."""
    return g.user["role"] == "worker" and _linked_worker() is not None


def _entry_worker():
    if _self_only():
        return _linked_worker()["id"]
    return request.form.get("worker_id", type=int)


def _is_manager():
    return g.user["role"] in auth.MANAGEMENT


def _can_remove(row, date_field):
    if _is_manager():
        return True
    return row["created_by"] == g.user["id"] and (row[date_field] or "")[:10] == date.today().isoformat()


def _back(d):
    return redirect(url_for("today.index", d=d) + request.form.get("anchor", ""))


def _check_day(d):
    """Return an error message if entries can't be added for this day."""
    if d > date.today().isoformat():
        return "You cannot record work for a future date."
    if farm.month_locked(d):
        return f"{d[:7]} pay has been approved, so that month is frozen."
    if not _is_manager() and d < (date.today() - timedelta(days=3)).isoformat():
        return "Entries older than 3 days can only be added by the manager."
    return None


def generators():
    return db.query("SELECT * FROM assets WHERE status NOT IN ('disposed','lost') AND "
                    "(category='Generator' OR lower(name) LIKE '%generator%' OR lower(name) LIKE '%genset%') "
                    "ORDER BY name")


def gen_status(a):
    """Latest hour meter reading, the last service, and when the next one is due."""
    last_run = db.query("SELECT MAX(stop_hours) h FROM generator_runs WHERE asset_id=?", (a["id"],), one=True)["h"]
    last_ev = db.query("SELECT MAX(meter_hours) h FROM asset_events WHERE asset_id=? AND meter_hours IS NOT NULL",
                       (a["id"],), one=True)["h"]
    readings = [x for x in (last_run, last_ev) if x is not None]
    meter = max(readings) if readings else None
    svc = db.query("SELECT event_date, meter_hours FROM asset_events WHERE asset_id=? AND event_type='service' "
                   "AND meter_hours IS NOT NULL ORDER BY meter_hours DESC, id DESC LIMIT 1", (a["id"],), one=True)
    every = a["service_hours"] if "service_hours" in a.keys() and a["service_hours"] else farm.GEN_SERVICE_DEFAULT_HOURS
    since = (meter - svc["meter_hours"]) if (meter is not None and svc) else None
    due_at = (svc["meter_hours"] + every) if svc else None
    if svc is None:
        state = "unknown"
    elif since >= every:
        state = "due"
    elif since >= every * 0.9:
        state = "soon"
    else:
        state = "ok"
    month = db.query("SELECT COALESCE(SUM(stop_hours-start_hours),0) hrs, COALESCE(SUM(diesel_l),0) l, "
                     "COALESCE(SUM(diesel_cost),0) kes FROM generator_runs WHERE asset_id=? AND run_date>=?",
                     (a["id"], date.today().strftime("%Y-%m-01")), one=True)
    return dict(asset=a, meter=meter, last_service=svc, every=every, since=since, due_at=due_at, state=state,
                month_hours=month["hrs"], month_litres=month["l"], month_kes=month["kes"],
                l_per_hour=(month["l"] / month["hrs"]) if month["hrs"] else None)


def diesel_item():
    return db.query("SELECT * FROM stock_items WHERE active=1 AND category='fuel' AND lower(name) LIKE '%diesel%' "
                    "ORDER BY id LIMIT 1", one=True)


def on_hand(item_id):
    r = db.query("SELECT COALESCE(SUM(qty),0) q FROM stock_batches WHERE item_id=?", (item_id,), one=True)
    return r["q"]


def _base_for(item, amount, unit):
    """Convert what was typed to the item's base unit. Returns None if they don't fit."""
    qty, base = to_base(amount, unit)
    if qty is None:
        return None
    if base != item["unit"]:
        return None
    return qty


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def day_records(d):
    """Everything recorded for one day: jobs, feeding, spraying, generator,
    who was at work, a summary and the things to look at. Shared by the
    Today page, the Daily Sheet and the printable day report."""
    jobs = db.query("SELECT l.*, w.name, b.code AS block_code, u.full_name AS by_name FROM work_logs l "
                    "JOIN workers w ON w.id=l.worker_id LEFT JOIN crop_blocks b ON b.id=l.block_id "
                    "LEFT JOIN users u ON u.id=l.created_by WHERE l.work_date=? ORDER BY w.name, l.id", (d,))
    feeds = db.query("SELECT f.*, gr.name AS group_name, w.name AS worker_name, u.full_name AS by_name FROM feed_logs f "
                     "LEFT JOIN groups_ gr ON gr.id=f.group_id LEFT JOIN workers w ON w.id=f.worker_id "
                     "LEFT JOIN users u ON u.id=f.created_by WHERE f.feed_date=? ORDER BY f.id", (d,))
    sprays = db.query("SELECT m.move_date, m.block_id, m.worker_id, m.notes, m.created_by, i.name AS item, i.unit, "
                      "i.phi_days, b.code AS block_code, b.organic, w.name AS worker_name, u.full_name AS by_name, "
                      "-SUM(m.qty) AS qty, SUM(-m.qty*COALESCE(m.unit_cost,0)) AS cost FROM stock_movements m "
                      "JOIN stock_items i ON i.id=m.item_id LEFT JOIN crop_blocks b ON b.id=m.block_id "
                      "LEFT JOIN workers w ON w.id=m.worker_id LEFT JOIN users u ON u.id=m.created_by "
                      "WHERE m.move_type='used' AND m.block_id IS NOT NULL AND i.category IN ('chemical','fertiliser') "
                      "AND substr(m.move_date,1,10)=? "
                      "GROUP BY m.item_id, m.block_id, m.worker_id, m.created_at ORDER BY m.created_at", (d,))
    spray_rows = []
    for s in sprays:
        safe = (date.fromisoformat(s["move_date"][:10]) + timedelta(days=s["phi_days"])).isoformat() if s["phi_days"] else None
        spray_rows.append(dict(s, safe=safe))
    runs = db.query("SELECT r.*, a.name AS gen_name, b.code AS block_code, w.name AS worker_name, u.full_name AS by_name "
                    "FROM generator_runs r JOIN assets a ON a.id=r.asset_id LEFT JOIN crop_blocks b ON b.id=r.block_id "
                    "LEFT JOIN workers w ON w.id=r.worker_id LEFT JOIN users u ON u.id=r.created_by "
                    "WHERE r.run_date=? ORDER BY r.start_hours", (d,))
    services = db.query("SELECT e.*, a.name AS gen_name FROM asset_events e JOIN assets a ON a.id=e.asset_id "
                        "WHERE e.event_date=? AND e.event_type IN ('service','repair') AND e.meter_hours IS NOT NULL", (d,))

    gens = [gen_status(a) for a in generators()]

    # Jobs ticked by staff on their phones (farm app), with before/after photos
    tasks = db.query("SELECT t.*, w.name AS worker_name, b.code AS block_code FROM job_tasks t "
                     "JOIN workers w ON w.id=t.worker_id LEFT JOIN crop_blocks b ON b.id=t.block_id "
                     "WHERE t.task_date=? ORDER BY w.name, t.status, t.id", (d,))
    tasks = [dict(t, title=(t["title_en"] or "")) for t in tasks]
    photos = [t for t in tasks if t["photo_before"] or t["photo"]]

    # Day summary and things to look at (shown to everyone; the manager acts on them)
    present = db.query("SELECT w.id, w.name FROM attendance a JOIN workers w ON w.id=a.worker_id "
                       "WHERE a.work_date=? AND a.status IN ('present','half') ORDER BY w.name", (d,))
    with_job = {j["worker_id"] for j in jobs} | {t["worker_id"] for t in tasks if t["status"] == "done"}
    no_job = [p for p in present if p["id"] not in with_job]
    gen_hours = sum((r["stop_hours"] - r["start_hours"]) for r in runs)
    diesel = sum((r["diesel_l"] or 0) for r in runs)
    flags = []
    for p in no_job:
        flags.append(("amber", f"{p['name']} is marked at work but has no job recorded."))
    for t in tasks:
        if t["status"] == "not_done":
            flags.append(("amber", f"{t['worker_name']} could not do \"{t['title']}\"" + (f": {t['note']}" if t["note"] else ".")))
        elif t["status"] == "open" and t["photo_before"]:
            flags.append(("amber", f"{t['worker_name']} started \"{t['title']}\" (before photo taken) but has not finished it."))
    for gs in gens:
        if gs["state"] == "due":
            flags.append(("red", f"{gs['asset']['name']} is due for a service: {gs['since']:g} hours since the last one "
                                 f"(every {gs['every']:g})."))
        elif gs["state"] == "soon":
            flags.append(("amber", f"{gs['asset']['name']} service due at {gs['due_at']:g} hours "
                                   f"(meter now {gs['meter']:g})."))
        elif gs["state"] == "unknown" and gs["meter"] is not None:
            flags.append(("gray", f"{gs['asset']['name']}: record the last service and its hour reading, "
                                  f"so service reminders can start."))
    for r in runs:
        if not r["diesel_l"] and (r["stop_hours"] - r["start_hours"]) >= 3:
            flags.append(("amber", f"{r['gen_name']} ran {r['stop_hours'] - r['start_hours']:g} hours with no diesel recorded."))
    gaps = _meter_gaps(runs)
    flags.extend(("amber", g_) for g_ in gaps)
    active_groups = db.query("SELECT gr.id, gr.name FROM groups_ gr WHERE gr.active=1 AND EXISTS "
                             "(SELECT 1 FROM animals an WHERE an.current_group_id=gr.id AND an.status='active') "
                             "ORDER BY gr.name")
    fed = {f["group_id"] for f in feeds}
    unfed = [x for x in active_groups if x["id"] not in fed]
    if feeds and unfed:
        flags.append(("gray", "No feeding recorded yet for: " + ", ".join(x["name"] for x in unfed) + "."))
    for s in spray_rows:
        if s["safe"]:
            flags.append(("amber", f"Block {s['block_code']}: no harvest for sale before {s['safe']} ({s['item']})."))

    summary = dict(at_work=len(present), jobs=len(jobs) + sum(1 for t in tasks if t["status"] == "done"),
                   app_open=sum(1 for t in tasks if t["status"] == "open"), feed_kg=sum(f["kg"] for f in feeds),
                   feed_cost=sum((f["cost"] or 0) for f in feeds), sprays=len(spray_rows),
                   gen_hours=gen_hours, diesel=diesel)

    return dict(jobs=jobs, feeds=feeds, sprays=spray_rows, runs=runs, services=services, gens=gens,
                present=present, no_job=no_job, flags=flags, summary=summary, tasks=tasks, photos=photos)


@bp.route("")
@auth.login_required
@auth.require_roles(*auth.DAILY_VIEW)
def index():
    d = _day()
    dd = date.fromisoformat(d)
    me = _linked_worker()
    self_only = _self_only()

    r = day_records(d)
    jobs, feeds, spray_rows, runs, services, gens = r["jobs"], r["feeds"], r["sprays"], r["runs"], r["services"], r["gens"]
    summary, flags = r["summary"], r["flags"]

    stock_feeds = db.query("SELECT * FROM stock_items WHERE active=1 AND category='feed' ORDER BY name")
    spray_items = db.query("SELECT * FROM stock_items WHERE active=1 AND category IN ('chemical','fertiliser') ORDER BY name")
    groups = db.query("SELECT gr.*, (SELECT COUNT(*) FROM animals an WHERE an.current_group_id=gr.id "
                      "AND an.status='active') AS head FROM groups_ gr WHERE gr.active=1 ORDER BY gr.name")

    return render_template(
        "today.html", d=d, day_name=dd.strftime("%A %d %B %Y"),
        prev=(dd - timedelta(days=1)).isoformat(), next=(dd + timedelta(days=1)).isoformat(),
        is_today=d == date.today().isoformat(), future=d > date.today().isoformat(),
        blocked=_check_day(d), can_add=g.user["role"] in auth.DAILY_LOG, is_manager=_is_manager(),
        me=me, self_only=self_only, staff=farm.workers(), blocks=farm.blocks(),
        activities=farm.ACTIVITIES, units=farm.OUTPUT_UNITS,
        jobs=jobs, feeds=feeds, sprays=spray_rows, runs=runs, services=services, gens=gens,
        summary=summary, flags=flags, groups=groups, forage=farm.FORAGE, stock_feeds=stock_feeds,
        spray_items=spray_items, gen_uses=farm.GEN_USES, service_types=farm.GEN_SERVICE_TYPES,
        diesel=diesel_item(), on_hand=on_hand, uid=g.user["id"], today_iso=date.today().isoformat())


def _meter_gaps(runs):
    """Flag where a run's start reading doesn't follow on from the previous stop."""
    out = []
    by_gen = {}
    for r in runs:
        by_gen.setdefault(r["asset_id"], []).append(r)
    for aid, rs in by_gen.items():
        first = rs[0]
        prev = db.query("SELECT stop_hours FROM generator_runs WHERE asset_id=? AND (run_date<? OR (run_date=? AND start_hours<?)) "
                        "ORDER BY run_date DESC, stop_hours DESC LIMIT 1",
                        (aid, first["run_date"], first["run_date"], first["start_hours"]), one=True)
        last = prev["stop_hours"] if prev else None
        for r in rs:
            if last is not None and abs(r["start_hours"] - last) > 0.5:
                kind = "ran without being recorded" if r["start_hours"] > last else "reading is lower than the last one"
                out.append(f"{r['gen_name']}: meter jumped from {last:g} to {r['start_hours']:g} "
                           f"({kind}). Check the reading.")
            last = r["stop_hours"]
    return out


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@bp.route("/job", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def add_job():
    d = _day()
    err = _check_day(d)
    wid = _entry_worker()
    activity = (request.form.get("activity") or "").strip()
    if err:
        flash(err, "error")
        return _back(d)
    if not wid or not activity:
        flash("Choose the person and the job.", "error")
        return _back(d)
    if not db.query("SELECT 1 FROM workers WHERE id=?", (wid,), one=True):
        abort(400)
    lid = db.execute("INSERT INTO work_logs (work_date, worker_id, activity, block_id, hours, quantity, unit, notes, source, created_by) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (d, wid, activity, request.form.get("block_id", type=int) or None, request.form.get("hours", type=float),
                      request.form.get("quantity", type=float), request.form.get("unit") or None,
                      (request.form.get("notes") or "").strip() or None, "today", g.user["id"]))
    db.audit(g.user, "create", "work_log", lid, f"Today log: {activity} for worker {wid} on {d}")
    flash("Job saved.", "success")
    return _back(d)


@bp.route("/job/<int:lid>/delete", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def delete_job(lid):
    row = db.query("SELECT * FROM work_logs WHERE id=?", (lid,), one=True)
    if not row:
        abort(404)
    if not _can_remove(row, "created_at") or farm.month_locked(row["work_date"]):
        flash("Only the person who entered it (on the same day) or the manager can remove this.", "error")
    elif row["source"] == "sheet":
        flash("This is the main job from the daily sheet; change it on the daily sheet.", "error")
    else:
        db.execute("DELETE FROM work_logs WHERE id=?", (lid,))
        db.audit(g.user, "delete", "work_log", lid, f"Deleted job {row['activity']} {row['work_date']}", details=dict(row))
        flash("Job removed.", "success")
    return redirect(url_for("today.index", d=row["work_date"]))


# ---------------------------------------------------------------------------
# Feeding
# ---------------------------------------------------------------------------

@bp.route("/feed", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def add_feed():
    d = _day()
    err = _check_day(d)
    if err:
        flash(err, "error")
        return _back(d)
    group_id = request.form.get("group_id", type=int) or None
    herd = (request.form.get("herd") or "").strip() or None
    choice = request.form.get("feed") or ""
    kg = request.form.get("kg", type=float)
    wid = _entry_worker() or None
    if not (group_id or herd) or not choice or not kg or kg <= 0:
        flash("Say which group, what feed and how many kg.", "error")
        return _back(d)
    item = None
    feed_name = choice
    if choice.startswith("store:"):
        item = db.query("SELECT * FROM stock_items WHERE id=? AND category='feed'", (int(choice[6:]),), one=True)
        if not item:
            abort(400)
        feed_name = item["name"]
    elif choice not in farm.FORAGE:
        abort(400)
    cost = None
    msg = f"Feeding saved: {kg:g} kg of {feed_name}."
    if item:
        qty = _base_for(item, kg, "kg")
        if qty is None:
            flash(f"{item['name']} is kept in the store by {item['unit']}, not by weight, so it can't be taken "
                  f"off by kg here. Issue it from the store instead.", "error")
            return _back(d)
        shelf = on_hand(item["id"])
        group = db.query("SELECT name FROM groups_ WHERE id=?", (group_id,), one=True) if group_id else None
        cost = take_out(item, qty, d, "used", None, f"Fed to {group['name'] if group else herd}", g.user, worker_id=wid)
        msg += " Taken off the store shelf."
        if qty > shelf + 1e-9:
            msg += " More was used than the store shows: please count this item."
    fid = db.execute("INSERT INTO feed_logs (feed_date, group_id, herd, feed, item_id, kg, cost, worker_id, notes, created_by) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (d, group_id, herd, feed_name, item["id"] if item else None, kg, cost, wid,
                      (request.form.get("notes") or "").strip() or None, g.user["id"]))
    db.audit(g.user, "create", "feed_log", fid, f"Fed {kg:g} kg {feed_name} on {d}")
    flash(msg, "success")
    return _back(d)


@bp.route("/feed/<int:fid>/delete", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def delete_feed(fid):
    row = db.query("SELECT * FROM feed_logs WHERE id=?", (fid,), one=True)
    if not row:
        abort(404)
    if row["item_id"]:
        flash("This feed came out of the store. Correct it in the store (Adjust) so the shelf stays right.", "error")
    elif not _can_remove(row, "created_at"):
        flash("Only the person who entered it (on the same day) or the manager can remove this.", "error")
    else:
        db.execute("DELETE FROM feed_logs WHERE id=?", (fid,))
        db.audit(g.user, "delete", "feed_log", fid, f"Deleted feeding {row['feed']} {row['feed_date']}", details=dict(row))
        flash("Feeding removed.", "success")
    return redirect(url_for("today.index", d=row["feed_date"]))


# ---------------------------------------------------------------------------
# Spraying (a store issue to a block)
# ---------------------------------------------------------------------------

@bp.route("/spray", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def add_spray():
    d = _day()
    err = _check_day(d)
    if err:
        flash(err, "error")
        return _back(d)
    item = db.query("SELECT * FROM stock_items WHERE id=? AND active=1", (request.form.get("item_id", type=int),), one=True)
    block = db.query("SELECT * FROM crop_blocks WHERE id=?", (request.form.get("block_id", type=int),), one=True)
    amount = request.form.get("amount", type=float)
    unit = request.form.get("unit") or ""
    wid = _entry_worker()
    pest = (request.form.get("pest") or "").strip()
    rate = (request.form.get("rate_note") or "").strip()
    if not item or item["category"] not in SPRAY_CATEGORIES or not block or not amount or amount <= 0 or not wid:
        flash("Choose the product, the block, the amount used and who sprayed.", "error")
        return _back(d)
    qty = _base_for(item, amount, unit)
    if qty is None:
        flash(f"{item['name']} is kept in {item['unit']}. Enter the amount in "
              f"{'ml or litres' if item['unit'] == 'ml' else 'g or kg' if item['unit'] == 'g' else item['unit']}.", "error")
        return _back(d)
    flag = ""
    if block["organic"] and not item["organic_ok"]:
        if not request.form.get("confirm_organic"):
            flash(f"Not saved: {item['name']} is not marked as allowed for organic use and block {block['code']} is in "
                  f"the certified organic area. Check with the certifier first. If this is approved, tick the confirm box.",
                  "error")
            return _back(d)
        flag = " | NOT ORGANIC-APPROVED, used on organic block (confirmed)"
    shelf = on_hand(item["id"])
    note = "; ".join(x for x in [("Spray: " + pest) if pest else "Spray", rate] if x) + flag
    take_out(item, qty, d, "used", None, note, g.user, block_id=block["id"],
             planting_id=request.form.get("planting_id", type=int) or None, worker_id=wid)
    db.audit(g.user, "create", "stock_issue", item["id"],
             f"Today log: sprayed {qty:g} {item['unit']} {item['name']} on block {block['code']}{flag}")
    msg = f"Spraying saved: {amount:g} {unit} of {item['name']} on block {block['code']}."
    if item["phi_days"]:
        safe = (date.fromisoformat(d) + timedelta(days=item["phi_days"])).isoformat()
        msg += f" No harvest for sale from {block['code']} before {safe}."
    if qty > shelf + 1e-9:
        msg += " More was used than the store shows: please count this item."
    flash(msg, "success" if qty <= shelf + 1e-9 else "info")
    return _back(d)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

@bp.route("/generator", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def add_run():
    d = _day()
    err = _check_day(d)
    if err:
        flash(err, "error")
        return _back(d)
    a = db.query("SELECT * FROM assets WHERE id=?", (request.form.get("asset_id", type=int),), one=True)
    start = request.form.get("start_hours", type=float)
    stop = request.form.get("stop_hours", type=float)
    litres = request.form.get("diesel_l", type=float)
    wid = _entry_worker() or None
    if not a or start is None or stop is None:
        flash("Choose the generator and enter the hour meter at start and at stop.", "error")
        return _back(d)
    if stop < start:
        flash("The stop reading must be the same as or higher than the start reading.", "error")
        return _back(d)
    if stop - start > 24:
        flash(f"That is {stop - start:g} hours in one day. Check the readings.", "error")
        return _back(d)
    if litres is not None and litres < 0:
        flash("Diesel added can't be negative.", "error")
        return _back(d)
    cost = request.form.get("diesel_cost", type=float)
    from_store = 0
    msg = f"Generator run saved: {stop - start:g} hours."
    ditem = diesel_item()
    if litres and ditem and request.form.get("from_store"):
        qty = _base_for(ditem, litres, "l")
        if qty is not None:
            shelf = on_hand(ditem["id"])
            cost = take_out(ditem, qty, d, "used", None, f"Generator {a['name']}", g.user,
                            block_id=request.form.get("block_id", type=int) or None, worker_id=wid) or cost
            from_store = 1
            msg += f" {litres:g} L diesel taken off the store shelf."
            if qty > shelf + 1e-9:
                msg += " More diesel was used than the store shows: please count it."
    rid = db.execute("INSERT INTO generator_runs (run_date, asset_id, start_hours, stop_hours, diesel_l, diesel_cost, from_store, "
                     "powered, block_id, worker_id, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d, a["id"], start, stop, litres, cost, from_store, request.form.get("powered") or None,
                      request.form.get("block_id", type=int) or None, wid,
                      (request.form.get("notes") or "").strip() or None, g.user["id"]))
    db.audit(g.user, "create", "generator_run", rid, f"{a['name']} {start:g}-{stop:g} h, diesel {litres or 0:g} L on {d}")
    gs = gen_status(a)
    if gs["state"] == "due":
        msg += f" Service is now due ({gs['since']:g} hours since the last one)."
    flash(msg, "success")
    return _back(d)


@bp.route("/generator/<int:rid>/delete", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def delete_run(rid):
    row = db.query("SELECT * FROM generator_runs WHERE id=?", (rid,), one=True)
    if not row:
        abort(404)
    if row["from_store"] and not _is_manager():
        flash("The diesel for this run came out of the store, so only the manager can remove it "
              "(and adjust the store if it was not really used).", "error")
        return redirect(url_for("today.index", d=row["run_date"]))
    if not _can_remove(row, "created_at"):
        flash("Only the person who entered it (on the same day) or the manager can remove this.", "error")
    else:
        db.execute("DELETE FROM generator_runs WHERE id=?", (rid,))
        db.audit(g.user, "delete", "generator_run", rid, f"Deleted generator run {row['run_date']}", details=dict(row))
        flash("Generator run removed." + (" The diesel taken from the store was not put back: adjust the store if "
                                          "it was not really used." if row["from_store"] else ""), "success")
    return redirect(url_for("today.index", d=row["run_date"]))


@bp.route("/generator/service", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.DAILY_LOG)
def add_service():
    d = _day()
    err = _check_day(d)
    if err:
        flash(err, "error")
        return _back(d)
    a = db.query("SELECT * FROM assets WHERE id=?", (request.form.get("asset_id", type=int),), one=True)
    kind = request.form.get("kind")
    meter = request.form.get("meter_hours", type=float)
    what = (request.form.get("notes") or "").strip()
    if not a or kind not in dict(farm.GEN_SERVICE_TYPES) or meter is None or not what:
        flash("Choose the generator, the hour meter reading and say what was done.", "error")
        return _back(d)
    eid = db.execute("INSERT INTO asset_events (asset_id, event_date, event_type, worker_id, cost, notes, created_by, meter_hours) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (a["id"], d, kind, _entry_worker() or None, request.form.get("cost", type=float), what,
                      g.user["id"], meter))
    if kind == "repair" and request.form.get("still_faulty"):
        db.execute("UPDATE assets SET condition='needs_repair' WHERE id=?", (a["id"],))
    db.audit(g.user, "update", "asset", a["id"], f"{a['name']}: {kind} at {meter:g} h ({what})")
    flash(f"{'Service' if kind == 'service' else 'Repair'} saved for {a['name']} at {meter:g} hours.", "success")
    return _back(d)


@bp.route("/generator/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def add_generator():
    from blueprints.assets import next_asset_no
    d = _day()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the generator a name, e.g. 'Generator 10 kVA'.", "error")
        return _back(d)
    every = request.form.get("service_hours", type=float) or farm.GEN_SERVICE_DEFAULT_HOURS
    aid = db.execute("INSERT INTO assets (asset_no, name, category, location, service_hours) VALUES (?,?,?,?,?)",
                     (next_asset_no(), name, "Generator", request.form.get("location") or None, every))
    db.audit(g.user, "create", "asset", aid, f"Added generator {name}")
    flash(f"{name} added. Record its last service and hour reading so reminders can start.", "success")
    return _back(d)
