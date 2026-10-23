from __future__ import annotations

import csv
import hmac
import io
import json
import os
import secrets
import sqlite3
import hashlib
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def load_or_create_env() -> bool:
    """Create a private local .env file on first run and load its values."""
    generated = False
    if not ENV_FILE.exists():
        generated_password = secrets.token_urlsafe(16)
        generated_secret = secrets.token_hex(32)
        ENV_FILE.write_text(
            "# Local configuration. Keep this file private and do not commit it.\n"
            f"ADMIN_USERNAME=admin\nADMIN_PASSWORD={generated_password}\nSECRET_KEY={generated_secret}\nDATA_DIR=./data\n",
            encoding="utf-8",
        )
        try:
            ENV_FILE.chmod(0o600)
        except OSError:
            pass
        generated = True
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return generated


ENV_CREATED = load_or_create_env()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "forms_app.sqlite3"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=1 when deployed behind HTTPS.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

FIELD_TYPES = {"short_text", "paragraph", "email", "number", "date", "select", "radio", "checkbox"}


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_connection():
    conn = connect_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def default_form() -> dict[str, Any]:
    """Return the user's Health & Safety Concern form from the supplied screenshots."""
    return {
        "title": "Workplace Wellness TEST Form",
        "description": "Your Voice Matters! Help Us Improve Workplace Health & Safety",
        "submit_label": "Submit",
        "success_message": "Thank you. Your response has been recorded.",
        "fields": [
            {
                "id": "project_department_name",
                "label": "What is your Project Name/Department Name?",
                "type": "short_text",
                "required": True,
                "help_text": "",
                "options": [],
            },
            {
                "id": "sopra_steria_email_id",
                "label": "What is your Sopra Steria Email ID?",
                "type": "email",
                "required": True,
                "help_text": "",
                "options": [],
            },
            {
                "id": "safety_concern_details",
                "label": "Please provide details about the health & safety concern you mentioned in the previous question.",
                "type": "paragraph",
                "required": True,
                "help_text": "",
                "options": [],
            },
            {
                "id": "concern_category",
                "label": "Choose the category in which your health & safety concern belongs to:",
                "type": "radio",
                "required": True,
                "help_text": "",
                "options": [
                    "Facility & Infrastructure Safety (e.g., parking, lift safety)",
                    "Transport & Commute Safety Issues",
                    "Fire & Emergency Safety Issues",
                    "Hygiene & Sanitation Issues (Eg: Washroom Hygiene/Cafeteria/Kitchen Hygiene/Drinking Water Issues)",
                    "Workplace Environment & Ergonomics Issues (Eg: Workstation Ergonomics/Lighting & Noise Levels/Air Quality & Ventilation/Workplace Cleanliness)",
                    "Other",
                ],
            },
            {
                "id": "reported_before",
                "label": "Have you reported these concerns before?",
                "type": "radio",
                "required": True,
                "help_text": "",
                "options": ["Yes", "No"],
            },
            {
                "id": "previous_report_outcome",
                "label": "If yes, to whom and what was the outcome? (Optional)",
                "type": "paragraph",
                "required": False,
                "help_text": "",
                "options": [],
            },
            {
                "id": "additional_information",
                "label": "Is there anything else you would like to add? (Optional)",
                "type": "paragraph",
                "required": False,
                "help_text": "",
                "options": [],
            },
        ],
    }


