"""The farm app: the colourful, phone-first screens for the daily work.

* Home      -- a tile for each area of the farm, today's numbers, what needs you.
* My jobs   -- each worker's list for the day, built from their routines. Tick a
               job done (with a weight and a photo where asked), say why it
               couldn't be done, or add a job that wasn't on the list.
* Confirm   -- the supervisor signs the day off in the evening.
* Spray     -- every spray recorded: block, product, amount, water, pest, who,
               gloves and mask, label photo, safe-to-harvest date and bee safety.
* Report    -- the day on one page for Mohamed.
* Routines  -- the manager sets who does what, and how often.

Who can do what:
* A worker whose login is linked to their staff record sees and ticks only
  their own jobs.
* A shared farm-phone login (worker role, not linked) picks the person.
* The supervisor (the staff member chosen in Settings, Godfrey by default) and
  managers confirm the day. Managers and admins can do everything.
"""
import os
import uuid
from datetime import date, datetime, timedelta
from flask import (Blueprint, render_template, request, redirect, url_for, flash, g, abort, session,
                   current_app, send_from_directory)
from werkzeug.utils import secure_filename
import db
import auth
import farm
import farmapp_text as tx

bp = Blueprint("farmapp", __name__, url_prefix="/farm")

APP_ROLES = ("admin", "manager", "worker", "viewer", "storekeeper")
EDIT_ROLES = ("admin", "manager", "worker", "storekeeper")
PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _day():
    d = db.parse_date(request.values.get("d"))
    return d or date.today()


def _photo_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(current_app.config["DATABASE"])), "farm-photos")
    os.makedirs(d, exist_ok=True)
    return d


def _save_photo(field):
    f = request.files.get(field)
    if not f or not f.filename:
        return None
    ext = os.path.splitext(secure_filename(f.filename))[1].lower() or ".jpg"
    if ext not in PHOTO_TYPES:
        raise ValueError("bad_photo")
    name = f"{date.today():%Y%m}-{uuid.uuid4().hex[:12]}{ext}"
    f.save(os.path.join(_photo_dir(), name))
    return name


def linked_worker():
    u = g.user
    wid = u["worker_id"] if u and "worker_id" in u.keys() else None
    if not wid:
        return None
    return db.query("SELECT * FROM workers WHERE id=?", (wid,), one=True)


def is_manager():
    return g.user["role"] in auth.MANAGEMENT


def self_only():
    return g.user["role"] == "worker" and linked_worker() is not None


def _setting_worker(key, name_like):
    """A staff member chosen in settings, or found by name as a sensible default."""
    row = db.query("SELECT value FROM settings WHERE key=?", (key,), one=True)
    if row and row["value"] and str(row["value"]).isdigit():
        w = db.query("SELECT * FROM workers WHERE id=?", (int(row["value"]),), one=True)
        if w:
            return w
    return db.query("SELECT * FROM workers WHERE active=1 AND lower(name) LIKE ? ORDER BY id LIMIT 1",
                    (name_like.lower() + "%",), one=True)


def supervisor():
    return _setting_worker("farm_supervisor_worker_id", "Godfrey")


def apiary_keeper():
    return _setting_worker("farm_apiary_worker_id", "Muli")


def can_confirm():
    if is_manager():
        return True
    me, sup = linked_worker(), supervisor()
    return bool(me and sup and me["id"] == sup["id"])


def can_see_all():
    return g.user["role"] in ("admin", "manager", "viewer", "storekeeper") or can_confirm() or \
        (g.user["role"] == "worker" and not self_only())


def _routine_due(r, d):
    start = date.fromisoformat(r["start_date"][:10])
    if d < start:
        return False
    if r["frequency"] == "daily":
        return str(d.weekday()) in (r["days"] or "0123456")
    if r["frequency"] == "weekly":
        return r["weekday"] is not None and d.weekday() == r["weekday"]
    if r["frequency"] == "every":
        n = r["every_days"] or 14
        last = db.query("SELECT MAX(task_date) d FROM job_tasks WHERE routine_id=? AND status='done'",
                        (r["id"],), one=True)["d"]
        nxt = (date.fromisoformat(last) + timedelta(days=n)) if last else start
        return d >= nxt
    return False


