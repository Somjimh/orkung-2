from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
import db
import helpers
import auth

bp = Blueprint("tasks", __name__, url_prefix="/tasks")

TASK_TYPES = [
    ("expected_birth", "Expected birth"), ("pregnancy_check", "Pregnancy check"),
    ("vaccination", "Vaccination"), ("deworming", "Deworming / treatment"),
    ("follow_up", "Follow-up examination"), ("weaning", "Weaning"),
    ("weight_check", "Weight measurement"), ("identification", "Identification task"),
    ("withdrawal_end", "Withdrawal period completion"), ("incomplete_record", "Incomplete record"),
    ("general", "General"),
]


@bp.route("")
@auth.login_required
def index():
    status = request.args.get("status", "pending")
    ttype = request.args.get("type")
    assigned_to = request.args.get("assigned_to", type=int)

    where = ["1=1"]; args = []
    if status != "all":
        where.append("t.status=?"); args.append(status)
    if ttype:
        where.append("t.task_type=?"); args.append(ttype)
    if assigned_to:
        where.append("t.assigned_to=?"); args.append(assigned_to)

    rows = db.query(
        f"SELECT t.*, u.full_name AS assignee, c.full_name AS completer FROM tasks t "
        f"LEFT JOIN users u ON u.id=t.assigned_to LEFT JOIN users c ON c.id=t.completed_by "
        f"WHERE {' AND '.join(where)} ORDER BY (t.status='pending') DESC, t.due_date", tuple(args))

    return render_template("tasks_index.html", rows=rows, status=status, ttype=ttype, assigned_to=assigned_to,
                           task_types=TASK_TYPES, users=helpers.user_options(), today=db.today_str())


@bp.route("/add", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def add():
    title = request.form.get("title", "").strip()
    due_date = request.form.get("due_date")
    if not title or not due_date:
        flash("Title and due date are required.", "error")
        return redirect(url_for("tasks.index"))
    new_id = db.execute(
        "INSERT INTO tasks (task_type, title, description, due_date, assigned_to, priority, related_group_id, created_by) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (request.form.get("task_type", "general"), title, request.form.get("description"), due_date,
         request.form.get("assigned_to", type=int) or None, request.form.get("priority", "normal"),
         request.form.get("related_group_id", type=int) or None, g.user["id"]))
    db.audit(g.user, "create", "task", new_id, f"Created task: {title}")
    flash("Task created.", "success")
    return redirect(url_for("tasks.index"))


@bp.route("/<int:task_id>/complete", methods=["POST"])
@auth.login_required
def complete(task_id):
    task = db.query("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    if not task:
        abort(404)
    if g.user["role"] not in auth.STAFF + ("admin",):
        abort(403)
    db.execute("UPDATE tasks SET status='completed', completed_at=datetime('now'), completed_by=? WHERE id=?",
              (g.user["id"], task_id))
    db.audit(g.user, "status_change", "task", task_id, f"Task completed: {task['title']}")
    flash("Task marked complete and kept in the task history.", "success")
    return redirect(url_for("tasks.index", status=request.form.get("return_status", "pending")))


@bp.route("/<int:task_id>/cancel", methods=["POST"])
@auth.login_required
@auth.require_roles(*auth.MANAGEMENT)
def cancel(task_id):
    task = db.query("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    if not task:
        abort(404)
    db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
    db.audit(g.user, "status_change", "task", task_id, f"Task cancelled: {task['title']}")
    flash("Task cancelled.", "success")
    return redirect(url_for("tasks.index"))
