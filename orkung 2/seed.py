"""Reference-data + initial-user bootstrap. Runs once, automatically, the
first time the app starts against a fresh database (see app.py / db.init_db).
This is NOT the demo dataset -- see seed_demo.py for the clearly-labelled
fictional demo animals.
"""
from werkzeug.security import generate_password_hash
import db


DEFAULT_SPECIES = [
    ("Goat", 1),
    ("Sheep", 1),
    ("Cattle", 0),
    ("Other", 0),
]

DEFAULT_BREEDS = {
    "Goat": ["Boer", "Galla", "Toggenburg", "Saanen", "Local/Indigenous", "Cross-bred"],
    "Sheep": ["Dorper", "Red Maasai", "Merino", "Blackhead Persian", "Local/Indigenous", "Cross-bred"],
    "Cattle": ["Friesian", "Boran", "Sahiwal", "Local/Indigenous", "Cross-bred"],
    "Other": ["Unspecified"],
}

DEFAULT_MEDICINES = [
    ("Oxytetracycline LA", "medicine", 21, 4, "Broad-spectrum antibiotic"),
    ("Albendazole (dewormer)", "medicine", 7, 3, "Broad-spectrum anthelmintic"),
    ("Ivermectin injectable", "medicine", 14, 5, "Endectocide"),
    ("PPR Vaccine", "vaccine", 0, 0, "Peste des Petits Ruminants"),
    ("CDT (Clostridial) Vaccine", "vaccine", 0, 0, "Enterotoxaemia / tetanus"),
    ("Foot rot treatment (topical)", "medicine", 0, 0, "Topical foot bath / spray"),
]

DEFAULT_USERS = [
    # username, password, full name, role, email
    ("admin", "Admin#2026", "Farm Administrator", "admin", "admin@orkung.local"),
    ("manager", "Manager#2026", "Herd Manager", "manager", "manager@orkung.local"),
    ("worker", "Worker#2026", "Farm Worker", "worker", "worker@orkung.local"),
    ("viewer", "Viewer#2026", "Auditor", "viewer", "viewer@orkung.local"),
    ("vet", "Vet#2026", "Visiting Veterinarian", "vet", "vet@orkung.local"),
]

DEFAULT_SITES = [("Home Farm", "Main holding site")]
DEFAULT_GROUPS = [("Breeding Does/Ewes", "General breeding flock"),
                   ("Growing Kids/Lambs", "Weaned young stock, growing out"),
                   ("Sale/Finishing Pen", "Animals being finished for sale")]


def run_reference_seed():
    already = db.query("SELECT 1 FROM users LIMIT 1", one=True)
    if already:
        return  # already bootstrapped

    for name, enabled in DEFAULT_SPECIES:
        db.execute("INSERT OR IGNORE INTO species (name, enabled) VALUES (?,?)", (name, enabled))

    species_rows = db.query("SELECT id, name FROM species")
    species_id_by_name = {r["name"]: r["id"] for r in species_rows}
    for sp_name, breeds in DEFAULT_BREEDS.items():
        sid = species_id_by_name[sp_name]
        for b in breeds:
            db.execute("INSERT OR IGNORE INTO breeds (species_id, name) VALUES (?,?)", (sid, b))

    for name, mtype, meat_days, milk_days, notes in DEFAULT_MEDICINES:
        db.execute(
            "INSERT OR IGNORE INTO medicines (name, type, default_meat_withdrawal_days, default_milk_withdrawal_days, notes) "
            "VALUES (?,?,?,?,?)", (name, mtype, meat_days, milk_days, notes))

    for username, pw, full_name, role, email in DEFAULT_USERS:
        db.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, full_name, role, email) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(pw), full_name, role, email))

    for name, desc in DEFAULT_SITES:
        db.execute("INSERT OR IGNORE INTO sites (name, description) VALUES (?,?)", (name, desc))

    site = db.query("SELECT id FROM sites LIMIT 1", one=True)
    for name, desc in DEFAULT_GROUPS:
        db.execute("INSERT OR IGNORE INTO groups_ (name, site_id, description) VALUES (?,?,?)",
                   (name, site["id"] if site else None, desc))

    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('weigh_reminder_days','60')")
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('weight_change_warn_pct','30')")
