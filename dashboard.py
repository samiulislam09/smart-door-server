"""Dashboard blueprint: login, the dashboard page, JSON APIs, snapshot serving, and
owner re-enroll. Kept separate from the core door API in server.py.

Auth is a single shared password (DASHBOARD_PASSWORD env). Only these routes (and the
camera view) are protected; /verify stays open for the ESP32.
"""
import os
from functools import wraps

from flask import (Blueprint, request, session, redirect, url_for,
                   render_template, jsonify, send_from_directory, abort)

import db

bp = Blueprint("dashboard", __name__)

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("dashboard.login"))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/")
def index():
    return redirect(url_for("dashboard.home"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect(url_for("dashboard.home"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard.login"))


@bp.route("/dashboard")
@login_required
def home():
    return render_template("dashboard.html", users=db.list_users())


@bp.route("/api/events")
@login_required
def api_events():
    limit = min(int(request.args.get("limit", 50)), 200)
    verdict = request.args.get("verdict") or None
    return jsonify(db.query_events(limit=limit, verdict=verdict))


@bp.route("/api/stats")
@login_required
def api_stats():
    days = min(max(int(request.args.get("days", 7)), 1), 90)
    return jsonify(db.stats(days=days))


@bp.route("/snapshots/<path:fname>")
@login_required
def snapshot(fname):
    return send_from_directory(db.SNAPSHOT_DIR, fname)


@bp.route("/api/users")
@login_required
def api_users():
    return jsonify(db.list_users())


@bp.route("/user_photos/<int:pid>")
@login_required
def user_photo(pid):
    row = db.get_photo(pid)
    if not row:
        abort(404)
    return send_from_directory(str(db.ASSETS_USERS_DIR), row["image_path"],
                               mimetype="image/jpeg")


@bp.route("/users", methods=["POST"])
@login_required
def users_create():
    import server
    file = request.files.get("photo")
    if not file:
        return redirect(url_for("dashboard.home", msg="No file selected"))
    ok, message = server.enroll_user(request.form.get("name", ""), file.read())
    return redirect(url_for("dashboard.home", msg=message))


@bp.route("/users/<int:uid>/photos", methods=["POST"])
@login_required
def users_add_photo(uid):
    import server
    file = request.files.get("photo")
    if not file:
        return redirect(url_for("dashboard.home", msg="No file selected"))
    ok, message = server.add_user_photo(uid, file.read())
    return redirect(url_for("dashboard.home", msg=message))


@bp.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
def users_delete(uid):
    import server
    db.delete_user(uid)
    server.init_owners()
    return redirect(url_for("dashboard.home", msg="User removed."))


@bp.route("/photos/<int:pid>/delete", methods=["POST"])
@login_required
def photo_delete(pid):
    import server
    db.delete_photo(pid)
    server.init_owners()
    return redirect(url_for("dashboard.home", msg="Photo removed."))