def make_jobs(d):
    """Create the day's jobs from the routines (only today and past days, once each)."""
    if d > date.today():
        return
    iso = d.isoformat()
    for r in db.query("SELECT r.* FROM routines r JOIN workers w ON w.id=r.worker_id WHERE r.active=1 AND w.active=1"):
        if not _routine_due(r, d):
            continue
        if r["frequency"] == "every":
            # one open job at a time: an unfinished one carries on (and shows as overdue)
            if db.query("SELECT 1 FROM job_tasks WHERE routine_id=? AND status='open' AND task_date<=?",
                        (r["id"], iso), one=True):
                continue
        if db.query("SELECT 1 FROM job_tasks WHERE routine_id=? AND task_date=?", (r["id"], iso), one=True):
            continue
        db.execute("INSERT INTO job_tasks (task_date, routine_id, worker_id, area, title_en, title_sw, block_id, "
                   "needs_weight, needs_photo) VALUES (?,?,?,?,?,?,?,?,?)",
                   (iso, r["id"], r["worker_id"], r["area"], r["title_en"], r["title_sw"], r["block_id"],
                    r["needs_weight"], r["needs_photo"]))


def jobs_for(d, worker_id=None):
    """The day's jobs, plus 'every N days' jobs still open from earlier days."""
    iso = d.isoformat()
    sql = ("SELECT t.*, w.name AS worker_name, b.code AS block_code, r.frequency, r.every_days "
           "FROM job_tasks t JOIN workers w ON w.id=t.worker_id LEFT JOIN crop_blocks b ON b.id=t.block_id "
           "LEFT JOIN routines r ON r.id=t.routine_id "
           "WHERE (t.task_date=? OR (t.status='open' AND t.task_date<? AND r.frequency='every'))")
    args = [iso, iso]
    if worker_id:
        sql += " AND t.worker_id=?"
        args.append(worker_id)
    sql += " ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'not_done' THEN 1 ELSE 2 END, t.task_date, t.id"
    out = []
    for t in db.query(sql, args):
        late = (d - date.fromisoformat(t["task_date"])).days
        out.append(dict(t, late=late, title=tx.title(t), area_info=tx.AREA.get(t["area"]) or tx.AREA["general"],
                        area_label=tx.area_name(t["area"])))
    return out


def _can_touch(task):
    if is_manager() or can_confirm():
        return True
    if g.user["role"] not in EDIT_ROLES:
        return False
    if self_only():
        return task["worker_id"] == linked_worker()["id"]
    return True


def safe_periods(on=None):
    on = (on or date.today()).isoformat()
    return db.query("SELECT s.*, b.code AS block_code, b.name AS block_name FROM spray_records s "
                    "JOIN crop_blocks b ON b.id=s.block_id WHERE s.safe_date IS NOT NULL AND s.safe_date>? "
                    "ORDER BY s.safe_date DESC", (on,))


@bp.app_context_processor
def _inject():
    return dict(t=tx.t, lang_now=tx.lang, area_name=tx.area_name)


# ---------------------------------------------------------------------------
# Language and photos
# ---------------------------------------------------------------------------

@bp.route("/lang/<code>")
@auth.login_required
def set_lang(code):
    if code in tx.LANGS:
        session["lang"] = code
    nxt = request.args.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("farmapp.home")
    return redirect(nxt)


@bp.route("/photo/<name>")
@auth.login_required
def photo(name):
    if name != secure_filename(name):
        abort(404)
    return send_from_directory(_photo_dir(), name)


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

