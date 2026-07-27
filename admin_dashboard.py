"""Authenticated monitoring and control dashboard for the email scheduler."""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from monitoring import COMMAND_ACTIONS, MonitoringStore, attach_database_logging


PROJECT_DIR = Path(__file__).resolve().parent
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict[str, list[float]] = {}
_login_lock = Lock()


def _required_environment(name: str, testing: bool) -> str:
    value = os.getenv(name, "")
    if not value and not testing:
        raise RuntimeError(f"Required dashboard environment variable {name} is not set")
    return value or f"test-{name.lower()}"


def _login_allowed(address: str) -> bool:
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    with _login_lock:
        attempts = [value for value in _login_attempts.get(address, []) if value > cutoff]
        _login_attempts[address] = attempts
        return len(attempts) < LOGIN_MAX_ATTEMPTS


def _record_login_failure(address: str) -> None:
    with _login_lock:
        _login_attempts.setdefault(address, []).append(time.monotonic())


def _clear_login_failures(address: str) -> None:
    with _login_lock:
        _login_attempts.pop(address, None)


def _worker_health(state: dict | None, max_age: float = 180) -> dict[str, str]:
    if not state:
        return {"status": "down", "label": "No worker heartbeat"}
    try:
        updated_at = datetime.fromisoformat(state["updated_at"])
        age = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError):
        return {"status": "down", "label": "Invalid heartbeat"}
    if age > max_age:
        return {"status": "down", "label": f"Heartbeat stale ({age:.0f}s)"}
    worker_state = state.get("state", "unknown")
    if worker_state == "paused":
        return {"status": "warning", "label": "Scheduler paused"}
    if worker_state in {"running", "sending"}:
        return {"status": "up", "label": f"Worker {worker_state}"}
    return {"status": "down", "label": f"Worker {worker_state}"}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    testing = bool(test_config and test_config.get("TESTING"))
    secret_key = (test_config or {}).get("SECRET_KEY") or _required_environment(
        "ADMIN_SECRET_KEY", testing
    )
    admin_password = (test_config or {}).get("ADMIN_PASSWORD") or _required_environment(
        "ADMIN_PASSWORD", testing
    )
    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    database_url = (test_config or {}).get("DATABASE_URL")
    if not testing and len(admin_password) < 12:
        raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters")
    if not testing and len(secret_key) < 32:
        raise RuntimeError("ADMIN_SECRET_KEY must contain at least 32 characters")

    app.config.update(
        SECRET_KEY=secret_key,
        TESTING=testing,
        ADMIN_PASSWORD=admin_password,
        ADMIN_USERNAME=admin_username,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")) and not testing,
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    store = MonitoringStore(database_url)
    store.initialize()
    app.extensions["monitoring_store"] = store
    app.logger.setLevel(logging.INFO)
    attach_database_logging(app.logger, store)

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.template_filter("display_time")
    def display_time(value):
        if not value:
            return "—"
        try:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo:
                parsed = parsed.astimezone()
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.path != "/static/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def authenticate_and_protect():
        public_endpoints = {"login", "healthz", "static"}
        if request.endpoint not in public_endpoints and not session.get("authenticated"):
            return redirect(url_for("login", next=request.full_path))
        if request.method == "POST":
            supplied = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                abort(400, "Invalid CSRF token")
        return None

    def logged_in(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            address = request.remote_addr or "unknown"
            if not _login_allowed(address):
                app.logger.warning("Dashboard login rate-limited address=%s", address)
                return render_template("login.html", rate_limited=True), 429
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            valid_user = hmac.compare_digest(username, app.config["ADMIN_USERNAME"])
            valid_password = hmac.compare_digest(password, app.config["ADMIN_PASSWORD"])
            if valid_user and valid_password:
                _clear_login_failures(address)
                session.clear()
                session["authenticated"] = True
                session["username"] = username
                session.permanent = True
                store.record_event("INFO", "admin.auth", "Admin login succeeded")
                return redirect(url_for("dashboard"))
            _record_login_failure(address)
            store.record_event(
                "WARNING", "admin.auth", f"Admin login failed from {address}"
            )
            flash("Invalid username or password.", "error")
        return render_template("login.html", rate_limited=False)

    @app.post("/logout")
    @logged_in
    def logout():
        username = session.get("username", "admin")
        store.record_event("INFO", "admin.auth", f"Admin logout: {username}")
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @logged_in
    def dashboard():
        state = store.get_state()
        events, _ = store.list_events(per_page=12)
        commands = store.list_commands(20)
        config = {}
        if state and state.get("config_json"):
            try:
                config = json.loads(state["config_json"])
            except (TypeError, json.JSONDecodeError):
                config = {}
        latest_smtp_check = next(
            (item for item in commands if item["action"] == "check_smtp"), None
        )
        attachment_exists = bool(
            config.get("attachment") and Path(config["attachment"]).is_file()
        )
        modules = [
            {"name": "Worker", **_worker_health(state)},
            {
                "name": "Database",
                "status": "up",
                "label": f"{store.backend_name} connected",
            },
            {
                "name": "Scheduler",
                "status": "warning" if state and state.get("paused") else "up",
                "label": "Paused" if state and state.get("paused") else "Active",
            },
            {
                "name": "SMTP",
                "status": (
                    "up"
                    if latest_smtp_check
                    and latest_smtp_check["status"] == "completed"
                    else "neutral"
                ),
                "label": (
                    latest_smtp_check["result"]
                    if latest_smtp_check
                    else "Use Check SMTP for a live test"
                ),
            },
            {
                "name": "Attachment",
                "status": "up" if attachment_exists else "neutral",
                "label": "Available" if attachment_exists else "Awaiting worker check",
            },
            {
                "name": "Commands",
                "status": "up",
                "label": f"{store.dashboard_stats()['pending_commands']} pending",
            },
        ]
        return render_template(
            "dashboard.html",
            state=state,
            worker_health=_worker_health(state),
            stats=store.dashboard_stats(),
            events=events,
            deliveries=store.list_deliveries(8),
            commands=commands[:8],
            modules=modules,
            config=config,
        )

    @app.get("/logs")
    @logged_in
    def logs():
        page = request.args.get("page", 1, type=int)
        per_page = 100
        level = request.args.get("level", "")[:20]
        module = request.args.get("module", "")[:100]
        search = request.args.get("search", "")[:200]
        events, total = store.list_events(
            page=page,
            per_page=per_page,
            level=level,
            module=module,
            search=search,
        )
        return render_template(
            "logs.html",
            events=events,
            total=total,
            page=max(page, 1),
            pages=max((total + per_page - 1) // per_page, 1),
            levels=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            modules=store.event_modules(),
            filters={"level": level, "module": module, "search": search},
        )

    @app.get("/logs.csv")
    @logged_in
    def download_logs():
        level = request.args.get("level", "")[:20]
        module = request.args.get("module", "")[:100]
        search = request.args.get("search", "")[:200]

        def generate_csv():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "created_at", "level", "module", "message"])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
            for event in store.iter_events(
                level=level, module=module, search=search
            ):
                message = str(event["message"])
                if message.startswith(("=", "+", "-", "@")):
                    message = "'" + message
                writer.writerow(
                    [
                        event["id"],
                        event["created_at"],
                        event["level"],
                        event["module"],
                        message,
                    ]
                )
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
        return Response(
            generate_csv(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=scheduler-logs.csv"},
        )

    @app.get("/deliveries")
    @logged_in
    def deliveries():
        return render_template(
            "deliveries.html", deliveries=store.list_deliveries(500)
        )

    @app.get("/commands")
    @logged_in
    def commands():
        return render_template("commands.html", commands=store.list_commands(500))

    @app.post("/commands/<int:command_id>/cancel")
    @logged_in
    def cancel_command(command_id: int):
        username = session.get("username", "admin")
        cancelled = store.cancel_command(command_id, username)
        if cancelled:
            store.record_event(
                "INFO",
                "admin.actions",
                f"Cancelled queued command #{command_id} by {username}",
            )
            flash(f"Command #{command_id} cancelled.", "success")
        else:
            flash("Only queued commands can be cancelled.", "error")
        return redirect(url_for("commands"))

    @app.post("/actions/<action>")
    @logged_in
    def queue_action(action: str):
        if action not in COMMAND_ACTIONS:
            abort(404)
        username = session.get("username", "admin")
        try:
            command_id = store.queue_command(action, username)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("commands"))
        store.record_event(
            "INFO",
            "admin.actions",
            f"Queued {action} command #{command_id} by {username}",
        )
        flash(f"Action queued: {action.replace('_', ' ')} (#{command_id})", "success")
        target = request.referrer or ""
        parsed_target = urlsplit(target)
        if parsed_target.scheme and parsed_target.scheme not in {"http", "https"}:
            target = ""
        elif parsed_target.netloc and parsed_target.netloc != request.host:
            target = ""
        return redirect(target or url_for("dashboard"))

    @app.get("/api/status")
    @logged_in
    def api_status():
        state = store.get_state()
        return jsonify(
            {
                "state": state,
                "worker_health": _worker_health(state),
                "stats": store.dashboard_stats(),
            }
        )

    @app.get("/healthz")
    def healthz():
        healthy, message = store.healthcheck()
        return jsonify({"healthy": healthy, "message": message}), 200 if healthy else 503

    return app


if __name__ == "__main__":
    create_app().run(
        host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False
    )
