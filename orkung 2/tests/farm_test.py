"""Crops, daily work, payroll, store, assets and milestones: end-to-end checks.

Runs entirely in-process with Flask's test client against a *copy* of a
database (default: instance/orkung.db), so it also proves the start-up upgrade
of an older database. Usage:

    python3 tests/farm_test.py [path/to/copy-from.db]

Prints PASS/FAIL per check and exits non-zero if anything fails.
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "instance", "orkung.db")
tmpdir = tempfile.mkdtemp(prefix="orkung-farm-test-")
dbpath = os.path.join(tmpdir, "orkung.db")
shutil.copy2(src, dbpath)
before = sqlite3.connect(dbpath)
users_before = before.execute("SELECT id, username, password_hash, role FROM users ORDER BY id").fetchall()
animals_before = before.execute("SELECT COUNT(*) FROM animals").fetchone()[0]
before.close()
os.environ["ORKUNG_DB"] = dbpath

from app import create_app  # noqa: E402
from config import Config  # noqa: E402


class TestConfig(Config):
    DATABASE = dbpath
    TESTING = True


app = create_app(TestConfig)
FAILS = []


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


# ---------------------------------------------------------------------------
# 1. Upgrade of the existing database
# ---------------------------------------------------------------------------
users_after = [tuple(r) for r in q("SELECT id, username, password_hash, role FROM users ORDER BY id")]
check("existing users kept exactly (ids, usernames, passwords, roles)", users_after == [tuple(u) for u in users_before])
check("existing animals untouched", q("SELECT COUNT(*) c FROM animals")[0]["c"] == animals_before)
check("users table now allows the storekeeper role", "storekeeper" in q("SELECT sql FROM sqlite_master WHERE name='users'")[0]["sql"])
backups = [f for f in os.listdir(tmpdir) if ".before-roles-" in f]
check("a full backup copy was written before the users table changed", len(backups) == 1)
check("foreign keys intact after upgrade", not q("PRAGMA foreign_key_check"))
for t in ("crop_blocks", "plantings", "workers", "attendance", "work_logs", "harvests", "crop_sales", "pay_advances",
          "pay_runs", "pay_lines", "assets", "asset_events", "milestones"):
    check(f"table {t} created", bool(q("SELECT name FROM sqlite_master WHERE name=?", (t,))))
cols = {r["name"] for r in q("PRAGMA table_info(stock_movements)")}
check("store movements carry block, planting and person", {"block_id", "planting_id", "worker_id"} <= cols)

# second start must not upgrade again
app2 = create_app(TestConfig)
check("restart does not make a second backup", len([f for f in os.listdir(tmpdir) if ".before-roles-" in f]) == 1)


def client(user, pw):
    c = app.test_client()
    r = c.post("/login", data={"username": user, "password": pw}, follow_redirects=True)
    check(f"login {user}", r.status_code == 200 and "Log out" in r.get_data(as_text=True))
    return c


admin = client("admin", "Admin#2026")
manager = client("manager", "Manager#2026")
worker = client("worker", "Worker#2026")
viewer = client("viewer", "Viewer#2026")
vet = client("vet", "Vet#2026")

r = admin.post("/admin/users/add", data={"username": "store", "password": "Store#2026", "full_name": "Store Keeper",
                                         "role": "storekeeper"}, follow_redirects=True)
check("admin can create a storekeeper login", q("SELECT role FROM users WHERE username='store'")[0]["role"] == "storekeeper"
      if q("SELECT role FROM users WHERE username='store'") else False, r.status_code)
store = client("store", "Store#2026")

# ---------------------------------------------------------------------------
# 2. Blocks, plantings, staff
# ---------------------------------------------------------------------------
manager.post("/crops/blocks/add", data={"code": "HAB-A", "name": "Habanero half-acre A", "area_acres": "0.5",
                                        "water_source": "Borehole", "organic": ""})
manager.post("/crops/blocks/add", data={"code": "D1", "name": "Block D bananas", "area_acres": "2", "organic": "on"})
hab = q("SELECT * FROM crop_blocks WHERE code='HAB-A'")[0]
d1 = q("SELECT * FROM crop_blocks WHERE code='D1'")[0]
check("blocks saved (organic flag kept)", hab["organic"] == 0 and d1["organic"] == 1)
r = manager.post("/crops/blocks/add", data={"code": "hab-a"}, follow_redirects=True)
check("duplicate block code refused", "already exists" in r.get_data(as_text=True))
manager.post("/crops/plantings/add", data={"block_id": hab["id"], "crop": "Habanero", "planted_date": "2026-08-01",
                                           "plant_count": "2000"})
manager.post("/crops/plantings/add", data={"block_id": d1["id"], "crop": "Banana", "planted_date": "2026-08-15",
                                           "variety": "Grand Nain"})
check("plantings saved", len(q("SELECT * FROM plantings")) == 2)
r = worker.post("/crops/blocks/add", data={"code": "X"})
check("farm worker cannot add blocks", r.status_code == 403)

manager.post("/work/staff", data={"name": "Asha Monthly", "pay_type": "monthly", "monthly_salary": "15000",
                                  "start_date": "2026-08-01", "job_title": "Farm manager"})
manager.post("/work/staff", data={"name": "Baraka Casual", "pay_type": "daily", "daily_rate": "600", "start_date": "2026-08-01"})
r = manager.post("/work/staff", data={"name": "No Rate", "pay_type": "daily"}, follow_redirects=True)
check("staff without a rate refused", "Enter the name" in r.get_data(as_text=True))
asha = q("SELECT * FROM workers WHERE name='Asha Monthly'")[0]
bar = q("SELECT * FROM workers WHERE name='Baraka Casual'")[0]
check("two staff on the register", len(q("SELECT * FROM workers")) == 2)
r = viewer.post("/work/staff", data={"name": "Hack", "pay_type": "daily", "daily_rate": "1"})
check("viewer cannot add staff", r.status_code == 403)

# ---------------------------------------------------------------------------
# 3. Daily sheets for September 2026
# ---------------------------------------------------------------------------
def sheet(c, d, rows):
    data = {"d": d, "wid": []}
    for wid, (status, act, block, qty, unit, hours) in rows.items():
        data["wid"].append(str(wid))
        data[f"s_{wid}"] = status
        data[f"a_{wid}"] = act
        data[f"b_{wid}"] = str(block or "")
        data[f"q_{wid}"] = qty
        data[f"u_{wid}"] = unit
        data[f"h_{wid}"] = hours
    return c.post("/work/day", data=data, follow_redirects=True)

for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"):
    sheet(manager, day, {asha["id"]: ("present", "Irrigation", hab["id"], "", "", ""),
                         bar["id"]: ("present" if day < "2026-09-04" else ("half" if day == "2026-09-04" else ""),
                                     "Harvesting / picking", hab["id"], "40", "kg", "")})
sheet(manager, "2026-09-08", {asha["id"]: ("absent", "", "", "", "", ""), bar["id"]: ("", "", "", "", "", "")})
r = sheet(manager, "2026-09-09", {asha["id"]: ("half", "", "", "", "", ""), bar["id"]: ("", "", "", "", "", "")})
check("sheet warns about a person at work with no job", "no job recorded" in r.get_data(as_text=True))
check("attendance rows saved", len(q("SELECT * FROM attendance")) == 5 + 4 + 2)
# extra job split by hours on 1 Sep: Asha 4h irrigation (sheet) + 4h weeding on D1
q_rows = q("SELECT id FROM work_logs WHERE worker_id=? AND work_date='2026-09-01'", (asha["id"],))
c = sqlite3.connect(dbpath); c.execute("UPDATE work_logs SET hours=4 WHERE id=?", (q_rows[0]["id"],)); c.commit(); c.close()
manager.post("/work/log/add", data={"d": "2026-09-01", "worker_id": asha["id"], "activity": "Weeding",
                                    "block_id": d1["id"], "hours": "4"})
sheet(manager, "2026-09-07", {asha["id"]: ("present", "Pump repair", "", "", "", ""),
                              bar["id"]: ("present", "Generator repair", "", "", "", "")})
html = manager.get("/work/day?d=2026-09-07").get_data(as_text=True)
check("repair jobs offered and kept on the sheet", html.count("selected>Pump repair") == 1
      and html.count("selected>Generator repair") == 1 and "House / building repair" in html
      and "Irrigation pipe repair" in html)
c = sqlite3.connect(dbpath); c.execute("DELETE FROM attendance WHERE work_date='2026-09-07'"); c.execute("DELETE FROM work_logs WHERE work_date='2026-09-07'"); c.commit(); c.close()
r = sheet(manager, "2099-01-01", {asha["id"]: ("present", "Weeding", "", "", "", "")})
check("future sheet refused", "future date" in r.get_data(as_text=True))
r = viewer.post("/work/day", data={"d": "2026-09-10", "wid": [asha["id"]], f"s_{asha['id']}": "present"})
check("viewer cannot fill the sheet", r.status_code == 403)
# re-saving a day replaces, never duplicates
sheet(manager, "2026-09-02", {asha["id"]: ("present", "Irrigation", hab["id"], "", "", ""),
                              bar["id"]: ("present", "Harvesting / picking", hab["id"], "40", "kg", "")})
check("re-saving a day does not duplicate jobs", len(q("SELECT * FROM work_logs WHERE work_date='2026-09-02'")) == 2)

with app.app_context():
    import farm
    lab = farm.labour_rows("2026-09-01", "2026-09-30")
    day_cost = 15000 / 26
    sep1 = [r for r in lab if r["worker_id"] == asha["id"] and r["work_date"] == "2026-09-01"]
    check("a day with two jobs is split by hours", len(sep1) == 2 and all(abs(r["cost"] - day_cost / 2) < 0.01 for r in sep1),
          [round(r["cost"], 2) for r in sep1])
    idle = [r for r in lab if r["activity"] == "No job recorded"]
    check("paid half day with no job shows as idle cost", len(idle) == 1 and abs(idle[0]["cost"] - day_cost / 2) < 0.01)
    total = sum(r["cost"] for r in lab)
    expect = day_cost * 5 + day_cost * 0.5 + 600 * 3.5
    check("month labour cost = 5.5 salary-days + 3.5 casual days", abs(total - expect) < 0.05, f"{total:.2f} vs {expect:.2f}")
    blocks, general = farm.block_summary("2026-09-01", "2026-09-30")
    hab_row = [b for b in blocks if b["block"]["code"] == "HAB-A"][0]
    check("habanero block carries its labour", abs(hab_row["labour"] - (day_cost * 4.5 + 600 * 3.5)) < 0.05,
          round(hab_row["labour"], 2))
    check("costs follow the crop on the block", "Habanero" in farm.crop_summary("2026-09-01", "2026-09-30"))

# ---------------------------------------------------------------------------
# 4. Store: chemical with a pre-harvest interval, organic check, storekeeper
# ---------------------------------------------------------------------------
r = store.post("/stock/add", data={"name": "Test Abamectin", "pack_size": "1 L", "category": "chemical",
                                   "price_per_pack": "2000", "reorder_packs": "1", "phi_days": "7"}, follow_redirects=True)
item = q("SELECT * FROM stock_items WHERE name='Test Abamectin'")
check("storekeeper can add a store item", bool(item))
item = item[0]
check("item stored in ml with harvest interval", item["unit"] == "ml" and item["pack_size"] == 1000 and item["phi_days"] == 7)
store.post(f"/stock/{item['id']}/move", data={"kind": "purchase", "packs": "2", "price_per_pack": "2000",
                                              "move_date": "2026-09-01"})
r = store.post(f"/stock/{item['id']}/issue", data={"move_date": "2026-09-03", "loose": "200", "block_id": d1["id"]},
               follow_redirects=True)
check("issue without a person refused", "needs a name" in r.get_data(as_text=True))
r = store.post(f"/stock/{item['id']}/issue", data={"move_date": "2026-09-03", "loose": "200", "worker_id": asha["id"],
                                                   "block_id": d1["id"]}, follow_redirects=True)
check("non-organic chemical on organic block refused", "Not issued" in r.get_data(as_text=True))
r = store.post(f"/stock/{item['id']}/issue", data={"move_date": "2026-09-03", "loose": "200", "worker_id": asha["id"],
                                                   "block_id": hab["id"], "rate_note": "20 ml / 20 L"}, follow_redirects=True)
check("issue to the conventional block works and warns about harvest", "must not be harvested" in r.get_data(as_text=True))
mv = q("SELECT * FROM stock_movements WHERE item_id=? AND move_type='used'", (item["id"],))
check("issue recorded against person and block at cost", len(mv) == 1 and mv[0]["worker_id"] == asha["id"]
      and mv[0]["block_id"] == hab["id"] and abs(mv[0]["unit_cost"] - 2.0) < 1e-9)
r = store.post(f"/stock/{item['id']}/issue", data={"move_date": "2026-09-03", "loose": "100", "worker_id": bar["id"],
                                                   "block_id": d1["id"], "confirm_organic": "on"}, follow_redirects=True)
mv = q("SELECT * FROM stock_movements WHERE item_id=? AND move_type='used' ORDER BY id DESC LIMIT 1", (item["id"],))[0]
check("confirmed organic exception is flagged in the record", "NOT ORGANIC-APPROVED" in (mv["notes"] or ""))
r = manager.post("/crops/harvests", data={"harvest_date": "2026-09-05", "block_id": hab["id"], "crop": "Habanero",
                                          "kg": "50"}, follow_redirects=True)
check("harvest inside the pre-harvest interval refused", "Not saved" in r.get_data(as_text=True))
r = manager.post("/crops/harvests", data={"harvest_date": "2026-09-02", "block_id": hab["id"], "crop": "Habanero",
                                          "kg": "10"}, follow_redirects=True)
check("a harvest before the spray date is not blocked", q("SELECT SUM(kg) s FROM harvests")[0]["s"] == 10)
c = sqlite3.connect(dbpath); c.execute("DELETE FROM harvests"); c.commit(); c.close()
r = worker.post("/crops/harvests", data={"harvest_date": "2026-09-11", "block_id": hab["id"], "crop": "Habanero",
                                         "grade": "Red A", "kg": "52.5"}, follow_redirects=True)
check("farm worker can record a harvest after the interval", q("SELECT SUM(kg) s FROM harvests")[0]["s"] == 52.5)
# count: shelf has 2000-200-100 = 1700 ml; count finds 1500 -> 200 ml short = KES 400
batch = q("SELECT id FROM stock_batches WHERE item_id=?", (item["id"],))[0]["id"]
store.post(f"/stock/{item['id']}/move", data={"kind": "count", "batch_id": batch, "loose": "1500", "move_date": "2026-09-30"})
r = admin.get("/stock/losses")
check("count shortfall appears on the losses report at cost", "KES 400" in r.get_data(as_text=True))
with app.app_context():
    import farm
    inp = farm.input_rows("2026-09-01", "2026-09-30")
    check("input cost per block (HAB-A KES 400, D1 KES 200)",
          round(sum(r["cost"] for r in inp if r["block_id"] == hab["id"])) == 400
          and round(sum(r["cost"] for r in inp if r["block_id"] == d1["id"])) == 200)
r = vet.post(f"/stock/add", data={"name": "x", "pack_size": "1 L"})
check("vet cannot add store items", r.status_code == 403)

# ---------------------------------------------------------------------------
# 5. Sales
# ---------------------------------------------------------------------------
manager.post("/crops/sales", data={"sale_date": "2026-09-12", "crop": "Habanero", "grade": "Red A", "kg": "50",
                                   "price_per_kg": "125", "buyer": "Test buyer"})
s = q("SELECT * FROM crop_sales")[0]
check("sale on credit saved", s["kg"] * s["price_per_kg"] == 6250 and s["amount_paid"] == 0)
manager.post(f"/crops/sales/{s['id']}/payment", data={"amount": "6250", "ref": "QWE123"})
check("payment clears the balance", q("SELECT amount_paid FROM crop_sales")[0]["amount_paid"] == 6250)
r = worker.get("/crops/sales")
check("farm worker cannot see sales", r.status_code == 403)

# ---------------------------------------------------------------------------
# 6. Payroll for September 2026
# ---------------------------------------------------------------------------
manager.post("/payroll/advance", data={"worker_id": asha["id"], "amount": "2000", "advance_date": "2026-09-10",
                                       "reason": "school fees"})
r = worker.get("/payroll")
check("farm worker cannot open payroll", r.status_code == 403)
r = store.get("/payroll")
check("storekeeper cannot open payroll", r.status_code == 403)
manager.post("/payroll/run", data={"period": "2026-09"})
run = q("SELECT * FROM pay_runs WHERE period='2026-09'")[0]
la = q("SELECT * FROM pay_lines WHERE run_id=? AND worker_id=?", (run["id"], asha["id"]))[0]
lb = q("SELECT * FROM pay_lines WHERE run_id=? AND worker_id=?", (run["id"], bar["id"]))[0]
ded = 1.5 * 15000 / 26
check("monthly: full salary less 1.5 unpaid days", abs(la["basic"] - 15000) < 0.01 and abs(la["absence_deduction"] - ded) < 0.01,
      (la["basic"], la["absence_deduction"]))
check("monthly: advance recovered and net correct", la["advances"] == 2000 and abs(la["net"] - (15000 - ded - 2000)) < 0.01, la["net"])
check("casual: 3.5 days x 600 = 2,100", abs(lb["gross"] - 2100) < 0.01 and lb["net"] == lb["gross"], lb["gross"])
r = manager.post(f"/payroll/run/2026-09/line/{la['id']}", data={"additions": "500", "advances": "5000",
                                                                 "other_deductions": "0", "notes": "overtime"},
                 follow_redirects=True)
la = q("SELECT * FROM pay_lines WHERE id=?", (la["id"],))[0]
check("advance deduction capped at what is owed", la["advances"] == 2000)
check("additions raise gross and net", abs(la["gross"] - (15000 - ded + 500)) < 0.01)
r = manager.post("/payroll/run/2026-09/approve")
check("manager cannot approve pay", r.status_code == 403)
r = admin.post("/payroll/run/2026-09/approve", follow_redirects=True)
check("administrator approves pay", q("SELECT status FROM pay_runs WHERE period='2026-09'")[0]["status"] == "approved")
la2 = q("SELECT * FROM pay_lines WHERE id=?", (la["id"],))[0]
check("approval keeps the manager's adjustments", la2["additions"] == 500 and la2["notes"] == "overtime")
r = sheet(manager, "2026-09-15", {asha["id"]: ("present", "Weeding", "", "", "", "")})
check("approved month's sheets are frozen", "can no longer be changed" in r.get_data(as_text=True)
      and not q("SELECT * FROM attendance WHERE work_date='2026-09-15'"))
with app.app_context():
    from blueprints.work import outstanding_advances
    check("advance balance is zero after approval", outstanding_advances(asha["id"]) == 0)
r = manager.post("/payroll/run/2026-09/paid", data={"paid_date": "2026-10-02", "line_id": [la["id"], lb["id"]],
                                                    f"ref_{la['id']}": "MPESA1", f"ref_{lb['id']}": "MPESA2"},
                 follow_redirects=True)
check("payments recorded and run marked paid", q("SELECT status FROM pay_runs WHERE period='2026-09'")[0]["status"] == "paid"
      and q("SELECT payment_ref FROM pay_lines WHERE id=?", (la["id"],))[0]["payment_ref"] == "MPESA1")
r = admin.post("/payroll/run/2026-09/reopen", follow_redirects=True)
check("a paid run cannot be reopened", q("SELECT status FROM pay_runs WHERE period='2026-09'")[0]["status"] == "paid")
r = viewer.get(f"/payroll/run/2026-09/slip/{asha['id']}")
check("payslip shows the net pay", r.status_code == 200 and f"{15000 - ded + 500 - 2000:,.0f}" in r.get_data(as_text=True))
r = admin.get("/payroll/run/2026-09?export=xlsx")
check("payroll exports to Excel", r.status_code == 200 and r.data[:2] == b"PK")

# ---------------------------------------------------------------------------
# 7. Assets
# ---------------------------------------------------------------------------
store.post("/assets/add", data={"name": "Knapsack sprayer 16 L", "category": "Sprayer", "cost": "4500", "location": "Store"})
a = q("SELECT * FROM assets")[0]
check("asset numbered AST-0001", a["asset_no"] == "AST-0001")
r = store.post(f"/assets/{a['id']}/event", data={"event_type": "check_out"}, follow_redirects=True)
check("check-out without a person refused", "Say who" in r.get_data(as_text=True))
store.post(f"/assets/{a['id']}/event", data={"event_type": "check_out", "worker_id": bar["id"]})
check("checked out to the person", q("SELECT status, custodian_id FROM assets")[0]["status"] == "out"
      and q("SELECT custodian_id FROM assets")[0]["custodian_id"] == bar["id"])
r = admin.get("/assets")
check("asset shows under 'Checked out now'", "Checked out now" in r.get_data(as_text=True))
store.post(f"/assets/{a['id']}/event", data={"event_type": "return", "condition": "needs_repair"})
check("returned with condition", q("SELECT status, condition FROM assets")[0]["condition"] == "needs_repair")
r = store.post(f"/assets/{a['id']}/event", data={"event_type": "lost"}, follow_redirects=True)
check("lost needs a note", "Write what happened" in r.get_data(as_text=True))
r = store.post(f"/assets/{a['id']}/event", data={"event_type": "disposed", "notes": "x"})
check("storekeeper cannot write off an asset", r.status_code == 403)
store.post(f"/assets/{a['id']}/event", data={"event_type": "repair", "cost": "800", "notes": "new pump seal"})
check("repair cost recorded and condition good", q("SELECT condition FROM assets")[0]["condition"] == "good"
      and q("SELECT SUM(cost) s FROM asset_events")[0]["s"] == 800)
r = worker.get("/assets")
check("farm worker cannot open assets", r.status_code == 403)

# ---------------------------------------------------------------------------
# 8. Milestones
# ---------------------------------------------------------------------------
manager.post("/milestones/load-plan")
n1 = len(q("SELECT * FROM milestones"))
manager.post("/milestones/load-plan")
check("plan milestones load once only", n1 > 20 and len(q("SELECT * FROM milestones")) == n1, n1)
m = q("SELECT * FROM milestones WHERE plan_key='sk-gate0'")[0]
check("Sukoon gate 0 dated 13 Nov 2026", m["due_date"] == "2026-11-13")
manager.post(f"/milestones/{m['id']}", data={"status": "done", "due_date": m["due_date"], "owner": "Mohamed",
                                             "note": "quotes in"})
m = q("SELECT * FROM milestones WHERE id=?", (m["id"],))[0]
check("milestone marked done with owner and dated note", m["status"] == "done" and m["done_date"] and "quotes in" in m["detail"])
with app.app_context():
    import farm
    check("overdue is red, undated is grey", farm.rag("2020-01-01", "open")[0] == "red" and farm.rag(None, "open")[0] == "gray")

# ---------------------------------------------------------------------------
# 9. Every page opens for the right people
# ---------------------------------------------------------------------------
pages = ["/crops", "/crops?month=2026-09", f"/crops/blocks/{hab['id']}?month=2026-09", "/crops/harvests?month=2026-09",
         "/crops/sales?month=2026-09", "/crops/report/blocks?month=2026-09", "/crops/report/crops?month=2026-09",
         "/crops/report/inputs?month=2026-09", "/work/day", "/work/day?d=2026-09-01", "/work/log?month=2026-09",
         "/work/staff", f"/work/staff/{asha['id']}?month=2026-09", "/payroll", "/payroll/run/2026-09", "/stock",
         "/stock?cat=chemical", f"/stock/{item['id']}", "/stock/countsheet", "/stock/losses", "/stock/report",
         "/assets", f"/assets/{a['id']}", "/assets/verify-sheet", "/milestones", "/milestones?done=1&enterprise=Livestock",
         "/reports", "/", "/animals"]
for p in pages:
    r = admin.get(p)
    check(f"admin opens {p}", r.status_code == 200, r.status_code)
allowed = {
    "manager": pages,
    "viewer": [p for p in pages],
    "store": ["/crops", "/crops/harvests", "/crops/sales", "/stock", f"/stock/{item['id']}", "/assets", "/milestones", "/reports", "/"],
    "worker": ["/crops", "/crops/harvests", "/stock", "/reports", "/"],
    "vet": ["/stock", "/reports", "/"],
}
denied = {
    "store": ["/payroll", "/work/day", "/work/staff"],
    "worker": ["/payroll", "/work/day", "/assets", "/milestones", "/crops/sales"],
    "vet": ["/crops", "/payroll", "/work/day", "/assets"],
}
sessions = dict(manager=manager, viewer=viewer, store=store, worker=worker, vet=vet)
for who, ps in allowed.items():
    bad = [p for p in ps if sessions[who].get(p).status_code != 200]
    check(f"{who} can open their pages", not bad, bad)
for who, ps in denied.items():
    bad = [p for p in ps if sessions[who].get(p).status_code != 403]
    check(f"{who} is blocked from others", not bad, bad)
# menu shows the right sections
html = store.get("/").get_data(as_text=True)
check("storekeeper menu shows Store and Assets but not Payroll", "Store</a>" in html and "Assets</a>" in html and "Payroll</a>" not in html)
html = admin.get("/").get_data(as_text=True)
check("admin menu shows Crops & Farm section", "Crops &amp; Farm" in html and "Daily Sheet" in html)

# fresh database also starts cleanly
fresh = os.path.join(tmpdir, "fresh.db")


class FreshConfig(Config):
    DATABASE = fresh
    TESTING = True


create_app(FreshConfig)
f = sqlite3.connect(fresh)
check("a brand-new database starts with all tables", f.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='pay_lines'").fetchone()[0] == 1)
f.close()

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "ALL CHECKS PASSED")
shutil.rmtree(tmpdir, ignore_errors=True)
sys.exit(1 if FAILS else 0)
