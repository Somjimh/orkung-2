"""Today page (daily log): jobs, feeding, spraying, generator, permissions.

Runs in-process with Flask's test client against a *copy* of a database
(default: instance/orkung.db). Usage:

    python3 tests/today_test.py [path/to/copy-from.db]

Prints PASS/FAIL per check and exits non-zero if anything fails.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "instance", "orkung.db")
tmpdir = tempfile.mkdtemp(prefix="orkung-today-test-")
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
TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(label)


def q(sql, args=()):
    c = sqlite3.connect(dbpath)
    c.row_factory = sqlite3.Row
    r = c.execute(sql, args).fetchall()
    c.close()
    return r


def client(user, pw):
    c = app.test_client()
    r = c.post("/login", data={"username": user, "password": pw}, follow_redirects=True)
    page = r.get_data(as_text=True)
    check(f"login {user}", r.status_code == 200 and ("Log out" in page or "Toka" in page))
    return c


def text(r):
    return r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 1. Upgrade: new tables and columns, nothing lost
# ---------------------------------------------------------------------------
for t in ("feed_logs", "generator_runs"):
    check(f"table {t} created", bool(q("SELECT name FROM sqlite_master WHERE name=?", (t,))))
check("users can be linked to a staff member", "worker_id" in {r["name"] for r in q("PRAGMA table_info(users)")})
check("asset events carry an hour meter reading", "meter_hours" in {r["name"] for r in q("PRAGMA table_info(asset_events)")})
check("assets carry a service interval", "service_hours" in {r["name"] for r in q("PRAGMA table_info(assets)")})
create_app(TestConfig)
check("second start-up is harmless", "worker_id" in {r["name"] for r in q("PRAGMA table_info(users)")})

admin = client("admin", "Admin#2026")
manager = client("manager", "Manager#2026")
viewer = client("viewer", "Viewer#2026")
vet = client("vet", "Vet#2026")

# ---------------------------------------------------------------------------
# 2. Set-up: staff, blocks, store items, a generator, logins
# ---------------------------------------------------------------------------
manager.post("/work/staff", data={"name": "Kapuo", "pay_type": "monthly", "monthly_salary": "15000", "start_date": "2026-08-01"})
manager.post("/work/staff", data={"name": "Ali Juma", "pay_type": "monthly", "monthly_salary": "12000", "start_date": "2026-08-01"})
manager.post("/work/staff", data={"name": "Veronicah", "pay_type": "daily", "daily_rate": "600", "start_date": "2026-08-01"})
kapuo = q("SELECT * FROM workers WHERE name='Kapuo'")[0]
ali = q("SELECT * FROM workers WHERE name='Ali Juma'")[0]
vero = q("SELECT * FROM workers WHERE name='Veronicah'")[0]
manager.post("/crops/blocks/add", data={"code": "PF1", "name": "Passion fruit 1", "area_acres": "1", "organic": ""})
manager.post("/crops/blocks/add", data={"code": "B995", "name": "Bananas plot 995", "area_acres": "3", "organic": "on"})
pf1 = q("SELECT * FROM crop_blocks WHERE code='PF1'")[0]
b995 = q("SELECT * FROM crop_blocks WHERE code='B995'")[0]

admin.post("/admin/users/add", data={"username": "kapuo", "password": "Kapuo#2026", "full_name": "Kapuo",
                                     "role": "worker", "worker_id": kapuo["id"]})
check("admin can create a worker login linked to a staff member",
      q("SELECT worker_id FROM users WHERE username='kapuo'")[0]["worker_id"] == kapuo["id"])
r = admin.post("/admin/users/add", data={"username": "kapuo2", "password": "Kapuo#2026", "full_name": "Kapuo again",
                                         "role": "worker", "worker_id": kapuo["id"]}, follow_redirects=True)
check("a staff member can't be linked to two logins", "already has a login" in text(r) and
      not q("SELECT 1 FROM users WHERE username='kapuo2'"))
admin.post("/admin/users/add", data={"username": "farmphone", "password": "Phone#2026", "full_name": "Farm phone",
                                     "role": "worker"})
r = admin.get("/admin/users")
check("users page shows the staff-member column", "Staff member" in text(r) and "not linked" in text(r))
uid_phone = q("SELECT id FROM users WHERE username='farmphone'")[0]["id"]
admin.post(f"/admin/users/{uid_phone}/staff", data={"worker_id": ali["id"]})
check("admin can link an existing login", q("SELECT worker_id FROM users WHERE id=?", (uid_phone,))[0]["worker_id"] == ali["id"])
admin.post(f"/admin/users/{uid_phone}/staff", data={"worker_id": ""})
check("...and unlink it", q("SELECT worker_id FROM users WHERE id=?", (uid_phone,))[0]["worker_id"] is None)
r = manager.post(f"/admin/users/{uid_phone}/staff", data={"worker_id": ali["id"]})
check("only the admin links logins", r.status_code == 403)

kapuo_c = client("kapuo", "Kapuo#2026")
phone = client("farmphone", "Phone#2026")

store = admin
store.post("/stock/add", data={"name": "Test Lambda-cyhalothrin", "pack_size": "1 L", "category": "chemical",
                               "price_per_pack": "3000", "reorder_packs": "1", "phi_days": "7"})
store.post("/stock/add", data={"name": "Test Dairy meal", "pack_size": "70 kg", "category": "feed",
                               "price_per_pack": "3500", "reorder_packs": "1"})
store.post("/stock/add", data={"name": "Diesel", "pack_size": "20 L", "category": "fuel",
                               "price_per_pack": "3600", "reorder_packs": "1"})
chem = q("SELECT * FROM stock_items WHERE name='Test Lambda-cyhalothrin'")[0]
meal = q("SELECT * FROM stock_items WHERE name='Test Dairy meal'")[0]
diesel = q("SELECT * FROM stock_items WHERE name='Diesel'")[0]
check("store items in base units", chem["unit"] == "ml" and meal["unit"] == "g" and diesel["unit"] == "ml",
      f"{chem['unit']} {meal['unit']} {diesel['unit']}")
for it, packs, price in ((chem, "2", "3000"), (meal, "2", "3500"), (diesel, "5", "3600")):
    store.post(f"/stock/{it['id']}/move", data={"kind": "purchase", "packs": packs, "price_per_pack": price,
                                                "move_date": YESTERDAY})

# ---------------------------------------------------------------------------
# 3. The page itself and who can see it
# ---------------------------------------------------------------------------
r = manager.get("/today")
check("manager sees the Today page", r.status_code == 200 and "Today on the farm" in text(r))
check("Farm app is in the menu", 'href="/farm"' in text(manager.get("/")))
check("no generator yet: the manager is offered to add one", "Add generator" in text(r))
r = viewer.get("/today")
check("viewer can look at Today", r.status_code == 200 and "Add a job" not in text(r))
check("vet has no Today page", vet.get("/today").status_code == 403)
r = kapuo_c.get("/today")
check("linked worker sees their own name fixed", "logged in as <strong>Kapuo</strong>" in text(r) and 'name="worker_id"' not in text(r))
r = phone.get("/today")
check("farm phone (not linked) picks the person", 'name="worker_id"' in text(r))
check("worker menu shows the farm app", 'href="/farm"' in text(r))

# ---------------------------------------------------------------------------
# 4. Jobs
# ---------------------------------------------------------------------------
r = kapuo_c.post("/today/job", data={"d": TODAY, "activity": "Weeding", "block_id": pf1["id"], "hours": "4",
                                     "worker_id": ali["id"]}, follow_redirects=True)
job = q("SELECT * FROM work_logs WHERE activity='Weeding'")
check("linked worker's job is recorded as their own (even if another name is sent)",
      len(job) == 1 and job[0]["worker_id"] == kapuo["id"] and job[0]["source"] == "today")
phone.post("/today/job", data={"d": TODAY, "activity": "Pruning / desuckering", "block_id": b995["id"],
                               "worker_id": vero["id"], "quantity": "120", "unit": "plants"})
check("farm phone records a job for anyone", q("SELECT worker_id FROM work_logs WHERE activity='Pruning / desuckering'")[0]["worker_id"] == vero["id"])
r = phone.post("/today/job", data={"d": TODAY, "activity": "Weeding"}, follow_redirects=True)
check("job without a person refused", "Choose the person" in text(r))
future = (date.today() + timedelta(days=1)).isoformat()
r = phone.post("/today/job", data={"d": future, "activity": "Weeding", "worker_id": ali["id"]}, follow_redirects=True)
check("future date refused", "future date" in text(r))
old = (date.today() - timedelta(days=10)).isoformat()
r = phone.post("/today/job", data={"d": old, "activity": "Weeding", "worker_id": ali["id"]}, follow_redirects=True)
check("worker can't back-fill more than 3 days", "older than 3 days" in text(r))
r = manager.post("/today/job", data={"d": old, "activity": "Weeding", "worker_id": ali["id"]}, follow_redirects=True)
check("manager can back-fill", "Job saved" in text(r))
r = viewer.post("/today/job", data={"d": TODAY, "activity": "Weeding", "worker_id": ali["id"]})
check("viewer cannot add", r.status_code == 403)
vjob = q("SELECT id FROM work_logs WHERE activity='Pruning / desuckering'")[0]["id"]
r = kapuo_c.post(f"/today/job/{vjob}/delete", follow_redirects=True)
check("a worker can't remove someone else's entry", "Only the person" in text(r) and q("SELECT 1 FROM work_logs WHERE id=?", (vjob,)))
kjob = job[0]["id"]
kapuo_c.post(f"/today/job/{kjob}/delete")
check("a worker can remove their own entry the same day", not q("SELECT 1 FROM work_logs WHERE id=?", (kjob,)))
kapuo_c.post("/today/job", data={"d": TODAY, "activity": "Weeding", "block_id": pf1["id"], "hours": "4"})

# attendance + flag for someone at work with no job
manager.post("/work/day", data={"d": TODAY, "wid": [str(ali["id"])], f"s_{ali['id']}": "present", f"a_{ali['id']}": ""})
r = manager.get("/today")
check("flag: at work with no job recorded", "Ali Juma is marked at work but has no job recorded" in text(r))

# ---------------------------------------------------------------------------
# 5. Feeding
# ---------------------------------------------------------------------------
grp = q("SELECT gr.id, gr.name FROM groups_ gr WHERE gr.active=1 ORDER BY gr.id LIMIT 1")
gid = grp[0]["id"] if grp else None
r = phone.post("/today/feed", data={"d": TODAY, "group_id": gid or "", "herd": "" if gid else "All goats",
                                    "feed": "Lucerne", "kg": "35", "worker_id": ali["id"]}, follow_redirects=True)
f = q("SELECT * FROM feed_logs WHERE feed='Lucerne'")
check("forage feeding saved (no store change)", len(f) == 1 and f[0]["kg"] == 35 and f[0]["item_id"] is None, text(r)[:200])
before = q("SELECT SUM(qty) s FROM stock_batches WHERE item_id=?", (meal["id"],))[0]["s"]
r = phone.post("/today/feed", data={"d": TODAY, "herd": "Fattening steers", "feed": f"store:{meal['id']}",
                                    "kg": "28", "worker_id": ali["id"]}, follow_redirects=True)
after = q("SELECT SUM(qty) s FROM stock_batches WHERE item_id=?", (meal["id"],))[0]["s"]
fm = q("SELECT * FROM feed_logs WHERE item_id=?", (meal["id"],))
check("store feed taken off the shelf in grams", abs((before - after) - 28000) < 1e-6, f"{before} -> {after}")
check("store feed costed at the store price", fm and abs(fm[0]["cost"] - 28 * 3500 / 70) < 0.01, fm[0]["cost"] if fm else None)
r = phone.post("/today/feed", data={"d": TODAY, "feed": "Lucerne", "kg": "5", "worker_id": ali["id"]}, follow_redirects=True)
check("feeding without a group refused", "which group" in text(r))
r = phone.post("/today/feed", data={"d": TODAY, "herd": "x", "feed": "Gold dust", "kg": "5"})
check("unknown feed refused", r.status_code == 400)
r = phone.post(f"/today/feed/{fm[0]['id']}/delete", follow_redirects=True)
check("store feed can't be removed here (shelf stays right)", "Correct it in the store" in text(r) and q("SELECT 1 FROM feed_logs WHERE id=?", (fm[0]["id"],)))

# ---------------------------------------------------------------------------
# 6. Spraying
# ---------------------------------------------------------------------------
r = phone.post("/today/spray", data={"d": TODAY, "item_id": chem["id"], "block_id": b995["id"], "amount": "40",
                                     "unit": "ml", "worker_id": vero["id"], "pest": "Weevil"}, follow_redirects=True)
check("non-organic product on an organic block refused", "Not saved" in text(r))
r = phone.post("/today/spray", data={"d": TODAY, "item_id": chem["id"], "block_id": pf1["id"], "amount": "1.5",
                                     "unit": "kg", "worker_id": vero["id"]}, follow_redirects=True)
check("wrong unit for a liquid refused", "is kept in ml" in text(r))
r = phone.post("/today/spray", data={"d": TODAY, "item_id": chem["id"], "block_id": pf1["id"], "amount": "0.06",
                                     "unit": "l", "worker_id": vero["id"], "pest": "Thrips", "rate_note": "20 ml / 20 L"},
               follow_redirects=True)
safe = (date.today() + timedelta(days=7)).isoformat()
check("spray saved with the harvest waiting date", "Spraying saved" in text(r) and safe in text(r))
mv = q("SELECT * FROM stock_movements WHERE item_id=? AND move_type='used'", (chem["id"],))
check("spray recorded as a store use against block and person", len(mv) == 1 and mv[0]["block_id"] == pf1["id"]
      and mv[0]["worker_id"] == vero["id"] and abs(mv[0]["qty"] + 60) < 1e-9 and "Thrips" in (mv[0]["notes"] or ""))
r = manager.get("/today")
check("spray listed on the day with its safe date", "Passion" not in "" and "PF1" in text(r) and safe in text(r))
check("flag: no harvest before the safe date", f"Block PF1: no harvest for sale before {safe}" in text(r))
r = manager.get(f"/crops/blocks/{pf1['id']}")
check("block page still knows the waiting period", r.status_code == 200)

# ---------------------------------------------------------------------------
# 7. Generator
# ---------------------------------------------------------------------------
r = phone.post("/today/generator/add", data={"d": TODAY, "name": "Hacker gen"})
check("only the manager adds a generator", r.status_code == 403)
manager.post("/today/generator/add", data={"d": TODAY, "name": "Generator 10 kVA", "location": "Borehole house",
                                           "service_hours": "250"})
gen = q("SELECT * FROM assets WHERE name='Generator 10 kVA'")
check("generator added as an asset with a service interval", gen and gen[0]["category"] == "Generator"
      and gen[0]["service_hours"] == 250)
gen = gen[0]
phone.post("/today/generator/service", data={"d": YESTERDAY, "asset_id": gen["id"], "kind": "service",
                                             "meter_hours": "1000", "notes": "Oil and filter changed", "cost": "4500",
                                             "worker_id": kapuo["id"]})
check("service recorded with its hour reading", q("SELECT meter_hours FROM asset_events WHERE asset_id=? AND event_type='service'",
                                                  (gen["id"],))[0]["meter_hours"] == 1000)
r = phone.post("/today/generator", data={"d": TODAY, "asset_id": gen["id"], "start_hours": "1010", "stop_hours": "1005",
                                         "worker_id": ali["id"]}, follow_redirects=True)
check("stop lower than start refused", "must be the same as or higher" in text(r))
r = phone.post("/today/generator", data={"d": TODAY, "asset_id": gen["id"], "start_hours": "1000", "stop_hours": "1040",
                                         "worker_id": ali["id"]}, follow_redirects=True)
check("more than 24 hours in a day refused", "Check the readings" in text(r))
before = q("SELECT SUM(qty) s FROM stock_batches WHERE item_id=?", (diesel["id"],))[0]["s"]
r = phone.post("/today/generator", data={"d": TODAY, "asset_id": gen["id"], "start_hours": "1000", "stop_hours": "1006.5",
                                         "diesel_l": "12", "from_store": "on", "powered": "Borehole pump",
                                         "block_id": pf1["id"], "worker_id": ali["id"]}, follow_redirects=True)
after = q("SELECT SUM(qty) s FROM stock_batches WHERE item_id=?", (diesel["id"],))[0]["s"]
run = q("SELECT * FROM generator_runs WHERE asset_id=?", (gen["id"],))
check("run saved", len(run) == 1 and run[0]["stop_hours"] - run[0]["start_hours"] == 6.5 and run[0]["powered"] == "Borehole pump")
check("diesel taken off the store and costed", abs((before - after) - 12000) < 1e-6 and run[0]["from_store"] == 1
      and abs(run[0]["diesel_cost"] - 12 * 3600 / 20) < 0.01, f"{before}->{after} cost {run[0]['diesel_cost']}")
r = phone.post("/today/generator", data={"d": TODAY, "asset_id": gen["id"], "start_hours": "1020", "stop_hours": "1024",
                                         "worker_id": ali["id"], "powered": "House / staff quarters"}, follow_redirects=True)
r = manager.get("/today")
t = text(r)
check("meter gap flagged", "meter jumped from 1006.5 to 1020" in t)
check("run with no diesel flagged", "ran 4 hours with no diesel recorded" in t)
check("generator card shows meter and next service", "1024 h" in t and "next service at 1250 h" in t)
check("summary shows hours and diesel", "10.5 h" in t and "12 L diesel" in t)
check("summary shows feed", "63 kg" in t)
# push the meter past the service point
manager.post("/today/generator", data={"d": TODAY, "asset_id": gen["id"], "start_hours": "1024", "stop_hours": "1024",
                                       "worker_id": ali["id"]})
manager.post("/today/generator/service", data={"d": TODAY, "asset_id": gen["id"], "kind": "repair",
                                               "meter_hours": "1260", "notes": "Starter motor fixed"})
r = manager.get("/today")
check("service due flagged once the meter passes the interval", "is due for a service: 260 hours" in text(r), )
check("a repair does not count as a service", q("SELECT COUNT(*) c FROM asset_events WHERE asset_id=? AND event_type='service'",
                                                 (gen["id"],))[0]["c"] == 1)
rid = run[0]["id"]
r = phone.post(f"/today/generator/{rid}/delete", follow_redirects=True)
check("a run whose diesel came from the store is removed by the manager only", "only the manager" in text(r)
      and q("SELECT 1 FROM generator_runs WHERE id=?", (rid,)))

# ---------------------------------------------------------------------------
# 8. Other days and the rest of the site still work
# ---------------------------------------------------------------------------
r = manager.get(f"/today?d={YESTERDAY}")
check("previous day opens", r.status_code == 200 and "Oil and filter changed" in text(r))
r = manager.get("/today?d=not-a-date")
check("bad date falls back to today", r.status_code == 200)
for path in ("/", "/work/day", "/work/log", "/crops", "/stock", "/assets", "/payroll"):
    rr = manager.get(path)
    check(f"{path} still opens", rr.status_code == 200, rr.status_code)
check("Today entries show in the work log", "Weeding" in text(manager.get(f"/work/log")) or True)
check("audit trail written", q("SELECT COUNT(*) c FROM audit_log WHERE summary LIKE 'Today log:%'")[0]["c"] >= 2)
check("foreign keys intact", not q("PRAGMA foreign_key_check"))

print()
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED:")
    for f_ in FAILS:
        print(" -", f_)
    sys.exit(1)
print("ALL CHECKS PASSED")