@bp.route("")
@auth.login_required
def home():
    if g.user["role"] not in APP_ROLES:      # e.g. the vet: their pages are in the full records
        return redirect(url_for("dashboard.index"))
    if self_only() and not can_confirm():
        return redirect(url_for("farmapp.my_jobs"))
    d = date.today()
    make_jobs(d)
    jobs = jobs_for(d)
    workers = {j["worker_id"] for j in jobs}
    done_workers = {j["worker_id"] for j in jobs if j["status"] == "done"}
    tiles = []
    for key, en, sw, bg, fg in tx.AREAS:
        aj = [j for j in jobs if j["area"] == key]
        people = sorted({j["worker_name"] for j in aj})
        if key == "spraying":
            n = db.query("SELECT COUNT(*) c FROM spray_records WHERE spray_date=?", (d.isoformat(),), one=True)["c"]
            line = (f"{n} " + ("today" if tx.lang() == "en" else "leo")) if n else tx.t("spray")
            tiles.append(dict(key=key, name=tx.area_name(key), bg=bg, fg=fg, people="", line=line,
                              href=url_for("farmapp.spray")))
            continue
        done = sum(1 for j in aj if j["status"] == "done")
        line = f"{done}/{len(aj)} {tx.t('done').lower()}" if aj else "—"
        tiles.append(dict(key=key, name=tx.area_name(key), bg=bg, fg=fg, people=", ".join(people), line=line,
                          href=url_for("farmapp.report", d=d.isoformat(), area=key)))
    needs = _needs_you(d, jobs)
    conf = db.query("SELECT c.*, u.full_name FROM day_confirmations c JOIN users u ON u.id=c.confirmed_by "
                    "WHERE work_date=?", (d.isoformat(),), one=True)
    return render_template("farm/home.html", d=d, day_text=tx.long_date(d), tiles=tiles,
                           at_work=len(done_workers), staff=len(workers),
                           done=sum(1 for j in jobs if j["status"] == "done"), total=len(jobs),
                           needs=needs, conf=conf, can_confirm=can_confirm(), manager=is_manager())


def _needs_you(d, jobs):
    out = []
    for j in jobs:
        if j["status"] == "open" and j["late"] > 0:
            out.append(("red", f"{j['worker_name']}: {j['title']} · {tx.t('overdue').lower()} {j['late']} {tx.t('days')}"))
        elif j["status"] == "not_done":
            out.append(("amber", f"{j['worker_name']}: {j['title']} · {tx.t('not_done').lower()}"
                                 + (f" ({j['note']})" if j["note"] else "")))
    for s in safe_periods(d):
        out.append(("amber", f"{s['block_code']}: {tx.t('safe_from').lower()} {s['safe_date']} ({s['product']})"))
    return out


# ---------------------------------------------------------------------------
# My jobs
# ---------------------------------------------------------------------------

@bp.route("/jobs")
@auth.login_required
@auth.require_roles(*APP_ROLES)
def my_jobs():
    d = _day()
    make_jobs(d)
    me = linked_worker()
    staff = farm.workers()
    if self_only():
        who = me
    else:
        wid = request.args.get("w", type=int)
        who = next((w for w in staff if w["id"] == wid), None) or me
    jobs = jobs_for(d, who["id"]) if who else []
    done = sum(1 for j in jobs if j["status"] == "done")
    return render_template("farm/jobs.html", d=d, day_text=tx.long_date(d), who=who, staff=staff,
                           pick=not self_only(), jobs=jobs, done=done, total=len(jobs),
                           blocks=farm.blocks(), areas=tx.AREAS,
                           can_edit=g.user["role"] in EDIT_ROLES and d <= date.today(),
                           is_today=d == date.today(), prev=(d - timedelta(days=1)).isoformat(),
                           next=(d + timedelta(days=1)).isoformat(), sup=supervisor())


def _back_to_jobs(task_or_wid, d):
    wid = task_or_wid if isinstance(task_or_wid, int) else task_or_wid["worker_id"]
    args = {"d": d}
    if not self_only():
        args["w"] = wid
    return redirect(url_for("farmapp.my_jobs", **args))


