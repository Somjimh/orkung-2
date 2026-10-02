"""Import farm records from CSV: animals (with mother + status), weights, and
tasks/flags. Every import is preview -> check -> commit, matches animals by
species + tag (leading zeros ignored, so '039' and '39' are the same animal),
and never adds anything twice:

  * animals  -- skipped if the species/tag is already in the register
  * weights  -- skipped if that animal already has a weight on that date
  * tasks    -- skipped if a pending task with the same title already exists
"""
import csv
import os
import uuid
from collections import defaultdict
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, current_app
import db
import helpers
import auth

bp = Blueprint("records_import", __name__, url_prefix="/admin/import-records")

KINDS = {
    "animals": dict(
        label="Animals (with mother and status)",
        header=["species", "tag_id", "sex", "dob", "birth_weight", "dam_tag", "status", "status_date",
                "group", "source", "notes"],
        example=[("Goat", "135", "female", "2026-08-01", "3.5", "039", "active", "", "Growing Kids/Lambs",
                  "born_on_farm", "From births book")]),
    "weights": dict(
        label="Weights",
        header=["species", "tag_id", "measured_on", "weight_kg", "notes"],
        example=[("Goat", "039", "2026-09-12", "31.5", "From weights book")]),
    "tasks": dict(
        label="Tasks / flags",
        header=["title", "description", "priority", "due_date", "task_type"],
        example=[("Check goat 132 (slow growth)", "Only 5.2 kg at 7 weeks", "high", "2026-10-09", "follow_up")]),
}
STATUSES = ("active", "sold", "transferred", "slaughtered", "missing", "deceased")
PRIORITIES = ("normal", "high", "urgent")


def norm_tag(t):
    t = (t or "").strip().lower().rstrip("?").strip()
    return t.lstrip("0") or ("0" if t else "")


def _dir():
    d = os.path.abspath(os.path.join(current_app.config["UPLOAD_DIR"], "..", "imports"))
    os.makedirs(d, exist_ok=True)
    return d


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _valid_date(s):
    return bool(s) and db.parse_date(s) is not None


def _lookups():
    species = {r["name"].lower(): r["id"] for r in db.query("SELECT * FROM species")}
    by_key = {}
    for a in db.query("SELECT id, species_id, tag_id, sex, status FROM animals"):
        key = (a["species_id"], norm_tag(a["tag_id"]))
        # prefer an active animal if a tag was ever reused
        if key not in by_key or a["status"] == "active":
            by_key[key] = a
    return species, by_key


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def _check(kind, rows):
    """Return a list of dicts: line, data, errors (skip), notes (info only)."""
    species, by_key = _lookups()
    out = []
    if kind == "animals":
        seen = set()
        for i, r in enumerate(rows, start=2):
            errs, notes = [], []
            sid = species.get(r.get("species", "").lower())
            tag = norm_tag(r.get("tag_id"))
            if not tag:
                errs.append("missing tag")
            if not sid:
                errs.append("unknown species")
            if r.get("sex", "").lower() not in ("male", "female"):
                errs.append("sex must be male or female")
            if sid and tag and (sid, tag) in by_key:
                errs.append("already on the site (skipped)")
            elif (sid, tag) in seen:
                errs.append("duplicate in this file")
            if r.get("dob") and not _valid_date(r["dob"]):
                errs.append("dob must be YYYY-MM-DD")
            st = (r.get("status") or "active").lower()
            if st not in STATUSES:
                errs.append("unknown status")
            dam = norm_tag(r.get("dam_tag"))
            if dam and sid and (sid, dam) not in by_key and (sid, dam) not in seen:
                notes.append(f"mother {r['dam_tag']} not found, saved in notes only")
            if not errs:
                seen.add((sid, tag))
            out.append(dict(line=i, data=r, errors=errs, notes=notes))
    elif kind == "weights":
        existing = defaultdict(set)
        last = {}
        for w in db.query("SELECT animal_id, measured_on, weight_kg FROM weight_records ORDER BY measured_on"):
            existing[w["animal_id"]].add(w["measured_on"][:10])
            last[w["animal_id"]] = w["weight_kg"]
        seen = set()
        for i, r in enumerate(rows, start=2):
            errs, notes = [], []
            sid = species.get(r.get("species", "").lower())
            a = by_key.get((sid, norm_tag(r.get("tag_id")))) if sid else None
            wt = _f(r.get("weight_kg"))
            d = r.get("measured_on", "")[:10]
            if not sid:
                errs.append("unknown species")
            elif not a:
                errs.append("animal not on the site (import animals first)")
            if not wt or wt <= 0:
                errs.append("bad weight")
            if not _valid_date(d):
                errs.append("date must be YYYY-MM-DD")
            if a and d in existing[a["id"]]:
                errs.append("weight for this date already on the site (skipped)")
            elif a and (a["id"], d) in seen:
                errs.append("duplicate in this file")
            if not errs:
                seen.add((a["id"], d))
                prev = last.get(a["id"])
                if prev and abs(wt - prev) / prev * 100 > current_app.config["WEIGHT_CHANGE_WARN_PCT"]:
                    notes.append("big change from last weight (will be flagged)")
            out.append(dict(line=i, data=r, errors=errs, notes=notes))
    else:
        titles = {t["title"].strip().lower() for t in db.query("SELECT title FROM tasks WHERE status='pending'")}
        seen = set()
        for i, r in enumerate(rows, start=2):
            errs = []
            t = r.get("title", "").strip().lower()
            if not t:
                errs.append("missing title")
            elif t in titles:
                errs.append("already a pending task (skipped)")
            elif t in seen:
                errs.append("duplicate in this file")
            if not _valid_date(r.get("due_date")):
                errs.append("due_date must be YYYY-MM-DD")
            if (r.get("priority") or "normal").lower() not in PRIORITIES:
                errs.append("priority must be normal, high or urgent")
            if not errs:
                seen.add(t)
            out.append(dict(line=i, data=r, errors=errs, notes=[]))
    for o in out:
        o["ok"] = not o["errors"]
    return out


