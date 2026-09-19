"""Creates a small, CLEARLY LABELLED demonstration dataset of fictional goats
and sheep, covering the example scenarios called for in the spec:

  - an animal with several historical weights
  - a pregnant animal with an expected birth date
  - a mother with linked offspring
  - a completed treatment
  - a vaccination reminder (task)
  - an animal transferred between groups
  - an inactive sold animal whose history remains accessible

All demo animals are flagged is_demo=1 so they are never mixed up with a
real farm's live records; a farm can wipe them independently (see README).

Usage:  python3 seed_demo.py
"""
from datetime import date, timedelta
from app import create_app
import db


def d(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def future(days_ahead):
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def run():
    app = create_app()
    with app.app_context():
        already = db.query("SELECT 1 FROM animals WHERE is_demo=1 LIMIT 1", one=True)
        if already:
            print("Demo data already present -- nothing to do. (Delete demo animals first to reseed.)")
            return

        admin = db.query("SELECT id FROM users WHERE username='admin'", one=True)
        uid = admin["id"]
        species = {r["name"]: r["id"] for r in db.query("SELECT * FROM species")}
        breeds = {(r["species_id"], r["name"]): r["id"] for r in db.query("SELECT * FROM breeds")}
        groups = {r["name"]: r["id"] for r in db.query("SELECT * FROM groups_")}
        sites = {r["name"]: r["id"] for r in db.query("SELECT * FROM sites")}
        goat = species["Goat"]; sheep = species["Sheep"]
        site_id = sites["Home Farm"]
        grp_breeding = groups["Breeding Does/Ewes"]
        grp_growing = groups["Growing Kids/Lambs"]
        grp_sale = groups["Sale/Finishing Pen"]

        def add_animal(**kw):
            record_no = db.next_record_no()
            kw.setdefault("current_site_id", site_id)
            kw.setdefault("source", "born_on_farm")
            kw.setdefault("reproductive_status", "open")
            kw.setdefault("status", "active")
            cols = ["record_no", "is_demo", "created_by", "updated_by"] + list(kw.keys())
            vals = [record_no, 1, uid, uid] + list(kw.values())
            placeholders = ",".join("?" for _ in cols)
            return db.execute(f"INSERT INTO animals ({','.join(cols)}) VALUES ({placeholders})", vals), record_no

        # 1) Doe with a full weight history (steady growth) -------------------------------
        doe1_id, _ = add_animal(tag_id="DEMO-D01", name="Amani", species_id=goat,
                                breed_id=breeds.get((goat, "Boer")), sex="female",
                                dob=d(540), birth_type="twin", birth_weight=3.1,
                                current_group_id=grp_breeding, reproductive_status="lactating")
        weights = [(d(400), 14.0), (d(300), 19.5), (d(200), 25.0), (d(90), 31.0), (d(20), 33.5)]
        for wd, wk in weights:
            db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, method, created_by) VALUES (?,?,?,?,?)",
                      (doe1_id, wk, wd, "hanging scale", uid))

        # 2) Pregnant ewe with expected birth date -----------------------------------------
        ewe1_id, ewe1_no = add_animal(tag_id="DEMO-E01", name="Zawadi", species_id=sheep,
                                      breed_id=breeds.get((sheep, "Dorper")), sex="female",
                                      dob=d(730), current_group_id=grp_breeding, reproductive_status="pregnant")
        ram1_id, _ = add_animal(tag_id="DEMO-R01", species_id=sheep, breed_id=breeds.get((sheep, "Dorper")),
                                sex="male", dob=d(900), current_group_id=grp_breeding, reproductive_status="open")
        br_id = db.execute(
            "INSERT INTO breeding_records (dam_id, sire_id, method, service_date, pregnancy_check_date, "
            "pregnancy_result, expected_birth_date, status, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (ewe1_id, ram1_id, "natural", d(110), d(75), "pregnant", future(35), "active", uid))
        db.execute("INSERT INTO tasks (task_type, title, description, related_entity_type, related_entity_id, due_date, created_by) "
                  "VALUES (?,?,?,?,?,?,?)",
                  ("expected_birth", f"Expected birth: {ewe1_no}", "Prepare lambing pen and supplies.",
                   "breeding_record", br_id, future(35), uid))

        # 3) Mother with linked offspring (already kidded) ---------------------------------
        doe2_id, doe2_no = add_animal(tag_id="DEMO-D02", name="Neema", species_id=goat,
                                      breed_id=breeds.get((goat, "Galla")), sex="female",
                                      dob=d(800), current_group_id=grp_breeding, reproductive_status="lactating")
        buck1_id, _ = add_animal(tag_id="DEMO-B01", species_id=goat, breed_id=breeds.get((goat, "Galla")),
                                 sex="male", dob=d(1000), current_group_id=grp_breeding)
        birth_id = db.execute(
            "INSERT INTO birth_records (dam_id, sire_id, birth_date, total_born, born_alive, stillborn, "
            "colostrum_notes, mothering_notes, weaning_date, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doe2_id, buck1_id, d(70), 2, 2, 0, "Both kids nursed within the hour.", "Good mothering, no assistance needed.",
             d(0), uid))
        for i, (tag, sex, w) in enumerate([("DEMO-K01", "female", 2.8), ("DEMO-K02", "male", 3.0)]):
            kid_id, _ = add_animal(tag_id=tag, species_id=goat, breed_id=breeds.get((goat, "Galla")), sex=sex,
                                   dob=d(70), birth_type="twin", birth_weight=w, sire_id=buck1_id, dam_id=doe2_id,
                                   current_group_id=grp_growing, reproductive_status="young_stock")
            db.execute("INSERT INTO birth_offspring (birth_record_id, animal_id) VALUES (?,?)", (birth_id, kid_id))
            db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, created_by) VALUES (?,?,?,?)",
                      (kid_id, w, d(70), uid))
            db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, created_by) VALUES (?,?,?,?)",
                      (kid_id, w + 6.5, d(10), uid))

        # 4) Completed treatment (with withdrawal already lapsed) --------------------------
        db.execute(
            "INSERT INTO treatments (animal_id, treatment_type, medicine_name, dose, dose_unit, route, start_date, "
            "end_date, administered_by, meat_withdrawal_end, milk_withdrawal_end, result, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doe1_id, "Foot rot treatment", "Oxytetracycline LA", "5", "ml", "IM", d(60), d(58), "Farm Worker",
             d(37), d(53), "Resolved, no lameness on follow-up.", uid))

        # 5) Vaccination reminder (upcoming task) -------------------------------------------
        db.execute("INSERT INTO tasks (task_type, title, description, related_entity_type, related_entity_id, due_date, priority, created_by) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  ("vaccination", f"PPR vaccination due: {doe2_no}", "Annual PPR booster.", "animal", doe2_id,
                   future(9), "high", uid))

        # 6) Animal transferred between groups (has group history) -------------------------
        wether_id, wether_no = add_animal(tag_id="DEMO-W01", species_id=sheep, breed_id=breeds.get((sheep, "Red Maasai")),
                                          sex="male", dob=d(200), current_group_id=grp_sale, reproductive_status="not_breeding")
        db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, end_date, created_by) VALUES (?,?,?,?,?)",
                  (wether_id, grp_growing, d(200), d(45), uid))
        db.execute("INSERT INTO group_memberships (animal_id, group_id, start_date, created_by) VALUES (?,?,?,?)",
                  (wether_id, grp_sale, d(45), uid))
        db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, from_group_id, to_group_id, created_by) "
                  "VALUES (?,?,?,?,?,?)", (wether_id, "group_move", d(45), grp_growing, grp_sale, uid))
        db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, created_by) VALUES (?,?,?,?)",
                  (wether_id, 28.0, d(45), uid))
        db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, created_by) VALUES (?,?,?,?)",
                  (wether_id, 34.5, d(5), uid))

        # 7) Inactive SOLD animal -- history must remain fully accessible -------------------
        sold_id, sold_no = add_animal(tag_id="DEMO-S01", species_id=goat, breed_id=breeds.get((goat, "Toggenburg")),
                                      sex="male", dob=d(400), current_group_id=grp_sale, status="active",
                                      reproductive_status="not_breeding")
        db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, created_by) VALUES (?,?,?,?)",
                  (sold_id, 22.0, d(300), uid))
        db.execute("INSERT INTO weight_records (animal_id, weight_kg, measured_on, created_by) VALUES (?,?,?,?)",
                  (sold_id, 38.0, d(20), uid))
        db.execute("UPDATE animals SET status='sold', status_date=?, status_notes=? WHERE id=?",
                  (d(10), "Sold to Kericho Livestock Market, KES 8,500", sold_id))
        db.execute("INSERT INTO animal_events (animal_id, event_type, event_date, notes, created_by) VALUES (?,?,?,?,?)",
                  (sold_id, "sale", d(10), "Sold to Kericho Livestock Market", uid))

        # A couple more plain active animals so lists/filters have some variety
        add_animal(tag_id="DEMO-D03", species_id=goat, breed_id=breeds.get((goat, "Saanen")), sex="female",
                  dob=d(260), current_group_id=grp_growing, reproductive_status="young_stock")
        add_animal(tag_id="DEMO-E02", species_id=sheep, breed_id=breeds.get((sheep, "Merino")), sex="female",
                  dob=d(1100), current_group_id=grp_breeding, reproductive_status="open")

        db.audit(None, "import", "demo_data", summary="Demo dataset seeded (fictional goats/sheep, clearly labelled is_demo=1)")
        print("Demo dataset created: 11 animals covering weight history, pregnancy, birth/offspring linkage,")
        print("a completed treatment, a vaccination reminder, a group transfer, and a sold (inactive) animal.")


if __name__ == "__main__":
    run()