@bp.route("/jobs/<int:tid>/done", methods=["POST"])
@auth.login_required
@auth.require_roles(*EDIT_ROLES)
def job_done(tid):
    task = db.query("SELECT * FROM job_tasks WHERE id=?", (tid,), one=True)
    if not task:
        abort(404)
    back_d = request.form.get("d") or task["task_date"]
    if not _can_touch(task):
        abort(403)
    qty = request.form.get("qty", type=float)
    if task["needs_weight"] and (qty is None or qty <= 0):
        flash(tx.t("weight_kg") + "?", "error")
        return _back_to_jobs(task, back_d)
    try:
        ph = _save_photo("photo")
    except ValueError:
        flash("Photo must be a JPG or PNG picture.", "error")
        return _back_to_jobs(task, back_d)
    if task["needs_photo"] and not ph and not task["photo"]:
        flash(tx.t("take_photo") + "!", "error")
        return _back_to_jobs(task, back_d)
    db.execute("UPDATE job_tasks SET status='done', qty=?, unit=?, photo=COALESCE(?, photo), note=?, "
               "done_at=datetime('now'), done_by=? WHERE id=?",
               (qty, "kg" if qty is not None else None, ph, (request.form.get("note") or "").strip() or None,
                g.user["id"], tid))
    # the job counts on the day it was really done
    if task["task_date"] < date.today().isoformat() and task["routine_id"] and back_d == date.today().isoformat():
        clash = db.query("SELECT 1 FROM job_tasks WHERE routine_id=? AND task_date=? AND id!=?",
                         (task["routine_id"], back_d, tid), one=True)
        if not clash:
            db.execute("UPDATE job_tasks SET task_date=? WHERE id=?", (back_d, tid))
    db.audit(g.user, "update", "job_task", tid, f"Done: {task['title_en']}" + (f" ({qty:g} kg)" if qty else ""))
    flash(f"✓ {tx.title(task)}", "success")
    return _back_to_jobs(task, back_d)


@bp.route("/jobs/<int:tid>/not-done", methods=["POST"])
@auth.login_required
@auth.require_roles(*EDIT_ROLES)
def job_not_done(tid):
    task = db.query("SELECT * FROM job_tasks WHERE id=?", (tid,), one=True)
    if not task:
        abort(404)
    if not _can_touch(task):
        abort(403)
    reason = (request.form.get("note") or "").strip()
    if not reason:
        flash(tx.t("reason"), "error")
        return _back_to_jobs(task, request.form.get("d") or task["task_date"])
    db.execute("UPDATE job_tasks SET status='not_done', note=?, done_at=datetime('now'), done_by=? WHERE id=?",
               (reason, g.user["id"], tid))
    db.audit(g.user, "update", "job_task", tid, f"Not done: {task['title_en']} ({reason})")
    return _back_to_jobs(task, request.form.get("d") or task["task_date"])


@bp.route("/jobs/<int:tid>/reopen", methods=["POST"])
@auth.login_required
@auth.require_roles(*EDIT_ROLES)
def job_reopen(tid):
    task = db.query("SELECT * FROM job_tasks WHERE id=?", (tid,), one=True)
    if not task:
        abort(404)
    if not _can_touch(task):
        abort(403)
    if db.query("SELECT 1 FROM day_confirmations WHERE work_date=?", (task["task_date"],), one=True) and not is_manager():
        flash(tx.t("confirmed") + ": " + tx.t("confirmed_by").lower(), "error")
        return _back_to_jobs(task, task["task_date"])
    db.execute("UPDATE job_tasks SET status='open', qty=NULL, unit=NULL, done_at=NULL, done_by=NULL WHERE id=?", (tid,))
    db.audit(g.user, "update", "job_task", tid, f"Reopened: {task['title_en']}")
    return _back_to_jobs(task, request.form.get("d") or task["task_date"])


