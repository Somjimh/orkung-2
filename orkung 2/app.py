import os
from flask import Flask, g, render_template, send_from_directory, request
from config import Config
import db
import auth


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_object)

    db.init_app(app)
    with app.app_context():
        db.init_db(app)
        import seed
        seed.run_reference_seed()
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    @app.before_request
    def _load_user():
        auth.load_logged_in_user()

    @app.context_processor
    def inject_globals():
        pending_tasks = 0
        alerts = 0
        if g.get("user"):
            row = db.query(
                "SELECT COUNT(*) c FROM tasks WHERE status='pending' AND date(due_date) <= date('now', '+' || ? || ' days')",
                (app.config["UPCOMING_TASK_WINDOW_DAYS"],), one=True)
            pending_tasks = row["c"] if row else 0
        nav = auth.visible_sections(g.user["role"]) if g.get("user") else set()
        return dict(current_user=g.get("user"), role_labels=auth.ROLE_LABELS,
                    pending_tasks=pending_tasks, app_name="Orkung Livestock Manager", nav=nav)

    # Jinja helpers
    app.jinja_env.filters["age"] = db.age_display
    app.jinja_env.filters["fmtdate"] = lambda s: (s[:10] if s else "—")

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        if g.get("user") is None:
            from flask import abort
            abort(401)
        return send_from_directory(app.config["UPLOAD_DIR"], filename)

    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(os.path.join(app.root_path, "static"), "manifest.json")

    @app.route("/service-worker.js")
    def service_worker():
        resp = send_from_directory(os.path.join(app.root_path, "static", "js"), "service-worker.js")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="You do not have permission to view this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="That page could not be found."), 404

    # Blueprints
    from blueprints.auth_routes import bp as auth_bp
    from blueprints.dashboard import bp as dashboard_bp
    from blueprints.animals import bp as animals_bp
    from blueprints.weights import bp as weights_bp
    from blueprints.breeding import bp as breeding_bp
    from blueprints.health import bp as health_bp
    from blueprints.groups import bp as groups_bp
    from blueprints.tasks import bp as tasks_bp
    from blueprints.reports import bp as reports_bp
    from blueprints.admin import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(animals_bp)
    app.register_blueprint(weights_bp)
    app.register_blueprint(breeding_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(groups_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
