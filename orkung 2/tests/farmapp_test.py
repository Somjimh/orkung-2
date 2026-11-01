"""Farm app: routines, my jobs, confirm, spray, report, language, permissions.

Runs in-process with Flask's test client against a *copy* of a database
(default: instance/orkung.db). Usage:

    python3 tests/farmapp_test.py [path/to/copy-from.db] [--keep]

--keep prints the path of the test database so it can be looked at afterwards.
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
src = args[0] if args else os.path.join(ROOT, "instance", "orkung.db")
tmpdir = tempfile.mkdtemp(prefix="orkung-farmapp-test-")
dbpath = os.path.join(tmpdir, "orkung.db")
shutil.copy2(src, dbpath)
os.environ["ORKUNG_DB"] = dbpath

from app import create_app  # noqa: E402
from config import Config  # noqa: E402


class TestConfig(Config):
    DATABASE = dbpath
    TESTING = True


app = create_app(TestConfig)
FAILS = []
TODAY = date.today()
T = TODAY.isoformat()
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


def q(sql, a=()):
    c = sqlite3.connect(dbpath)
    c.row_factory = sqlite3.Row
    r = c.execute(sql, a).fetchall()
    c.close()
    return r


def client(user, pw):
    c = app.test_client()
    r = c.post("/login", data={"username": user, "password": pw}, follow_redirects=True)
    check(f"login {user}", r.status_code == 200, r.status_code)
    return c


def text(r):
    return r.get_data(as_text=True)


def photo():
    return (io.BytesIO(PNG), "load.png")


# ---------------------------------------------------------------------------
# 1. Tables, staff, blocks, logins
# ---------------------------------------------------------------------------
for tname in ("routines", "job_tasks", "day_confirmations", "spray_records"):
    check(f"table {tname} created", bool(q("SELECT name FROM sqlite_master WHERE name=?", (tname,))))
check("store items can be marked harmful to bees", "bee_harmful" in {r["name"] for r in q("PRAGMA table_info(stock_items)")})

admin = client("admin", "Admin#2026")
manager = client("manager", "Manager#2026")
viewer = client("viewer", "Viewer#2026")
vet = client("vet", "Vet#2026")
for name, pay in (("Godfrey Ouma", 26000), ("Kajona Malaika", 12000), ("Kapuo", 15000), ("Prince", 14000),
                  ("Muli", 14000), ("Ali Juma", 12000)):
    manager.post("/work/staff", data={"name": name, "pay_type": "monthly", "monthly_salary": str(pay), "start_date": "2026-08-01"})
W = {r["name"].split()[0]: r for r in q("SELECT * FROM workers")}
check("six staff on the register", all(k in W for k in ("Godfrey", "Kajona", "Kapuo", "Prince", "Muli", "Ali")))
for code, nm, org in (("LUC-A", "Lucerne A", ""), ("LUC-B", "Lucerne B", ""), ("B995", "Bananas plot 995", "on")):
    manager.post("/crops/blocks/add", data={"code": code, "name": nm, "area_acres": "1", "organic": org})
B = {r["code"]: r for r in q("SELECT * FROM crop_blocks")}
for user, wkey in (("godfrey", "Godfrey"), ("prince", "Prince"), ("muli", "Muli")):
    admin.post("/admin/users/add", data={"username": user, "password": "Farm#2026", "full_name": W[wkey]["name"],
                                         "role": "worker", "worker_id": W[wkey]["id"]})
admin.post("/admin/users/add", data={"username": "farmphone", "password": "Farm#2026", "full_name": "Farm phone", "role": "worker"})

# ---------------------------------------------------------------------------
# 2. Login lands on the farm app; language
# ---------------------------------------------------------------------------
c = app.test_client()
r = c.post("/login", data={"username": "manager", "password": "Manager#2026"})
check("login goes to the farm app", r.status_code == 302 and r.headers["Location"].endswith("/farm"))
r = manager.get("/farm")
check("home shows the area tiles", r.status_code == 200 and "Livestock" in text(r) and "Apiary" in text(r) and "Spraying" in text(r))
check("home offers to set the routine jobs", "Set the routine jobs" in text(r))
prince = client("prince", "Farm#2026")
r = prince.get("/farm", follow_redirects=True)
check("a linked worker goes straight to their jobs, in Kiswahili", "Kazi zangu leo" in text(r), text(r)[:300])
prince.get("/farm/lang/en?next=/farm/jobs")
check("language switch to English sticks", "My jobs today" in text(prince.get("/farm/jobs")))
r = prince.get("/farm/lang/sw?next=https://evil.example/")
check("language switch never redirects off the site", r.headers["Location"].endswith("/farm"))
r = vet.get("/farm")
check("vet is sent to the full records instead", r.status_code == 302 and r.headers["Location"].endswith("/"))
check("vet can't open farm pages", vet.get("/farm/jobs").status_code == 403)

# ---------------------------------------------------------------------------
# 3. Routines
# ---------------------------------------------------------------------------
r = prince.post("/farm/routines", data={"action": "suggested"})
check("a worker cannot set routines", r.status_code == 403)
r = manager.post("/farm/routines", data={"action": "suggested"}, follow_redirects=True)
rs = q("SELECT r.*, w.name FROM routines r JOIN workers w ON w.id=r.worker_id")
check("suggested routine jobs loaded for the staff", len(rs) == 10, len(rs))
manager.post("/farm/routines", data={"action": "suggested"})
check("loading again adds nothing twice", len(q("SELECT * FROM routines")) == 10)
luc = [x for x in rs if "Lucerne A" in x["title_en"]][0]
check("lucerne is every 14 days and asks for the weight", luc["frequency"] == "every" and luc["every_days"] == 14 and luc["needs_weight"] == 1)
api = [x for x in rs if x["title_en"].startswith("Apiary: inspect")][0]
check("apiary jobs start when the hives arrive", api["start_date"] == "2026-11-15" and api["needs_photo"] == 1)
kaj = [x for x in rs if x["name"].startswith("Kajona")][0]
check("Kajona has Sundays off", kaj["days"] == "012345")
manager.post("/farm/routines", data={"action": "add", "worker_id": W["Prince"]["id"], "area": "bananas",
                                     "title_en": "Desucker the bananas", "title_sw": "Ondoa machipukizi ya ndizi",
                                     "frequency": "weekly", "weekday": str(TODAY.weekday()), "start_date": T,
                                     "block_id": B["B995"]["id"]})
check("manager adds a routine job", q("SELECT 1 FROM routines WHERE title_en='Desucker the bananas'"))

# ---------------------------------------------------------------------------
# 4. My jobs
# ---------------------------------------------------------------------------
prince.get("/farm/lang/sw?next=/farm/jobs")
r = prince.get("/farm/jobs")
t = text(r)
check("Prince sees his lucerne jobs in Kiswahili", "Kata na upime Lusina A" in t and "Ondoa machipukizi ya ndizi" in t)
check("Prince does not see others' jobs", "Lisha na nywesha ng" not in t)
jobs = {x["title_en"]: x for x in q("SELECT * FROM job_tasks WHERE worker_id=? AND task_date=?", (W["Prince"]["id"], T))}
la = jobs["Cut and weigh Lucerne A"]
r = prince.post(f"/farm/jobs/{la['id']}/done", data={"d": T}, follow_redirects=True)
check("lucerne without a weight is refused", q("SELECT status FROM job_tasks WHERE id=?", (la["id"],))[0]["status"] == "open")
r = prince.post(f"/farm/jobs/{la['id']}/done", data={"d": T, "qty": "410"}, follow_redirects=True)
check("lucerne without a photo is refused", q("SELECT status FROM job_tasks WHERE id=?", (la["id"],))[0]["status"] == "open")
r = prince.post(f"/farm/jobs/{la['id']}/done", data={"d": T, "qty": "410", "photo": photo()},
                content_type="multipart/form-data", follow_redirects=True)
row = q("SELECT * FROM job_tasks WHERE id=?", (la["id"],))[0]
check("lucerne done with weight and photo", row["status"] == "done" and row["qty"] == 410 and row["photo"])
check("photo saved beside the database and served", os.path.exists(os.path.join(tmpdir, "farm-photos", row["photo"]))
      and prince.get(f"/farm/photo/{row['photo']}").status_code == 200)
check("photos need a login", app.test_client().get(f"/farm/photo/{row['photo']}").status_code in (302, 401))
lb = jobs["Cut and weigh Lucerne B"]
prince.post(f"/farm/jobs/{lb['id']}/not-done", data={"d": T, "note": "Mvua"})
check("job marked not done with a reason", q("SELECT status, note FROM job_tasks WHERE id=?", (lb["id"],))[0]["note"] == "Mvua")
kj = q("SELECT * FROM job_tasks WHERE worker_id=? AND task_date=?", (W["Kajona"]["id"], T))
if kj:
    r = prince.post(f"/farm/jobs/{kj[0]['id']}/done", data={"d": T})
    check("Prince cannot tick Kajona's job", r.status_code == 403)
prince.post("/farm/jobs/add", data={"d": T, "title": "Fixed the fence", "area": "general", "worker_id": W["Godfrey"]["id"]})
ex = q("SELECT * FROM job_tasks WHERE title_en='Fixed the fence'")
check("extra job is recorded as Prince's own (even if another name is sent)", ex and ex[0]["worker_id"] == W["Prince"]["id"]
      and ex[0]["status"] == "done")

phone = client("farmphone", "Farm#2026")
r = phone.get(f"/farm/jobs?w={W['Kajona']['id']}")
check("farm phone chooses the person", "Chagua mtu" in text(r) or "Choose the person" in text(r))
if TODAY.weekday() != 6:
    kjob = q("SELECT * FROM job_tasks WHERE worker_id=? AND task_date=?", (W["Kajona"]["id"], T))[0]
    phone.post(f"/farm/jobs/{kjob['id']}/done", data={"d": T})
    check("farm phone ticks Kajona's job", q("SELECT status FROM job_tasks WHERE id=?", (kjob["id"],))[0]["status"] == "done")

# every-14-days: an unfinished one carries on and shows as overdue
yday = (TODAY - timedelta(days=3)).isoformat()
sqlite3.connect(dbpath).execute("UPDATE job_tasks SET task_date=? WHERE id=?", (yday, lb["id"])).connection.commit()
sqlite3.connect(dbpath).execute("UPDATE job_tasks SET status='open', note=NULL WHERE id=?", (lb["id"],)).connection.commit()
r = prince.get("/farm/jobs")
check("an unfinished 14-day job carries over as overdue", "Imechelewa · 3 siku" in text(r))
check("...and is not duplicated", len(q("SELECT * FROM job_tasks WHERE routine_id=? AND status='open'", (lb["routine_id"],))) == 1)
prince.post(f"/farm/jobs/{lb['id']}/done", data={"d": T, "qty": "365", "photo": photo()}, content_type="multipart/form-data")
row = q("SELECT * FROM job_tasks WHERE id=?", (lb["id"],))[0]
check("done today: it counts on today", row["status"] == "done" and row["task_date"] == T)
nxt = (TODAY + timedelta(days=14)).isoformat()
with app.test_request_context():
    from blueprints import farmapp as fa
    import db as _db
    r_ = _db.query("SELECT * FROM routines WHERE id=?", (lb["routine_id"],), one=True)
    check("next lucerne cut is due in 14 days, not before", (not fa._routine_due(r_, TODAY + timedelta(days=13)))
          and fa._routine_due(r_, TODAY + timedelta(days=14)))

# ---------------------------------------------------------------------------
# 5. Spraying
# ---------------------------------------------------------------------------
admin.post("/stock/add", data={"name": "Test Dursban", "pack_size": "1 L", "category": "chemical", "price_per_pack": "2500",
                               "reorder_packs": "1", "phi_days": "14", "bee_harmful": "on"})
chem = q("SELECT * FROM stock_items WHERE name='Test Dursban'")[0]
check("store item marked harmful to bees", chem["bee_harmful"] == 1)
admin.post(f"/stock/{chem['id']}/move", data={"kind": "purchase", "packs": "1", "price_per_pack": "2500", "move_date": T})
r = prince.post("/farm/spray", data={"spray_date": T, "block_id": B["LUC-A"]["id"], "item": chem["id"], "amount": "40",
                                     "unit": "ml", "water_l": "20", "pest": "Aphids"}, follow_redirects=True)
check("bee-harmful spray refused without evening/keeper tick", not q("SELECT 1 FROM spray_records"))
r = prince.post("/farm/spray", data={"spray_date": T, "block_id": B["B995"]["id"], "item": chem["id"], "amount": "40",
                                     "unit": "ml", "bee_ack": "on"}, follow_redirects=True)
check("non-organic product on an organic block refused", not q("SELECT 1 FROM spray_records"))
r = prince.post("/farm/spray", data={"spray_date": T, "spray_time": "17:30", "block_id": B["LUC-A"]["id"], "item": chem["id"],
                                     "amount": "40", "unit": "ml", "water_l": "20", "pest": "Aphids", "ppe": "on",
                                     "bee_ack": "on", "photo": photo()}, content_type="multipart/form-data", follow_redirects=True)
s = q("SELECT * FROM spray_records")
safe = (TODAY + timedelta(days=14)).isoformat()
check("spray saved with everything", s and s[0]["water_l"] == 20 and s[0]["ppe"] == 1 and s[0]["bee_harmful"] == 1
      and s[0]["safe_date"] == safe and s[0]["photo"] and s[0]["worker_id"] == W["Prince"]["id"])
mv = q("SELECT * FROM stock_movements WHERE item_id=? AND move_type='used'", (chem["id"],))
check("product taken off the store shelf", mv and abs(mv[0]["qty"] + 40) < 1e-9 and mv[0]["block_id"] == B["LUC-A"]["id"])
check("apiary keeper (Muli) gets a job about the spray",
      q("SELECT 1 FROM job_tasks WHERE worker_id=? AND area='apiary' AND title_en LIKE 'Spray on LUC-A%'", (W["Muli"]["id"],)))
r = phone.post("/farm/spray", data={"spray_date": T, "block_id": B["LUC-B"]["id"], "item": "other", "product": "Neem oil",
                                    "phi_days": "1", "amount": "50", "unit": "ml", "worker_id": W["Ali"]["id"]},
               follow_redirects=True)
s2 = q("SELECT * FROM spray_records WHERE product='Neem oil'")
check("product not in the store can still be recorded", s2 and s2[0]["item_id"] is None and s2[0]["worker_id"] == W["Ali"]["id"])
r = viewer.post("/farm/spray", data={"block_id": B["LUC-B"]["id"]})
check("viewer cannot record a spray", r.status_code == 403)
r = manager.get("/farm/spray")
check("spray page lists recent sprays with the safe date", safe in text(r) and "Neem oil" in text(r) and "no gloves/mask" in text(r))

# ---------------------------------------------------------------------------
# 6. Confirm and report
# ---------------------------------------------------------------------------
r = prince.get("/farm/confirm")
check("Prince cannot confirm the day", r.status_code == 403)
godfrey = client("godfrey", "Farm#2026")
r = godfrey.get("/farm", follow_redirects=True)
check("Godfrey (supervisor) sees the home screen with the confirm button", "Thibitisha siku" in text(r) or "Confirm the day" in text(r))
fence = ex[0]
r = godfrey.post("/farm/confirm", data={"d": T, f"m_{fence['id']}": "not_done", "note": "Siku nzuri"}, follow_redirects=True)
check("Godfrey confirms the day", q("SELECT * FROM day_confirmations WHERE work_date=?", (T,)))
check("...and can mark a ticked job as not done", q("SELECT status FROM job_tasks WHERE id=?", (fence["id"],))[0]["status"] == "not_done")
r = manager.get("/farm/report")
t = text(r)
check("report shows who did what", "Prince" in t and "Godfrey Ouma" in t)
check("Kajona is in the report except on Sundays (his day off)", ("Kajona Malaika" in t) == (TODAY.weekday() != 6))
check("report shows the weighed lucerne", "410 kg" in t and "365 kg" in t)
check("report shows the spray and its safe date", "LUC-A" in t and safe in t)
check("report shows who confirmed", "Confirmed by Godfrey Ouma" in t)
check("report needs-you lists the spray waiting period", "Safe to harvest from" in t or "safe to harvest from" in t)
r = manager.get("/farm/report?area=fodder")
check("area view shows only that area", "Lucerne" in text(r) and "Feed and water the cows" not in text(r))
r = prince.get("/farm/report", follow_redirects=True)
check("a linked worker is sent to their own jobs, not the report", "Kazi zangu leo" in text(r) or "My jobs today" in text(r))
r = prince.post(f"/farm/jobs/{la['id']}/reopen", data={"d": T}, follow_redirects=True)
check("after confirmation a worker can't reopen a job", q("SELECT status FROM job_tasks WHERE id=?", (la["id"],))[0]["status"] == "done")

# settings: choose who confirms
manager.post("/farm/routines", data={"action": "settings", "farm_supervisor_worker_id": W["Muli"]["id"],
                                     "farm_apiary_worker_id": W["Muli"]["id"]})
muli = client("muli", "Farm#2026")
check("supervisor can be changed in settings", muli.get("/farm/confirm").status_code == 200
      and godfrey.get("/farm/confirm").status_code == 403)

# ---------------------------------------------------------------------------
# 7. The rest of the site still works
# ---------------------------------------------------------------------------
for path in ("/", "/animals", "/work/day", "/crops", "/stock", "/payroll", "/today", "/farm/routines"):
    rr = manager.get(path)
    check(f"{path} still opens", rr.status_code == 200, rr.status_code)
check("foreign keys intact", not q("PRAGMA foreign_key_check"))

if "--keep" in sys.argv:
    print("TEST DB:", dbpath)
print()
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED:")
    for f_ in FAILS:
        print(" -", f_)
    sys.exit(1)
print("ALL CHECKS PASSED")
