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
    "observations": dict(
        label="Health observations",
        header=["date", "species", "tag_id", "signs", "temperature", "eating", "suspected_problem", "vet_diagnosis",
                "action", "seen_by", "notes"],
        example=[("2026-10-05", "Goat", "132", "Coughing, runny nose", "39.8", "Less than usual", "Pneumonia?", "",
                  "Treated", "Juma", "Kept in shade")]),
    "stock": dict(
        label="Medicine stock count",
        header=["count_date", "item", "category", "location", "pack_size", "full_packs", "loose_qty", "batch_no", "expiry",
                "condition", "price_per_pack", "reorder_packs", "notes"],
        example=[("2026-10-02", "Tylosin", "medicine", "Shelf", "100 ml", "1", "", "260104", "12/28", "Good", "850", "1", "")]),
    "treatments": dict(
        label="Treatments",
        header=["date", "species", "tag_id", "treatment_type", "medicine", "dose", "dose_unit", "route", "reason",
                "given_by", "vet", "withdrawal_days", "follow_up_date", "result", "notes"],
        example=[("2026-10-05", "Goat", "ALL", "Deworming", "Albendazole (dewormer)", "5", "ml", "Oral", "Routine deworming",
                  "Farm worker", "", "", "2026-10-19", "", "ALL = every active goat")]),
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


ALIASES = {
    "tag": "tag_id", "tagno": "tag_id", "tagnumber": "tag_id", "animaltag": "tag_id",
    "treatment": "treatment_type", "treatmenttype": "treatment_type", "type": "treatment_type",
    "medicine": "medicine", "medicinevaccine": "medicine", "drug": "medicine", "product": "medicine",
    "unit": "dose_unit", "doseunit": "dose_unit", "reasonsigns": "reason", "reason": "reason",
    "givenby": "given_by", "vet": "vet", "veterinarian": "vet", "withdrawaldays": "withdrawal_days",
    "meatwithdrawaldays": "withdrawal_days", "followupdate": "follow_up_date", "followup": "follow_up_date",
    "date": "date", "datetreated": "date", "weightkg": "weight_kg", "measuredon": "measured_on",
    "signsseen": "signs", "signssymptoms": "signs", "symptoms": "signs", "tempc": "temperature", "temperaturec": "temperature",
    "eatingdrinking": "eating", "appetite": "eating", "suspectedproblem": "suspected_problem", "provisionaldiagnosis": "suspected_problem",
    "vetdiagnosis": "vet_diagnosis", "confirmedbyvet": "vet_diagnosis", "confirmeddiagnosis": "vet_diagnosis",
    "actiontaken": "action", "seenby": "seen_by", "observedby": "seen_by",
    "duedate": "due_date", "itemname": "item", "itemnameasonlabel": "item", "name": "item",
    "locationshelf": "location", "packsizeunit": "pack_size", "packsize": "pack_size", "fullpacks": "full_packs",
    "looseqtyunit": "loose_qty", "looseqty": "loose_qty", "batchno": "batch_no", "batch": "batch_no", "expirydate": "expiry",
    "conditiongooddamagedexpired": "condition", "priceperpack": "price_per_pack", "priceperpackkes": "price_per_pack",
    "reorderpacks": "reorder_packs", "reorderlevelpacks": "reorder_packs", "countdate": "count_date", "tasktype": "task_type", "damtag": "dam_tag", "mothertag": "dam_tag",
    "birthweight": "birth_weight", "statusdate": "status_date", "tagid": "tag_id",
}


def _key(h):
    k = "".join(ch for ch in (h or "").lower() if ch.isalnum())
    return ALIASES.get(k, (h or "").strip().lower().replace(" ", "_"))