@bp.route("/jobs/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*EDIT_ROLES)
def job_add():
    d = _day()
    if d > date.today():
        abort(400)
    wid = linked_worker()["id"] if self_only() else request.form.get("worker_id", type=int)
    title = (request.form.get("title") or "").strip()
    area = request.form.get("area") or "general"
    if not wid or not title or area not in tx.AREA:
        flash(tx.t("what_job"), "error")
        return redirect(url_for("farmapp.my_jobs", d=d.isoformat()))
    qty = request.form.get("qty", type=float)
    try:
        ph = _save_photo("photo")
    except ValueError:
        flash("Photo must be a JPG or PNG picture.", "error")
        return _back_to_jobs(wid, d.isoformat())
    tid = db.execute("INSERT INTO job_tasks (task_date, worker_id, area, title_en, title_sw, block_id, status, qty, unit, "
                     "photo, note, done_at, done_by, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,?)",
                     (d.isoformat(), wid, area, title, title, request.form.get("block_id", type=int) or None, "done",
                      qty, "kg" if qty else None, ph, (request.form.get("note") or "").strip() or None,
                      g.user["id"], g.user["id"]))
    db.audit(g.user, "create", "job_task", tid, f"Extra job for worker {wid}: {title}")
    flash(f"✓ {title}", "success")
    return _back_to_jobs(wid, d.isoformat())


# ---------------------------------------------------------------------------
# Confirm the day (supervisor)
# ---------------------------------------------------------------------------

@bp.route("/confirm", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*APP_ROLES)
def confirm():
    d = _day()
    if not can_confirm():
        abort(403)
    make_jobs(d)
    if request.method == "POST":
        if d > date.today():
            abort(400)
        for j in jobs_for(d):
            mark = request.form.get(f"m_{j['id']}")
            if mark == "not_done" and j["status"] != "not_done":
                db.execute("UPDATE job_tasks SET status='not_done', note=COALESCE(note, ?), done_by=? WHERE id=?",
                           ("by the supervisor", g.user["id"], j["id"]))
        db.execute("INSERT INTO day_confirmations (work_date, confirmed_by, note) VALUES (?,?,?) "
                   "ON CONFLICT(work_date) DO UPDATE SET confirmed_by=excluded.confirmed_by, "
                   "confirmed_at=datetime('now'), note=excluded.note",
                   (d.isoformat(), g.user["id"], (request.form.get("note") or "").strip() or None))
        db.audit(g.user, "update", "day_confirmation", None, f"Confirmed {d.isoformat()}")
        flash(f"{tx.t('confirmed')} ✓", "success")
        return redirect(url_for("farmapp.report", d=d.isoformat()))
    jobs = jobs_for(d)
    people = {}
    for j in jobs:
        people.setdefault(j["worker_name"], []).append(j)
    conf = db.query("SELECT * FROM day_confirmations WHERE work_date=?", (d.isoformat(),), one=True)
    return render_template("farm/confirm.html", d=d, day_text=tx.long_date(d), people=people, conf=conf)


# ---------------------------------------------------------------------------
# Spraying
# ---------------------------------------------------------------------------

def _spray_items():
    return db.query("SELECT * FROM stock_items WHERE active=1 AND category IN ('chemical','fertiliser') ORDER BY name")


@bp.route("/spray", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*APP_ROLES)
def spray():
    if request.method == "POST":
        return _save_spray()
    recent = db.query("SELECT s.*, b.code AS block_code, b.name AS block_name, w.name AS worker_name FROM spray_records s "
                      "JOIN crop_blocks b ON b.id=s.block_id LEFT JOIN workers w ON w.id=s.worker_id "
                      "ORDER BY s.spray_date DESC, s.id DESC LIMIT 15")
    me = linked_worker()
    return render_template("farm/spray.html", items=_spray_items(), blocks=farm.blocks(), staff=farm.workers(),
                           me=me, self_only=self_only(), recent=recent, keeper=apiary_keeper(),
                           can_edit=g.user["role"] in EDIT_ROLES, today=date.today().isoformat(),
                           now=datetime.now().strftime("%H:%M"))


def _save_spray():
    if g.user["role"] not in EDIT_ROLES:
        abort(403)
    f = request.form
    d = db.parse_date(f.get("spray_date")) or date.today()
    if d > date.today():
        flash("You cannot record a spray in the future.", "error")
        return redirect(url_for("farmapp.spray"))
    block = db.query("SELECT * FROM crop_blocks WHERE id=?", (f.get("block_id", type=int),), one=True)
    amount = f.get("amount", type=float)
    unit = f.get("unit") or ""
    wid = linked_worker()["id"] if self_only() else f.get("worker_id", type=int)
    item = None
    choice = f.get("item") or ""
    if choice.isdigit():
        item = db.query("SELECT * FROM stock_items WHERE id=? AND active=1", (int(choice),), one=True)
    product = item["name"] if item else (f.get("product") or "").strip()
    if not block or not product or not amount or amount <= 0 or not wid:
        flash(tx.t("block") + ", " + tx.t("product") + ", " + tx.t("amount") + ", " + tx.t("sprayed_by") + "?", "error")
        return redirect(url_for("farmapp.spray"))
    phi = item["phi_days"] if item and item["phi_days"] else f.get("phi_days", type=int)
    bee = bool(item and item["bee_harmful"]) or bool(f.get("bee_harmful"))
    if bee and not f.get("bee_ack"):
        flash(f"{tx.t('harmful_bees')}: {tx.t('bee_ack')}.", "error")
        return redirect(url_for("farmapp.spray"))
    flag = ""
    if item and block["organic"] and not item["organic_ok"]:
        if not f.get("confirm_organic"):
            flash(f"{item['name']} is not marked as allowed for organic use and block {block['code']} is in the "
                  f"certified organic area. Check with the certifier first; if approved, tick the confirm box.", "error")
            return redirect(url_for("farmapp.spray"))
        flag = " | NOT ORGANIC-APPROVED, used on organic block (confirmed)"
    try:
        ph = _save_photo("photo")
    except ValueError:
        flash("Photo must be a JPG or PNG picture.", "error")
        return redirect(url_for("farmapp.spray"))
    cost = None
    msg = ""
    if item:
        from blueprints.stock import take_out, to_base
        qty, base = to_base(amount, unit)
        if qty is None or base != item["unit"]:
            flash(f"{item['name']}: {item['unit']}?", "error")
            return redirect(url_for("farmapp.spray"))
        cost = take_out(item, qty, d.isoformat(), "used", None, f"Spray: {f.get('pest') or ''}{flag}".strip(),
                        g.user, block_id=block["id"], worker_id=wid)
    safe = (d + timedelta(days=phi)).isoformat() if phi else None
    sid = db.execute("INSERT INTO spray_records (spray_date, spray_time, block_id, item_id, product, amount, unit, water_l, "
                     "pest, worker_id, ppe, phi_days, safe_date, bee_harmful, bee_note, photo, cost, notes, created_by) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d.isoformat(), f.get("spray_time") or None, block["id"], item["id"] if item else None, product,
                      amount, unit, f.get("water_l", type=float), (f.get("pest") or "").strip() or None, wid,
                      1 if f.get("ppe") else 0, phi, safe, 1 if bee else 0,
                      "evening; apiary keeper told" if bee else None, ph, cost,
                      ((f.get("notes") or "").strip() + flag).strip(" |") or None, g.user["id"]))
    keeper = apiary_keeper()
    if keeper:
        db.execute("INSERT INTO job_tasks (task_date, worker_id, area, title_en, title_sw, block_id, created_by) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (d.isoformat(), keeper["id"], "apiary",
                    f"Spray on {block['code']} ({product}): keep the bees safe",
                    f"Dawa imenyunyiziwa {block['code']} ({product}): linda nyuki", block["id"], g.user["id"]))
    db.audit(g.user, "create", "spray_record", sid, f"Sprayed {amount:g} {unit} {product} on {block['code']}{flag}")
    msg = f"✓ {product} · {block['code']}"
    if safe:
        msg += f" · {tx.t('safe_from')} {safe}"
    flash(msg, "success")
    return redirect(url_for("farmapp.spray"))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@bp.route("/report")
