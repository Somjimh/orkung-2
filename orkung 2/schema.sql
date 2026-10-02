-- Orkung Livestock Manager -- database schema (SQLite)
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT,
    role TEXT NOT NULL CHECK(role IN ('admin','manager','worker','viewer','vet')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,               -- create/update/status_change/delete_attempt/login/logout/import/export
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    summary TEXT,
    details TEXT,                       -- JSON blob of before/after where relevant
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS species (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS breeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    species_id INTEGER NOT NULL REFERENCES species(id),
    name TEXT NOT NULL,
    UNIQUE(species_id, name)
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS groups_ (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    site_id INTEGER REFERENCES sites(id),
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, site_id)
);

CREATE TABLE IF NOT EXISTS animals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_no TEXT NOT NULL UNIQUE,          -- internal record number e.g. ORK-000123
    tag_id TEXT NOT NULL,                    -- ear tag / ID
    eid TEXT,                                -- electronic ID
    name TEXT,
    species_id INTEGER NOT NULL REFERENCES species(id),
    breed_id INTEGER REFERENCES breeds(id),
    sex TEXT NOT NULL CHECK(sex IN ('female','male')),
    dob TEXT,
    dob_estimated INTEGER NOT NULL DEFAULT 0,
    birth_type TEXT,                         -- single/twin/triplet/other
    birth_weight REAL,
    color_markings TEXT,
    sire_id INTEGER REFERENCES animals(id),
    dam_id INTEGER REFERENCES animals(id),
    current_group_id INTEGER REFERENCES groups_(id),
    current_site_id INTEGER REFERENCES sites(id),
    source TEXT NOT NULL DEFAULT 'born_on_farm' CHECK(source IN ('born_on_farm','purchased','transferred_in')),
    acquisition_date TEXT,
    supplier TEXT,
    reproductive_status TEXT NOT NULL DEFAULT 'not_breeding',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','sold','transferred','slaughtered','missing','deceased')),
    status_date TEXT,
    status_notes TEXT,
    photo_path TEXT,
    notes TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Uniqueness of tag_id among *active* animals is enforced in the application layer
-- (blueprints/animals.py) rather than the database, because the business rule is
-- "block it, but let an administrator explicitly resolve the conflict" -- not a
-- hard constraint the database should refuse outright.
CREATE INDEX IF NOT EXISTS idx_animals_tag ON animals(tag_id);
CREATE INDEX IF NOT EXISTS idx_animals_species ON animals(species_id);
CREATE INDEX IF NOT EXISTS idx_animals_status ON animals(status);

CREATE TABLE IF NOT EXISTS weight_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    weight_kg REAL NOT NULL,
    measured_on TEXT NOT NULL,
    method TEXT,
    body_condition_score REAL,
    notes TEXT,
    flagged_outlier INTEGER NOT NULL DEFAULT 0,
    confirmed_by INTEGER REFERENCES users(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_weights_animal ON weight_records(animal_id, measured_on);

CREATE TABLE IF NOT EXISTS animal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    event_type TEXT NOT NULL CHECK(event_type IN ('birth','purchase','transfer','group_move','sale','slaughter','death','missing','return','tag_replacement','status_change')),
    event_date TEXT NOT NULL,
    from_group_id INTEGER REFERENCES groups_(id),
    to_group_id INTEGER REFERENCES groups_(id),
    from_site_id INTEGER REFERENCES sites(id),
    to_site_id INTEGER REFERENCES sites(id),
    old_tag TEXT,
    new_tag TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_animal ON animal_events(animal_id, event_date);

CREATE TABLE IF NOT EXISTS group_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    group_id INTEGER NOT NULL REFERENCES groups_(id),
    start_date TEXT NOT NULL,
    end_date TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memberships_animal ON group_memberships(animal_id);
CREATE INDEX IF NOT EXISTS idx_memberships_group ON group_memberships(group_id);

CREATE TABLE IF NOT EXISTS breeding_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dam_id INTEGER NOT NULL REFERENCES animals(id),
    sire_id INTEGER REFERENCES animals(id),
    sire_freeform TEXT,                       -- if sire not on-farm/not in register
    method TEXT NOT NULL CHECK(method IN ('natural','ai')),
    heat_observed_date TEXT,
    service_date TEXT,
    pregnancy_check_date TEXT,
    pregnancy_result TEXT CHECK(pregnancy_result IN ('pending','pregnant','not_pregnant') ),
    expected_birth_date TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','lost','not_pregnant')),
    loss_date TEXT,
    loss_notes TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_breeding_dam ON breeding_records(dam_id);

CREATE TABLE IF NOT EXISTS birth_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    breeding_id INTEGER REFERENCES breeding_records(id),
    dam_id INTEGER NOT NULL REFERENCES animals(id),
    sire_id INTEGER REFERENCES animals(id),
    birth_date TEXT NOT NULL,
    total_born INTEGER NOT NULL DEFAULT 0,
    born_alive INTEGER NOT NULL DEFAULT 0,
    stillborn INTEGER NOT NULL DEFAULT 0,
    colostrum_notes TEXT,
    mothering_notes TEXT,
    weaning_date TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_births_dam ON birth_records(dam_id);

CREATE TABLE IF NOT EXISTS birth_offspring (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    birth_record_id INTEGER NOT NULL REFERENCES birth_records(id),
    animal_id INTEGER NOT NULL REFERENCES animals(id)
);

CREATE TABLE IF NOT EXISTS medicines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK(type IN ('medicine','vaccine')),
    default_meat_withdrawal_days INTEGER NOT NULL DEFAULT 0,
    default_milk_withdrawal_days INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS health_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    observation_date TEXT NOT NULL,
    symptoms TEXT,
    provisional_diagnosis TEXT,
    confirmed_diagnosis TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_health_animal ON health_observations(animal_id);

CREATE TABLE IF NOT EXISTS treatments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    health_observation_id INTEGER REFERENCES health_observations(id),
    treatment_type TEXT,
    medicine_id INTEGER REFERENCES medicines(id),
    medicine_name TEXT,
    dose TEXT,
    dose_unit TEXT,
    route TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    administered_by TEXT,
    veterinarian TEXT,
    meat_withdrawal_end TEXT,
    milk_withdrawal_end TEXT,
    follow_up_date TEXT,
    result TEXT,
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_treatments_animal ON treatments(animal_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    notes TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attachments_entity ON attachments(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    related_entity_type TEXT,
    related_entity_id INTEGER,
    related_group_id INTEGER REFERENCES groups_(id),
    due_date TEXT NOT NULL,
    assigned_to INTEGER REFERENCES users(id),
    priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('normal','high','urgent')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','cancelled')),
    completed_at TEXT,
    completed_by INTEGER REFERENCES users(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date, status);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id INTEGER NOT NULL REFERENCES animals(id),
    body TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Medicine & supplies stock ---------------------------------------------------
-- Quantities are kept in a base unit per item (ml, g or pcs). Litres are stored
-- as ml and kilograms as g. Cost per base unit = price_per_pack / pack_size.
CREATE TABLE IF NOT EXISTS stock_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'medicine',      -- medicine / vaccine / supply
    pack_size REAL NOT NULL DEFAULT 1,
    unit TEXT NOT NULL DEFAULT 'ml',                -- ml / g / pcs
    price_per_pack REAL,                            -- KES
    reorder_level REAL NOT NULL DEFAULT 0,          -- in base units
    medicine_id INTEGER REFERENCES medicines(id),
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES stock_items(id),
    batch_no TEXT,
    expiry TEXT,                                    -- YYYY-MM-DD (end of month if only month known)
    location TEXT,
    condition TEXT,
    qty REAL NOT NULL DEFAULT 0,                    -- base units on hand
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_batches_item ON stock_batches(item_id);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES stock_items(id),
    batch_id INTEGER REFERENCES stock_batches(id),
    move_date TEXT NOT NULL,
    move_type TEXT NOT NULL CHECK(move_type IN ('count','purchase','used','adjust','disposed')),
    qty REAL NOT NULL,                              -- signed base units (+ in, - out); for 'count' the counted amount
    unit_cost REAL,                                 -- KES per base unit at the time
    treatment_id INTEGER REFERENCES treatments(id),
    notes TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_moves_item ON stock_movements(item_id, move_date);