@bp.route("", methods=["GET"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def form():
    return render_template("records_import.html", kinds=KINDS)


@bp.route("/template/<kind>.csv")
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def template(kind):
    k = KINDS.get(kind) or KINDS["weights"]
    return helpers.csv_response(f"{kind}_import_template.csv", k["header"], k["example"])


@bp.route("/preview", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def preview():
    kind = request.form.get("kind")
    file = request.files.get("file")
    if kind not in KINDS or not file or not file.filename:
        flash("Choose what you are importing and a CSV file.", "error")
        return redirect(url_for("records_import.form"))
    token = uuid.uuid4().hex
    path = os.path.join(_dir(), f"{token}.csv")
    file.save(path)
    try:
        rows = _check(kind, _read(path))
    except Exception as e:  # malformed CSV
        os.remove(path)
        flash(f"Could not read that file: {e}", "error")
        return redirect(url_for("records_import.form"))
    return render_template("records_import_preview.html", rows=rows, token=token, kind=kind,
                           label=KINDS[kind]["label"], valid=sum(r["ok"] for r in rows), total=len(rows))


@bp.route("/commit", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def commit():
    token = request.form.get("token", "")
    kind = request.form.get("kind")
    path = os.path.join(_dir(), f"{os.path.basename(token)}.csv")
    if kind not in KINDS or not os.path.exists(path):
        flash("Import session expired. Please upload the file again.", "error")
        return redirect(url_for("records_import.form"))
    checked = _check(kind, _read(path))  # re-check against the live data
    uid = g.user["id"]
    done = 0
    if kind == "animals":
        species, _ = _lookups()
        groups = {r["name"].lower(): r for r in db.query("SELECT * FROM groups_")}
        births = defaultdict(list)
        for row in checked:
            if not row["ok"]:
                continue
            r = row["data"]
            sid = species[r["species"].lower()]
            _, by_key = _lookups()
            dam = by_key.get((sid, norm_tag(r.get("dam_tag")))) if r.get("dam_tag") else None
            grp = groups.get((r.get("group") or "").lower())
            st = (r.get("status") or "active").lower()
            notes = r.get("notes") or ""
            if r.get("dam_tag") and not dam:
                notes = (notes + f" | Mother tag {r['dam_tag']} (not in register)").strip(" |")
            new_id = db.execute(
                "INSERT INTO animals (record_no, tag_id, species_id, sex, dob, birth_weight, dam_id, current_group_id, "
                "current_site_id, source, status, status_date, notes, created_by, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (db.next_record_no(), r["tag_id"].strip(), sid, r["sex"].lower(), r.get("dob") or None,
                 _f(r.get("birth_weight")), dam["id"] if dam else None, grp["id"] if grp else None,
                 grp["site_id"] if grp else None,
                 r.get("source") if r.get("source") in ("born_on_farm", "purchased", "transferred_in") else "born_on_farm",
                 st, r.get("status_date") or None, notes or None, uid, uid))
            if grp:
                db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, created_by) VALUES (?,?,?,?)",
                           (new_id, grp["id"], r.get("dob") or db.today_str(), uid))
            if r.get("dob"):
                db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, notes, created_by) VALUES (?,?,?,?,?)",
                           (new_id, "birth", r["dob"], "Imported from births book", uid))
            if st != "active":
                db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, notes, created_by) VALUES (?,?,?,?,?)",
                           (new_id, "death" if st == "deceased" else "status_change", r.get("status_date") or db.today_str(),
                            notes or None, uid))
            if dam and r.get("dob"):
                births[(dam["id"], r["dob"])].append((new_id, st))
            db.audit(g.user, "import", "animal", new_id, f"Imported {r['species']} {r['tag_id']} from CSV")
            done += 1
        for (dam_id, bdate), kids in births.items():
            exists = db.query("SELECT 1 FROM birth_records WHERE dam_id=? AND birth_date=?", (dam_id, bdate), one=True)
            if exists:
                continue
            br = db.execute(
                "INSERT INTO birth_records (dam_id, birth_date, total_born, born_alive, stillborn, notes, created_by) "
                "VALUES (?,?,?,?,?,?,?)", (dam_id, bdate, len(kids), len(kids), 0, "Imported from births book", uid))
            for kid_id, _ in kids:
                db.execute("INSERT INTO birth_offspring (birth_record_id, animal_id) VALUES (?,?)", (br, kid_id))
    elif kind == "weights":
        species, by_key = _lookups()
        threshold = current_app.config["WEIGHT_CHANGE_WARN_PCT"]
        for row in checked:
            if not row["ok"]:
                continue
            r = row["data"]
            a = by_key[(species[r["species"].lower()], norm_tag(r["tag_id"]))]
            wt = _f(r["weight_kg"])
            prev = db.query("SELECT weight_kg FROM weight_records WHERE animal_id=? AND measured_on<? "
                            "ORDER BY measured_on DESC LIMIT 1", (a["id"], r["measured_on"][:10]), one=True)
            flagged = 1 if prev and abs(wt - prev["weight_kg"]) / prev["weight_kg"] * 100 > threshold else 0
            new_id = db.execute(
                "INSERT INTO weight_records (animal_id, weight_kg, measured_on, method, notes, flagged_outlier, created_by) "
                "VALUES (?,?,?,?,?,?,?)", (a["id"], wt, r["measured_on"][:10], "scale", r.get("notes") or None, flagged, uid))
            db.audit(g.user, "import", "weight_record", new_id, f"Imported weight {wt} kg for {r['tag_id']} on {r['measured_on']}")
            done += 1
    else:
        for row in checked:
            if not row["ok"]:
                continue
            r = row["data"]
            new_id = db.execute(
                "INSERT INTO tasks (task_type, title, description, due_date, priority, created_by) VALUES (?,?,?,?,?,?)",
                (r.get("task_type") or "general", r["title"], r.get("description") or None, r["due_date"],
                 (r.get("priority") or "normal").lower(), uid))
            db.audit(g.user, "import", "task", new_id, f"Imported task: {r['title']}")
            done += 1
    os.remove(path)
    skipped = len(checked) - done
    db.audit(g.user, "import", kind, summary=f"CSV {kind} import: {done} added, {skipped} skipped")
    flash(f"Import complete: {done} {KINDS[kind]['label'].lower()} added, {skipped} skipped.", "success")
    return redirect(url_for("records_import.form"))