@auth.login_required
@auth.require_roles(*APP_ROLES)
def report():
    d = _day()
    if not can_see_all():
        return redirect(url_for("farmapp.my_jobs"))
    make_jobs(d)
    jobs = jobs_for(d)
    area = request.args.get("area")
    if area in tx.AREA:
        jobs = [j for j in jobs if j["area"] == area]
    people = {}
    for j in jobs:
        p = people.setdefault(j["worker_name"], dict(name=j["worker_name"], jobs=[], done=0))
        p["jobs"].append(j)
        p["done"] += 1 if j["status"] == "done" else 0
    weighed = [j for j in jobs if j["status"] == "done" and j["qty"]]
    sprays = db.query("SELECT s.*, b.code AS block_code, w.name AS worker_name FROM spray_records s "
                      "JOIN crop_blocks b ON b.id=s.block_id LEFT JOIN workers w ON w.id=s.worker_id "
                      "WHERE s.spray_date=? ORDER BY s.id", (d.isoformat(),))
    conf = db.query("SELECT c.*, u.full_name FROM day_confirmations c JOIN users u ON u.id=c.confirmed_by "
                    "WHERE work_date=?", (d.isoformat(),), one=True)
    return render_template("farm/report.html", d=d, day_text=tx.long_date(d), people=list(people.values()),
                           done=sum(1 for j in jobs if j["status"] == "done"), total=len(jobs),
                           weighed=weighed, sprays=sprays, needs=_needs_you(d, jobs), conf=conf,
                           area=tx.AREA.get(area), prev=(d - timedelta(days=1)).isoformat(),
                           next=(d + timedelta(days=1)).isoformat(), is_today=d == date.today(),
                           can_confirm=can_confirm())