def init_db() -> None:
    """Create tables and safely add ticket metadata to existing submissions."""
    with db_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS form_settings (id INTEGER PRIMARY KEY CHECK (id = 1), config_json TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, submitted_at TEXT NOT NULL, data_json TEXT NOT NULL, receipt_token TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ticket_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER NOT NULL, admin_username TEXT NOT NULL, comment TEXT NOT NULL, created_at TEXT NOT NULL)")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}
        additions = {
            "receipt_token": "TEXT",
            "ticket_id": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'Open'",
            "progress_update": "TEXT NOT NULL DEFAULT ''",
            "pending_reason": "TEXT NOT NULL DEFAULT ''",
            "closure_notes": "TEXT NOT NULL DEFAULT ''",
            "last_updated": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {name} {definition}")
        current = conn.execute("SELECT config_json FROM form_settings WHERE id = 1").fetchone()
        if not current:
            conn.execute("INSERT INTO form_settings (id, config_json) VALUES (1, ?)", (json.dumps(default_form(), ensure_ascii=False),))
        else:
            try:
                saved_form = json.loads(current["config_json"])
            except (ValueError, TypeError):
                saved_form = {}
            submitted = conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"]
            expected_form = default_form()
            saved_ids = [field.get("id") for field in saved_form.get("fields", []) if isinstance(field, dict)] if isinstance(saved_form, dict) else []
            expected_ids = [field["id"] for field in expected_form["fields"]]
            is_known_starter_form = isinstance(saved_form, dict) and (
                (not submitted and saved_form.get("title") == "New form" and not saved_form.get("fields"))
                or saved_form.get("title") == "Health & Safety Concern Reporting Form"
                or (saved_form.get("title") == expected_form["title"] and saved_ids != expected_ids)
            )
            if is_known_starter_form:
                conn.execute("UPDATE form_settings SET config_json = ? WHERE id = 1", (json.dumps(expected_form, ensure_ascii=False),))
        # Backfill IDs for older records without deleting or changing their answers.
        rows = conn.execute("SELECT id, submitted_at FROM submissions WHERE ticket_id IS NULL OR ticket_id = '' ORDER BY id ASC").fetchall()
        for row in rows:
            try:
                year = datetime.fromisoformat(row["submitted_at"].replace("Z", "+00:00")).year
            except (ValueError, TypeError):
                year = datetime.now(timezone.utc).year
            ticket_id = f"WW-{year}-{row['id']:04d}"
            conn.execute("UPDATE submissions SET ticket_id = ?, status = COALESCE(status, 'Open'), last_updated = COALESCE(last_updated, submitted_at) WHERE id = ?", (ticket_id, row["id"]))


def read_form() -> dict[str, Any]:
    with db_connection() as conn:
        row = conn.execute("SELECT config_json FROM form_settings WHERE id = 1").fetchone()
    try:
        form = json.loads(row["config_json"]) if row else default_form()
    except (ValueError, TypeError):
        form = default_form()
    if not isinstance(form, dict):
        form = default_form()
    return form


def is_admin() -> bool:
    return session.get("admin_user") == ADMIN_USERNAME


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_admin():
            if request.path.startswith("/admin/api/") or request.path.startswith("/admin/export"):
                return jsonify({"error": "Authentication required."}), 401
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def csrf_valid() -> bool:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


def normalize_form(incoming: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(incoming, dict):
        return None, "The form configuration must be an object."
    title = str(incoming.get("title", "")).strip()[:120]
    description = str(incoming.get("description", "")).strip()[:1000]
    submit_label = str(incoming.get("submit_label", "Submit response")).strip()[:50] or "Submit response"
    success_message = str(incoming.get("success_message", "Thank you. Your response has been recorded.")).strip()[:300]
    raw_fields = incoming.get("fields", [])
    if not title:
        return None, "Enter a form title."
    if not isinstance(raw_fields, list) or len(raw_fields) > 60:
        return None, "Add between 0 and 60 fields."
    fields: list[dict[str, Any]] = []
    ids: set[str] = set()
    for idx, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            return None, f"Field {idx + 1} is invalid."
        field_id = str(item.get("id", ""))[:50]
        label = str(item.get("label", "")).strip()[:160]
        field_type = str(item.get("type", "short_text"))
        if not field_id or field_id in ids:
            return None, f"Field {idx + 1} needs a unique identifier."
        if not label:
            return None, f"Enter a label for field {idx + 1}."
        if field_type not in FIELD_TYPES:
            return None, f"Field type for '{label}' is not supported."
        ids.add(field_id)
        required = bool(item.get("required", False))
        help_text = str(item.get("help_text", "")).strip()[:250]
        options: list[str] = []
        if field_type in {"select", "radio"}:
            raw_options = item.get("options", [])
            if not isinstance(raw_options, list):
                return None, f"Options for '{label}' are invalid."
            options = list(dict.fromkeys(str(x).strip()[:120] for x in raw_options if str(x).strip()))[:100]
            if len(options) < 1:
                return None, f"Add at least one option for '{label}'."
        fields.append({
            "id": field_id,
            "label": label,
            "type": field_type,
            "required": required,
            "help_text": help_text,
            "options": options,
        })
    return {
        "title": title,
        "description": description,
        "submit_label": submit_label,
        "success_message": success_message,
        "fields": fields,
    }, None


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/")
@app.get("/submit")
def index():
    return render_template("index.html")


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return jsonify({"name": "Health & Safety Concern Reporting", "short_name": "Safety Form", "start_url": "/submit", "display": "standalone", "background_color": "#f4f7f8", "theme_color": "#176b57", "description": "Submit a health and safety concern."}), 200, {"Content-Type": "application/manifest+json"}


@app.get("/service-worker.js")
def service_worker():
    return Response("self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));", mimetype="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/api/form")
def api_form():
    form = read_form()
    return jsonify({k: form.get(k) for k in ("title", "description", "submit_label", "success_message", "fields")})


@app.post("/api/submit")
def api_submit():
    form = read_form()
    if not form.get("fields"):
        return jsonify({"error": "This form is not configured yet. Please contact the administrator."}), 409
    if not request.is_json:
        return jsonify({"error": "Submit a valid form response."}), 415
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify({"error": "The response data is invalid."}), 400
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in form.get("fields", []):
        field_id = field["id"]
        field_type = field["type"]
        value = incoming.get(field_id, False if field_type == "checkbox" else "")
        if field_type == "checkbox":
            value = bool(value)
            if field.get("required") and not value:
                errors[field_id] = "Please check this box."
        else:
            if isinstance(value, (dict, list)):
                value = ""
            value = str(value).strip()[:5000]
            if field.get("required") and not value:
                errors[field_id] = "This field is required."
            elif value and field_type == "email" and ("@" not in value or value.startswith("@") or value.endswith("@") or "." not in value.rsplit("@", 1)[-1]):
                errors[field_id] = "Enter a valid email address."
            elif value and field_type == "number":
                try:
                    float(value)
                except ValueError:
                    errors[field_id] = "Enter a valid number."
            elif value and field_type in {"select", "radio"} and value not in field.get("options", []):
                errors[field_id] = "Choose one of the listed options."
        data[field_id] = value
    if errors:
        return jsonify({"error": "Please check the highlighted fields.", "field_errors": errors}), 400
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    receipt_token = secrets.token_urlsafe(32)
    with db_connection() as conn:
        cursor = conn.execute("INSERT INTO submissions (submitted_at, data_json, receipt_token, status, last_updated) VALUES (?, ?, ?, 'Open', ?)", (timestamp, json.dumps(data, ensure_ascii=False), hashlib.sha256(receipt_token.encode()).hexdigest(), timestamp))
        submission_id = cursor.lastrowid
        year = datetime.now(timezone.utc).year
        ticket_id = f"WW-{year}-{submission_id:04d}"
        conn.execute("UPDATE submissions SET ticket_id = ? WHERE id = ?", (ticket_id, submission_id))
    return jsonify({"ok": True, "message": form.get("success_message") or "Thank you. Your response has been recorded.", "ticket_id": ticket_id, "receipt_url": url_for("download_own_submission", token=receipt_token), "submitted_at": timestamp}), 201


@app.get("/submission/<token>/download.csv")
def download_own_submission(token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_connection() as conn:
        row = conn.execute("SELECT id, submitted_at, data_json FROM submissions WHERE receipt_token = ?", (token_hash,)).fetchone()
    if not row:
        return "Submission receipt not found or link is invalid.", 404
    form = read_form()
    data = json.loads(row["data_json"])
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Submission ID", row["id"]])
    writer.writerow(["Submitted at (UTC)", row["submitted_at"]])
    writer.writerow([])
    writer.writerow(["Question", "Answer"])
    for field in form.get("fields", []):
        writer.writerow([field["label"], data.get(field["id"], "")])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=health-safety-submission-{row['id']}.csv"})


@app.get("/admin/login")
def admin_login():
    if is_admin():
        return redirect(url_for("admin_dashboard"))
    return render_template("login.html", error=None)


@app.post("/admin/login")
def admin_login_post():
    username = str(request.form.get("username", ""))
    password = str(request.form.get("password", ""))
    if hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(password, ADMIN_PASSWORD):
        session.clear()
        session["admin_user"] = ADMIN_USERNAME
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("admin_dashboard"))
    return render_template("login.html", error="The username or password is incorrect."), 401


@app.post("/admin/logout")
@admin_required
def admin_logout():
    if not csrf_valid():
        return "Invalid security token. Refresh the page and try again.", 400
    session.clear()
    return redirect(url_for("index"))


@app.get("/admin")
@admin_required
def admin_dashboard():
    form = read_form()
    with db_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"]
    return render_template("admin.html", form=form, submission_count=count, csrf_token=session["csrf_token"])


@app.post("/admin/api/form")
@admin_required
def admin_save_form():
    if not csrf_valid():
        return jsonify({"error": "Invalid security token. Refresh the page and try again."}), 400
    incoming = request.get_json(silent=True)
    normalized, error = normalize_form(incoming)
    if error:
        return jsonify({"error": error}), 400
    with db_connection() as conn:
        conn.execute("UPDATE form_settings SET config_json = ? WHERE id = 1", (json.dumps(normalized, ensure_ascii=False),))
    return jsonify({"ok": True, "message": "Form saved successfully.", "form": normalized})


@app.get("/admin/api/submissions")
@admin_required
def admin_submissions():
    form = read_form()
    with db_connection() as conn:
        rows = conn.execute("SELECT id, submitted_at, data_json, ticket_id, status, progress_update, pending_reason, closure_notes, last_updated FROM submissions ORDER BY id DESC LIMIT 1000").fetchall()
    entries = []
    fields = form.get("fields", [])
    for row in rows:
        raw = json.loads(row["data_json"])
        answers = [{"label": f["label"], "value": raw.get(f["id"], "") } for f in fields]
        entries.append({"id": row["id"], "ticket_id": row["ticket_id"], "submitted_at": row["submitted_at"], "answers": answers, "status": row["status"] or "Open", "progress_update": row["progress_update"] or "", "pending_reason": row["pending_reason"] or "", "closure_notes": row["closure_notes"] or "", "last_updated": row["last_updated"] or row["submitted_at"], "search_text": " ".join(str(a["value"]) for a in answers)})
    return jsonify({"submissions": entries, "count": len(entries)})


@app.post("/admin/api/tickets/<int:submission_id>")
@admin_required
def admin_update_ticket(submission_id: int):
    if not csrf_valid():
        return jsonify({"error": "Invalid security token. Refresh the page and try again."}), 400
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify({"error": "Invalid ticket update."}), 400
    status = str(incoming.get("status", "")).strip()
    progress_update = str(incoming.get("progress_update", "")).strip()[:3000]
    pending_reason = str(incoming.get("pending_reason", "")).strip()[:2000]
    closure_notes = str(incoming.get("closure_notes", "")).strip()[:2000]
    if status not in {"Open", "In Progress", "Pending", "Closed"}:
        return jsonify({"error": "Choose a valid ticket status."}), 400
    if status == "Pending" and not pending_reason:
        return jsonify({"error": "A pending reason is required when status is Pending."}), 400
    if status == "Closed" and not closure_notes:
        return jsonify({"error": "Closure notes are required when status is Closed."}), 400
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with db_connection() as conn:
        cur = conn.execute("UPDATE submissions SET status = ?, progress_update = ?, pending_reason = ?, closure_notes = ?, last_updated = ? WHERE id = ?", (status, progress_update, pending_reason, closure_notes, timestamp, submission_id))
        if not cur.rowcount:
            return jsonify({"error": "Ticket not found."}), 404
    return jsonify({"ok": True, "message": "Ticket updated.", "last_updated": timestamp})


@app.post("/admin/api/tickets/<int:submission_id>/comments")
@admin_required
def admin_add_ticket_comment(submission_id: int):
    if not csrf_valid():
        return jsonify({"error": "Invalid security token. Refresh the page and try again."}), 400
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify({"error": "Enter a comment."}), 400
    comment = str(incoming.get("comment", "")).strip()
    if not comment:
        return jsonify({"error": "Comment cannot be empty."}), 400
    if len(comment) > 3000:
        return jsonify({"error": "Comment must be 3000 characters or fewer."}), 400

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    admin_username = str(session.get("admin_user", ADMIN_USERNAME))
    with db_connection() as conn:
        exists = conn.execute("SELECT id FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        if not exists:
            return jsonify({"error": "Ticket not found."}), 404
        conn.execute(
            "INSERT INTO ticket_comments (submission_id, admin_username, comment, created_at) VALUES (?, ?, ?, ?)",
            (submission_id, admin_username, comment, timestamp)
        )
        conn.execute(
            "UPDATE submissions SET last_updated = ? WHERE id = ?",
            (timestamp, submission_id)
        )
    return jsonify({"ok": True, "message": "Comment added.", "created_at": timestamp})

@app.get("/track")
def track_ticket_page():
    return render_template("track.html")


@app.post("/api/track")
def api_track_ticket():
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify({"error": "Enter your ticket ID and registered email."}), 400
    ticket_id = str(incoming.get("ticket_id", "")).strip().upper()[:40]
    email = str(incoming.get("email", "")).strip().casefold()[:254]
    if not ticket_id or not email:
        return jsonify({"error": "Ticket ID and registered email are required."}), 400
    with db_connection() as conn:
        rows = conn.execute("SELECT ticket_id, data_json, status, progress_update, pending_reason, closure_notes, last_updated, submitted_at FROM submissions WHERE ticket_id = ?", (ticket_id,)).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"])
        except (ValueError, TypeError):
            continue
        if str(data.get("sopra_steria_email_id", "")).strip().casefold() == email:
            with db_connection() as conn:
                comments = conn.execute("SELECT admin_username, comment, created_at FROM ticket_comments WHERE submission_id = (SELECT id FROM submissions WHERE ticket_id = ?) ORDER BY id ASC", (ticket_id,)).fetchall()
            return jsonify({"ticket": {"ticket_id": row["ticket_id"], "status": row["status"] or "Open", "progress_update": row["progress_update"] or "", "pending_reason": row["pending_reason"] or "", "closure_notes": row["closure_notes"] or "", "last_updated": row["last_updated"] or row["submitted_at"], "comments": [{"admin_username": c["admin_username"], "comment": c["comment"], "created_at": c["created_at"]} for c in comments]}})
    # Same message for unknown ticket and email mismatch to avoid disclosing ticket existence.
    return jsonify({"error": "We couldn't find a ticket matching those details. Check the ticket ID and registered email."}), 404


@app.get("/admin/export.csv")
@admin_required
def admin_export():
    form = read_form()
    with db_connection() as conn:
        rows = conn.execute("SELECT id, submitted_at, data_json FROM submissions ORDER BY id ASC").fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    fields = form.get("fields", [])
    writer.writerow(["Submission ID", "Submitted at (UTC)"] + [f["label"] for f in fields])
    for row in rows:
        data = json.loads(row["data_json"])
        values = [data.get(f["id"], "") for f in fields]
        writer.writerow([row["id"], row["submitted_at"]] + values)
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=form-submissions.csv"})


@app.post("/admin/api/clear-submissions")
@admin_required
def admin_clear_submissions():
    if not csrf_valid():
        return jsonify({"error": "Invalid security token."}), 400
    confirmation = (request.get_json(silent=True) or {}).get("confirm")
    if confirmation != "DELETE ALL SUBMISSIONS":
        return jsonify({"error": "Confirmation phrase does not match."}), 400
    with db_connection() as conn:
        conn.execute("DELETE FROM submissions")
    return jsonify({"ok": True, "message": "All submissions were deleted."})


init_db()

if __name__ == "__main__":
    print("\nForms Studio is starting")
    print("Open: http://127.0.0.1:5000")
    print("Admin: http://127.0.0.1:5000/admin")
    if ENV_CREATED:
        print(f"First-run admin username: {ADMIN_USERNAME}")
        print("First-run admin password is in the generated .env file beside app.py")
        print("Keep .env private; do not share or commit it.\n")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=False)