# ---------------------------------------------------------------------------
# Duplicate-weight clean-up: the same weight recorded twice for one animal a
# few days apart (e.g. 12 Sep book weights that were first typed in under the
# entry date 20 Sep). Keeps the earlier record, removes the later copy.
# ---------------------------------------------------------------------------
DUP_WINDOW_DAYS = 14


def _find_duplicate_weights():
    rows = db.query(
        "SELECT w.id, w.animal_id, w.weight_kg, w.measured_on, w.method, w.notes, a.tag_id, sp.name AS species "
        "FROM weight_records w JOIN animals a ON a.id=w.animal_id LEFT JOIN species sp ON sp.id=a.species_id "
        "ORDER BY w.animal_id, w.measured_on, w.id")
    by_animal = defaultdict(list)
    for r in rows:
        by_animal[r["animal_id"]].append(r)
    dupes, near = [], []
    for recs in by_animal.values():
        removed = set()
        for i, later in enumerate(recs):
            for earlier in recs[:i]:
                if earlier["id"] in removed or later["id"] in removed:
                    continue
                gap = db.days_between(earlier["measured_on"], later["measured_on"])
                if gap is None or gap < 0 or gap > DUP_WINDOW_DAYS:
                    continue
                if abs(earlier["weight_kg"] - later["weight_kg"]) < 0.05:
                    dupes.append(dict(keep=earlier, drop=later))
                    removed.add(later["id"])
                    break
    return dupes


@bp.route("/duplicate-weights", methods=["GET"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def duplicate_weights():
    return render_template("records_dupes.html", dupes=_find_duplicate_weights(), window=DUP_WINDOW_DAYS)


@bp.route("/duplicate-weights/remove", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def remove_duplicate_weights():
    dupes = _find_duplicate_weights()  # recomputed from live data
    for d in dupes:
        k, x = d["keep"], d["drop"]
        db.execute("DELETE FROM weight_records WHERE id=?", (x["id"],))
        db.audit(g.user, "delete", "weight_record", x["id"],
                 f"Removed duplicate weight {x['weight_kg']} kg on {x['measured_on'][:10]} for {x['species']} {x['tag_id']} "
                 f"(same weight already recorded on {k['measured_on'][:10]})",
                 details=dict(removed=dict(x), kept_id=k["id"]))
    flash(f"Removed {len(dupes)} duplicate weight record(s). The earlier record of each was kept.", "success")
    return redirect(url_for("records_import.duplicate_weights"))