# ---------------------------------------------------------------------------
# Routines (manager)
# ---------------------------------------------------------------------------

SUGGESTED = [
    # worker name starts with, area, English, Kiswahili, frequency, days/weekday/every, start, weight, photo
    ("Godfrey", "livestock", "Check, feed and water the goats and sheep", "Kagua, lisha na nywesha mbuzi na kondoo",
     "daily", "0123456", None),
    ("Kajona", "livestock", "Feed and water the cows", "Lisha na nywesha ng'ombe", "daily", "012345", None),
    ("Kapuo", "water", "Walk the irrigation lines and fix leaks", "Kagua mabomba ya umwagiliaji na rekebisha mivujo",
     "daily", "0123456", None),
    ("Kapuo", "water", "Wednesday meeting with Mohamed: piping issues", "Mkutano wa Jumatano na Mohamed: mabomba",
     "weekly", 2, None),
    ("Prince", "fodder", "Cut and weigh Lucerne A", "Kata na upime Lusina A", "every", 14, "weight"),
    ("Prince", "fodder", "Cut and weigh Lucerne B", "Kata na upime Lusina B", "every", 14, "weight"),
    ("Muli", "nursery", "Water and check the nursery", "Mwagilia na kagua kitalu", "daily", "0123456", None),
    ("Muli", "apiary", "Apiary: check the water and keep it clean", "Nyuki: kagua maji na safisha eneo",
     "daily", "0123456", "2026-11-15"),
    ("Muli", "apiary", "Apiary: inspect the hives", "Nyuki: kagua mizinga", "weekly", 5, "2026-11-15|photo"),
    ("Ali", "general", "Help Muli and Prince (as they direct)", "Saidia Muli na Prince (kama wanavyoelekeza)",
     "daily", "012345", "2026-11-01"),
]