def _cell(v):
    from datetime import datetime, date
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _xlsx_to_csv(src, dest, kind):
    """Read the first sheet whose header row has the columns we need; write it out as CSV."""
    from openpyxl import load_workbook
    wb = load_workbook(src, data_only=True, read_only=True)
    need = {"title"} if kind == "tasks" else {"item"} if kind == "stock" else {"tag_id"}
    hint = {"observations": "observ", "treatments": "treat", "weights": "weigh", "animals": "animal", "stock": "stock",
            "tasks": "task"}.get(kind, "")
    sheets = sorted(wb.worksheets, key=lambda ws: 0 if hint and hint in ws.title.lower() else 1)
    for ws in sheets:
        rows = list(ws.iter_rows(values_only=True))
        for hi, row in enumerate(rows[:15]):
            keys = [_key(_cell(c)) for c in row]
            if need <= set(keys):
                with open(dest, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(keys)
                    for r in rows[hi + 1:]:
                        vals = [_cell(c) for c in r]
                        if any(vals):
                            w.writerow(vals)
                return
    raise ValueError("no sheet with the expected column headings was found")


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{_key(k): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def _stock_batch(item, r):
    return db.query("SELECT * FROM stock_batches WHERE item_id=? AND ifnull(lower(batch_no),'')=? AND ifnull(lower(location),'')=?",
                    (item["id"], (r.get("batch_no") or "").strip().lower(), (r.get("location") or "").strip().lower()), one=True)


def _treatment_targets(sid, tag, by_key):
    tag = (tag or "").strip()
    if tag.upper() in ("ALL", "ALL ANIMALS", "HERD", "FLOCK"):
        return [a for a in db.query("SELECT id, tag_id FROM animals WHERE species_id=? AND status='active' ORDER BY tag_id", (sid,))]
    tags = [t for t in tag.replace(";", ",").split(",") if t.strip()]
    found = [by_key.get((sid, norm_tag(t))) for t in tags]
    return [a for a in found if a and a["status"] == "active"] if len(tags) > 1 else [a for a in found if a]


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
    elif kind == "observations":
        existing = {(o["animal_id"], (o["observation_date"] or "")[:10], (o["symptoms"] or "").strip().lower())
                    for o in db.query("SELECT animal_id, observation_date, symptoms FROM health_observations")}
        for i, r in enumerate(rows, start=2):
            errs, notes = [], []
            sid = species.get(r.get("species", "").lower())
            targets = _treatment_targets(sid, r.get("tag_id"), by_key) if sid else []
            if not sid:
                errs.append("unknown species")
            elif not targets:
                errs.append("animal not on the site")
            if not _valid_date(r.get("date")):
                errs.append("date must be a date")
            if not (r.get("signs") or r.get("suspected_problem") or r.get("notes")):
                errs.append("write what was seen")
            if not errs:
                new = [a for a in targets if (a["id"], r["date"][:10], (r.get("signs") or "").strip().lower()) not in existing]
                if not new:
                    errs.append("already recorded (skipped)")
                elif len(targets) > 1:
                    notes.append(f"applies to {len(new)} animals")
            out.append(dict(line=i, data=r, errors=errs, notes=notes))
    elif kind == "stock":
        from blueprints import stock as st
        seen = set()
        for i, r in enumerate(rows, start=2):
            errs, notes = [], []
            name = (r.get("item") or "").strip()
            size, unit = st.parse_amount(r.get("pack_size"))
            size_b, base = st.to_base(size, unit)
            if not name:
                errs.append("missing item name")
            if not size_b:
                errs.append("pack size needs a number and unit, e.g. 100 ml")
            if not _valid_date(r.get("count_date")):
                errs.append("count_date must be a date")
            full = _f(r.get("full_packs")) or 0
            loose_amt, loose_unit = st.parse_amount(r.get("loose_qty"))
            loose_b, lbase = st.to_base(loose_amt, loose_unit or unit) if loose_amt else (0, base)
            if loose_amt and lbase != base:
                errs.append("loose amount unit does not match pack unit")
            it = st.find_item(name) if name else None
            if it and base and it["unit"] != base:
                errs.append(f"item already exists in {it['unit']}, this row is in {base}")
            if r.get("expiry") and not st.parse_expiry(r["expiry"]):
                errs.append("expiry not understood (use MM/YY)")
            key = (name.lower(), (r.get("batch_no") or "").strip().lower(), (r.get("location") or "").strip().lower())
            if key in seen:
                errs.append("same item, batch and location twice in this file")
            if not errs:
                seen.add(key)
                b = _stock_batch(it, r) if it else None
                if b and db.query("SELECT 1 FROM stock_movements WHERE batch_id=? AND move_type IN ('count','adjust') AND move_date=?",
                                  (b["id"], r["count_date"][:10]), one=True):
                    errs.append("already counted on this date (skipped)")
                else:
                    qty = full * size_b + (loose_b or 0)
                    notes.append(f"{qty:g} {base}" + (" (new item)" if not it else ""))
                    if not r.get("price_per_pack") and not (it and it["price_per_pack"]):
                        notes.append("no price yet")
            out.append(dict(line=i, data=r, errors=errs, notes=notes))
    elif kind == "treatments":
        meds = {m["name"].strip().lower() for m in db.query("SELECT name FROM medicines")}
        done = {(t["animal_id"], (t["start_date"] or "")[:10], (t["medicine_name"] or t["mname"] or t["treatment_type"] or "").lower())
                for t in db.query("SELECT t.animal_id, t.start_date, t.medicine_name, t.treatment_type, m.name AS mname "
                                  "FROM treatments t LEFT JOIN medicines m ON m.id=t.medicine_id")}
        for i, r in enumerate(rows, start=2):
            errs, notes = [], []
            sid = species.get(r.get("species", "").lower())
            targets = _treatment_targets(sid, r.get("tag_id"), by_key) if sid else []
            if not sid:
                errs.append("unknown species")
            elif not targets:
                errs.append("animal not on the site")
            if not _valid_date(r.get("date")):
                errs.append("date must be a date (YYYY-MM-DD)")
            what = (r.get("medicine") or r.get("treatment_type") or "").strip()
            if not what:
                errs.append("enter a treatment or a medicine")
            if r.get("follow_up_date") and not _valid_date(r["follow_up_date"]):
                errs.append("follow-up date must be a date")
            if not errs:
                new = [a for a in targets if (a["id"], r["date"][:10], what.lower()) not in done]
                if not new:
                    errs.append("already recorded (skipped)")
                else:
                    if len(targets) > 1:
                        notes.append(f"applies to {len(new)} animals")
                    if len(new) < len(targets):
                        notes.append(f"{len(targets) - len(new)} already recorded, skipped")
                    if r.get("medicine") and r["medicine"].strip().lower() not in meds and not r.get("withdrawal_days"):
                        notes.append("medicine not in catalog: no automatic withdrawal date")
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
    try:
        if file.filename.lower().endswith((".xlsx", ".xlsm")):
            tmp = path + ".xlsx"
            file.save(tmp)
            try:
                _xlsx_to_csv(tmp, path, kind)
            finally:
                os.remove(tmp)
        else:
            file.save(path)
        rows = _check(kind, _read(path))
    except Exception as e:  # malformed file
        if os.path.exists(path):
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
    elif kind == "observations":
        species, by_key = _lookups()
        existing = {(o["animal_id"], (o["observation_date"] or "")[:10], (o["symptoms"] or "").strip().lower())
                    for o in db.query("SELECT animal_id, observation_date, symptoms FROM health_observations")}
        for row in checked:
            if not row["ok"]:
                continue
            r = row["data"]
            sid = species[r["species"].lower()]
            extra = []
            if r.get("temperature"):
                extra.append(f"Temp {r['temperature']} °C")
            if r.get("eating"):
                extra.append(f"Eating: {r['eating']}")
            if r.get("action"):
                extra.append(f"Action: {r['action']}")
            if r.get("seen_by"):
                extra.append(f"Seen by: {r['seen_by']}")
            if r.get("notes"):
                extra.append(r["notes"])
            for a in _treatment_targets(sid, r.get("tag_id"), by_key):
                key = (a["id"], r["date"][:10], (r.get("signs") or "").strip().lower())
                if key in existing:
                    continue
                new_id = db.execute(
                    "INSERT INTO health_observations (animal_id, observation_date, symptoms, provisional_diagnosis, confirmed_diagnosis, "
                    "notes, created_by) VALUES (?,?,?,?,?,?,?)",
                    (a["id"], r["date"][:10], r.get("signs") or None, r.get("suspected_problem") or None,
                     r.get("vet_diagnosis") or None, "; ".join(extra) or None, uid))
                existing.add(key)
                db.audit(g.user, "import", "health_observation", new_id, f"Imported observation for {r['species']} {a['tag_id']} on {r['date']}")
                done += 1
    elif kind == "stock":
        from blueprints import stock as st
        for row in checked:
            if not row["ok"]:
                continue
            r = row["data"]
            name = r["item"].strip()
            size, unit = st.parse_amount(r.get("pack_size"))
            size_b, base = st.to_base(size, unit)
            price = _f(r.get("price_per_pack"))
            reorder = _f(r.get("reorder_packs"))
            it = st.find_item(name)
            if not it:
                iid = db.execute("INSERT INTO stock_items (name, category, pack_size, unit, price_per_pack, reorder_level, notes) VALUES (?,?,?,?,?,?,?)",
                                 (name, (r.get("category") or "medicine").lower(), size_b, base, price,
                                  (reorder if reorder is not None else 1) * size_b, None))
                med = db.query("SELECT id FROM medicines WHERE lower(name)=lower(?)", (name,), one=True)
                if med:
                    db.execute("UPDATE stock_items SET medicine_id=? WHERE id=?", (med["id"], iid))
                it = db.query("SELECT * FROM stock_items WHERE id=?", (iid,), one=True)
            else:
                if price is not None:
                    db.execute("UPDATE stock_items SET price_per_pack=? WHERE id=?", (price, it["id"]))
                if reorder is not None:
                    db.execute("UPDATE stock_items SET reorder_level=? WHERE id=?", (reorder * it["pack_size"], it["id"]))
                it = db.query("SELECT * FROM stock_items WHERE id=?", (it["id"],), one=True)
            loose_amt, loose_unit = st.parse_amount(r.get("loose_qty"))
            loose_b = st.to_base(loose_amt, loose_unit or unit)[0] if loose_amt else 0
            qty = (_f(r.get("full_packs")) or 0) * it["pack_size"] + (loose_b or 0)
            b = _stock_batch(it, r)
            when = r["count_date"][:10]
            if b:
                diff = qty - b["qty"]
                db.execute("UPDATE stock_batches SET qty=?, expiry=?, condition=?, price_per_pack=? WHERE id=?",
                           (qty, st.parse_expiry(r.get("expiry")) or b["expiry"], r.get("condition") or b["condition"],
                            price if price is not None else b["price_per_pack"], b["id"]))
                b = db.query("SELECT * FROM stock_batches WHERE id=?", (b["id"],), one=True)
                db.execute("INSERT INTO stock_movements (item_id, batch_id, move_date, move_type, qty, unit_cost, notes, created_by) VALUES (?,?,?,?,?,?,?,?)",
                           (it["id"], b["id"], when, "adjust", diff, st.unit_cost(it, b), f"Stock count: {qty:g} {it['unit']}", uid))
            else:
                bid = db.execute("INSERT INTO stock_batches (item_id, batch_no, expiry, location, condition, qty, price_per_pack) VALUES (?,?,?,?,?,?,?)",
                                 (it["id"], r.get("batch_no") or None, st.parse_expiry(r.get("expiry")), r.get("location") or None,
                                  r.get("condition") or None, qty, price))
                b = db.query("SELECT * FROM stock_batches WHERE id=?", (bid,), one=True)
                db.execute("INSERT INTO stock_movements (item_id, batch_id, move_date, move_type, qty, unit_cost, notes, created_by) VALUES (?,?,?,?,?,?,?,?)",
                           (it["id"], bid, when, "count", qty, st.unit_cost(it, b), r.get("notes") or "Opening stock count", uid))
            db.audit(g.user, "import", "stock_item", it["id"], f"Stock count {name}: {qty:g} {it['unit']}")
            done += 1
    elif kind == "treatments":
        from datetime import timedelta
        species, by_key = _lookups()
        meds = {m["name"].strip().lower(): m for m in db.query("SELECT * FROM medicines")}
        stock_msgs = set()
        seen_t = {(t["animal_id"], (t["start_date"] or "")[:10], (t["medicine_name"] or t["mname"] or t["treatment_type"] or "").lower())
                for t in db.query("SELECT t.animal_id, t.start_date, t.medicine_name, t.treatment_type, m.name AS mname "
                                  "FROM treatments t LEFT JOIN medicines m ON m.id=t.medicine_id")}
        for row in checked:
            if not row["ok"]:
                continue
            r = row["data"]
            sid = species[r["species"].lower()]
            what = (r.get("medicine") or r.get("treatment_type") or "").strip()
            med = meds.get((r.get("medicine") or "").strip().lower())
            start = db.parse_date(r["date"])
            wd_days = int(_f(r.get("withdrawal_days")) or 0) if r.get("withdrawal_days") else None
            meat_wd = milk_wd = None
            if wd_days:
                meat_wd = (start + timedelta(days=wd_days)).isoformat()
            elif med:
                if med["default_meat_withdrawal_days"]:
                    meat_wd = (start + timedelta(days=med["default_meat_withdrawal_days"])).isoformat()
                if med["default_milk_withdrawal_days"]:
                    milk_wd = (start + timedelta(days=med["default_milk_withdrawal_days"])).isoformat()
            targets = [a for a in _treatment_targets(sid, r.get("tag_id"), by_key)
                       if (a["id"], r["date"][:10], what.lower()) not in seen_t]
            for a in targets:
                obs_id = None
                tnotes = "; ".join(x for x in [f"Reason: {r['reason']}" if r.get("reason") else "", r.get("notes") or ""] if x) or None
                new_id = db.execute(
                    "INSERT INTO treatments (animal_id, health_observation_id, treatment_type, medicine_id, medicine_name, dose, "
                    "dose_unit, route, start_date, administered_by, veterinarian, meat_withdrawal_end, milk_withdrawal_end, "
                    "follow_up_date, result, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a["id"], obs_id, r.get("treatment_type") or None, med["id"] if med else None,
                     None if med else (r.get("medicine") or None), r.get("dose") or None, r.get("dose_unit") or None,
                     r.get("route") or None, r["date"][:10], r.get("given_by") or g.user["full_name"], r.get("vet") or None,
                     meat_wd, milk_wd, r.get("follow_up_date") or None, r.get("result") or None, tnotes, uid))
                seen_t.add((a["id"], r["date"][:10], what.lower()))
                if r.get("medicine"):
                    from blueprints.stock import deduct_for_treatment
                    msg = deduct_for_treatment(new_id, r["medicine"], r.get("dose"), r.get("dose_unit"), r["date"][:10], g.user)
                    if msg:
                        stock_msgs.add(msg)
                db.audit(g.user, "import", "treatment", new_id, f"Imported treatment {what} for {r['species']} {a['tag_id']} on {r['date']}")
            if r.get("follow_up_date") and targets:
                label = f"{r['species']} {targets[0]['tag_id']}" if len(targets) == 1 else \
                    f"{len(targets)} {r['species'].lower()}{'' if r['species'].lower() == 'sheep' else 's'}"
                db.execute("INSERT INTO tasks (task_type, title, description, due_date, priority, created_by) VALUES (?,?,?,?,?,?)",
                           ("follow_up", f"Follow-up after {what}: {label} ({r['date'][:10]})",
                            f"Treatment given {r['date'][:10]}. {r.get('reason') or ''}".strip(), r["follow_up_date"], "normal", uid))
            done += len(targets)
        for msg in sorted(stock_msgs):
            flash(msg, "error")
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
    skipped = sum(1 for r in checked if not r["ok"]) if kind in ("treatments", "observations") else len(checked) - done
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
