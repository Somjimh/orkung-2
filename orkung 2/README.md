# Orkung Livestock Manager

A dedicated livestock record-keeping system for goats and sheep (with cattle
and other species available when an administrator enables them). Built as a
self-contained web application: a Python/Flask server, a SQLite database, and
a server-rendered, mobile-first interface with an original deep-green/earth
design system. No third-party branding, layout, or code was copied from any
other product -- CaproManager was consulted only as a functional reference
for what an individual-animal record system needs to do.

## Why Flask + SQLite, not Node/Next.js

This was built inside a sandboxed session with no access to the npm or PyPI
package registries, so a Node/React/Prisma stack could not actually be
installed or run there. Flask, Jinja2, Werkzeug and SQLite's Python driver
were pre-installed, which made it possible to build *and fully exercise* a
real, running application (automated end-to-end tests, live screenshots at
desktop and mobile widths) rather than hand-writing a much larger JavaScript
codebase with no way to execute it. On your own machine, this needs nothing
more exotic than `pip install -r requirements.txt`.

The result is a genuine multi-user, server-side-authenticated, relational
application -- not a static mockup. Every rule in the spec ("apply
permissions on the server", "never silently overwrite", "reload and verify
before reporting success", "preserve history when status changes", and so on)
is implemented and covered by the automated test in `tests/smoke_test.py`.

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Then open http://localhost:5000 in a browser. The database and an
`instance/uploads` folder are created automatically on first run, along with
five demo user accounts (change these before real use -- see **Users &
Settings**):

| Username | Password      | Role                      |
|----------|---------------|---------------------------|
| admin    | Admin#2026    | Administrator             |
| manager  | Manager#2026  | Manager                   |
| worker   | Worker#2026   | Farm worker               |
| viewer   | Viewer#2026   | Viewer / Auditor          |
| vet      | Vet#2026      | Veterinary professional   |

### Loading the demo dataset

A small, clearly-labelled set of fictional goats and sheep (flagged
`is_demo=1` in the database, kept separate from real records) can be loaded
with:

```bash
python3 seed_demo.py
```

It covers every example scenario called for in the spec: an animal with a
full weight history, a pregnant animal with an expected birth date, a mother
with linked offspring, a completed treatment, a pending vaccination
reminder, an animal transferred between groups (with group history), and an
inactive **sold** animal whose full history remains accessible. Run it once
against a fresh database; it will refuse to run again once demo data exists.

## Deploying for real use

The built-in server (`python3 app.py`) is fine for trying the app out, but
is Flask's development server. For real farm use:

1. Set a real `SECRET_KEY` environment variable (a long random string).
2. Run behind a production WSGI server, e.g.
   `pip install waitress && waitress-serve --port=8000 app:app`
   or `gunicorn -w 2 -b 0.0.0.0:8000 app:app` (Linux/macOS).
3. Put it behind HTTPS (a reverse proxy such as Caddy or nginx is the
   simplest way) so login sessions are encrypted in transit.
4. Point `ORKUNG_DB` at wherever you want the SQLite file to live if you
   don't want it inside the app folder, e.g. `export ORKUNG_DB=/data/orkung.db`.

SQLite comfortably handles a single farm's herd and a handful of concurrent
users. If you outgrow it, the schema (`schema.sql`) is plain SQL and the
data-access layer (`db.py`) is a thin wrapper -- moving to PostgreSQL later
is a swap of the connection layer, not a redesign.

## Backup & restore

- **Backup:** signed in as an administrator, go to **Users & Settings ->
  Backup**. This downloads the entire live SQLite database as one file
  (`orkung_backup_<timestamp>.db`). Store copies off the server.
- **Restore:** stop the application, replace `instance/orkung.db` with the
  backup file (keep the same filename, `orkung.db`), then start the
  application again. Uploaded photos/documents live in
  `instance/uploads/` -- back that folder up alongside the database if you
  want attachments restored too.

## Roles & permissions (enforced server-side)

Every route checks the signed-in user's role before doing anything --
permissions are not just hidden buttons. Denied requests are logged to the
audit trail.

- **Administrator** -- everything, including user management, species/breed
  configuration, and backups.
- **Manager** -- full animal records, breeding/health/movement/task
  management, and reports.
- **Farm worker** -- add animals and daily records (weights, health
  observations/treatments, movements, breeding/birth entries), complete
  assigned tasks; cannot manage users or delete/administer.
- **Viewer / Auditor** -- read-only access across the system, including the
  audit history report.
- **Veterinary professional** -- scoped to animal viewing, health
  observations/treatments/medicines, tasks, and reports only.

## What's implemented

Dashboard with live summary cards, herd breakdowns (species/sex/breed/age
class/group), population-over-time and weight-trend graphs (hand-rolled
inline SVG -- no external charting library, so the app has zero internet
dependency), upcoming tasks/alerts, and farm/site/species/group/date
filters; a searchable, filterable animal register with duplicate-tag
prevention (administrators can explicitly resolve a conflict); a full animal
profile (identification, parentage/offspring, weight history with
gain/ADG, breeding & birth history, health & treatments, movement &
group history, documents, a chronological timeline, comments, and a visible
audit trail); individual and bulk weight entry that never overwrites
history, warns on unusually large changes and requires confirmation to
save them, and exports to CSV/Excel; breeding & reproduction records
(heat, natural/AI service, pregnancy checks, expected birth dates,
losses/abortions) linked to births, where recording a birth can create and
auto-link offspring animal records in one step; health observations and
treatments (record-keeping only -- the app does not diagnose or prescribe)
with medicine/vaccine catalog defaults, withdrawal-period tracking, and
follow-up/vaccination alerts; groups, sites and bulk movements with full
historical membership; a task/alert system with due dates, assignment, and
a retained completion history; fourteen filterable, printable, CSV/Excel
exportable reports; CSV import with a downloadable template and a
preview/validate step before anything is committed; and a full audit log
covering create/update/status-change/access-denied events.

## Offline / mobile

The interface is responsive (desktop sidebar collapses to a mobile drawer
menu; tables become record cards on small screens) and installable as a
Progressive Web App (manifest + service worker cache the app shell). Per the
spec's own fallback instructions: this version does **not** claim full
offline data sync. It shows a clear "you're offline" banner, and any
in-progress form is continuously saved to the browser's local storage so a
dropped connection can't lose what you were typing -- but a record is only
actually written to the server once you're back online. Building true
offline-first sync with conflict detection is a substantial project of its
own; this keeps the app honest about that boundary rather than
overpromising.

## Automated testing

`tests/smoke_test.py` drives the running application over HTTP (all five
roles logging in, server-side permission enforcement, animal CRUD,
duplicate-tag handling, multi-entry weight history with gain/ADG
calculation and outlier confirmation, breeding -> birth -> linked-offspring
creation, treatments and withdrawal tracking, group moves, status changes
that preserve history, task completion, report exports, validation errors,
and audit logging) and asserts on the results. `tests/screenshot_qa.py`
captures desktop- and mobile-width screenshots of the key screens with
Playwright for visual review. Run the app, then:

```bash
python3 tests/smoke_test.py
```

## Project layout

```
app.py              Flask application factory, routing setup
config.py            Configuration (DB path, upload dir, business-rule thresholds)
db.py                SQLite access layer, audit logging, date helpers
auth.py              Authentication + server-side role-based access control
schema.sql            Full relational schema
seed.py               Reference data + default user bootstrap (runs automatically)
seed_demo.py           Optional, clearly-labelled demo dataset
helpers.py            Shared lookups, CSV/Excel export, hand-rolled SVG charts
blueprints/            One module per feature area (animals, weights, breeding, health, groups, tasks, reports, admin, auth)
templates/             Jinja2 templates (original design, no third-party UI kit)
static/                Hand-written CSS/JS, PWA manifest + service worker, icons
tests/                 Automated smoke test + screenshot QA script
```