@bp.route("/routines", methods=["GET", "POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def routines():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "suggested":
            n = _load_suggested()
            flash(f"{n} routine jobs added." if n else "Nothing added: the suggested jobs are already there, "
                  "or the staff names were not found.", "success" if n else "info")
        elif action == "toggle":
            rid = f.get("rid", type=int)
            db.execute("UPDATE routines SET active=1-active WHERE id=?", (rid,))
        elif action == "settings":
            for key in ("farm_supervisor_worker_id", "farm_apiary_worker_id"):
                v = f.get(key, type=int)
                db.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, str(v) if v else ""))
            flash("Saved.", "success")
        else:
            freq = f.get("frequency")
            if freq not in ("daily", "weekly", "every") or not f.get("worker_id", type=int) or not (f.get("title_en") or "").strip() \
                    or f.get("area") not in tx.AREA:
                flash("Fill in the person, area, job and how often.", "error")
                return redirect(url_for("farmapp.routines"))
            days = "".join(sorted(f.getlist("days"))) or "0123456"
            rid = db.execute("INSERT INTO routines (worker_id, area, title_en, title_sw, frequency, days, weekday, every_days, "
                             "start_date, block_id, needs_weight, needs_photo, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f.get("worker_id", type=int), f.get("area"), f.get("title_en").strip(),
                              (f.get("title_sw") or "").strip() or None, freq, days,
                              f.get("weekday", type=int) if freq == "weekly" else None,
                              max(1, f.get("every_days", type=int) or 14) if freq == "every" else None,
                              (db.parse_date(f.get("start_date")) or date.today()).isoformat(),
                              f.get("block_id", type=int) or None, 1 if f.get("needs_weight") else 0,
                              1 if f.get("needs_photo") else 0, g.user["id"]))
            db.audit(g.user, "create", "routine", rid, f"Routine: {f.get('title_en')}")
            flash("Routine job added.", "success")
        return redirect(url_for("farmapp.routines"))
    wd = tx.WEEKDAYS["en"]
    rows = []
    for r in db.query("SELECT r.*, w.name AS worker_name, b.code AS block_code FROM routines r JOIN workers w ON w.id=r.worker_id "
                      "LEFT JOIN crop_blocks b ON b.id=r.block_id ORDER BY r.active DESC, w.name, r.id"):
        if r["frequency"] == "daily":
            when = "every day" if r["days"] == "0123456" else ", ".join(wd[int(i)][:3] for i in r["days"])
        elif r["frequency"] == "weekly":
            when = f"every {wd[r['weekday']]}" if r["weekday"] is not None else "weekly"
        else:
            when = f"every {r['every_days']} days"
        rows.append(dict(r, when=when))
    return render_template("farm/routines.html", rows=rows, staff=farm.workers(), blocks=farm.blocks(), areas=tx.AREAS,
                           weekdays=tx.WEEKDAYS[tx.lang()], sup=supervisor(), keeper=apiary_keeper(),
                           today=date.today().isoformat())


def _load_suggested():
    n = 0
    for who, area, en, sw, freq, when, extra in SUGGESTED:
        w = db.query("SELECT * FROM workers WHERE active=1 AND lower(name) LIKE ? ORDER BY id LIMIT 1",
                     (who.lower() + "%",), one=True)
        if not w or db.query("SELECT 1 FROM routines WHERE worker_id=? AND title_en=?", (w["id"], en), one=True):
            continue
        start = date.today().isoformat()
        weight = photo = 0
        for part in (extra or "").split("|"):
            if part == "weight":
                weight = photo = 1
            elif part == "photo":
                photo = 1
            elif db.parse_date(part):
                start = part
        db.execute("INSERT INTO routines (worker_id, area, title_en, title_sw, frequency, days, weekday, every_days, "
                   "start_date, needs_weight, needs_photo, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (w["id"], area, en, sw, freq, when if freq == "daily" else "0123456",
                    when if freq == "weekly" else None, when if freq == "every" else None, start, weight, photo,
                    g.user["id"]))
        n += 1
    if n:
        db.audit(g.user, "create", "routine", None, f"Loaded {n} suggested routine jobs")
    return n
