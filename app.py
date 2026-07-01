"""
JobHawk SaaS — Multi-user job search automation platform.
Email/password login + optional WebAuthn passkey authentication.
"""

import datetime as dt
import json
import logging
import os
import re
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user,
    login_required, login_user, logout_user,
)
from passlib.hash import bcrypt as _bcrypt
from apscheduler.schedulers.background import BackgroundScheduler

import models
import scrapers
import scorer
import mailer

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE    = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
LOGS    = BASE / "logs"
for _d in [UPLOADS, LOGS]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=LOGS / "jobhawk.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Flask setup ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

login_manager = LoginManager(app)
login_manager.login_view = "login_page"
login_manager.login_message = "Please sign in to continee."

# WebAuthn config — set RP_ID to your actual domain in production
RP_ID     = os.environ.get("RP_ID", "localhost")
RP_NAME   = "JobHawk"
WA_ORIGIN = os.environ.get("WA_ORIGIN", f"https://{RP_ID}")

models.init_db()

# ── Flask-Login user object ───────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, row: dict):
        self.id    = row["id"]
        self.email = row["email"]
        self.name  = row.get("name") or ""

@login_manager.user_loader
def load_user(uid):
    row = models.user_by_id(int(uid))
    return User(row) if row else None


# ── Scan state (in-memory, global) ───────────────────────────────────────────

_scan_state = {
    "running": False,
    "last_ran": None,
    "last_status": "Never run",
    "run_count": 0,
}
_scan_lock = threading.Lock()


# ── Background scan ───────────────────────────────────────────────────────────

def _run_scan():
    with _scan_lock:
        if _scan_state["running"]:
            return
        _scan_state["running"]     = True
        _scan_state["last_status"] = "Running…"

    try:
        log.info("Global scan started")
        raw_jobs = scrapers.fetch_all_jobs()

        user_ids = models.user_all_ids()
        for uid in user_ids:
            try:
                _process_user(uid, raw_jobs)
            except Exception as ue:
                log.exception("Error processing user %s: %s", uid, ue)

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _scan_lock:
            _scan_state["last_ran"]    = now
            _scan_state["last_status"] = f"Done — {len(raw_jobs)} jobs scraped, {now}"
            _scan_state["run_count"]  += 1
        log.info("Global scan done: %d raw jobs", len(raw_jobs))

    except Exception as e:
        log.exception("Scan failed: %s", e)
        with _scan_lock:
            _scan_state["last_status"] = f"Error: {e}"
    finally:
        with _scan_lock:
            _scan_state["running"] = False


def _process_user(uid: int, raw_jobs: list):
    """Score raw jobs against a user's profile and auto-apply."""
    import sqlite3
    user    = models.user_by_id(uid)
    profile = models.profile_get(uid)

    if not profile.get("resume_name"):
        return   # user hasn't finished onboarding yet

    enriched = scorer.enrich_jobs(raw_jobs, profile)
    min_score = int(profile.get("min_score") or 10)
    new_jobs_this_run = []  # ALL new matching jobs found this scan (for digest)

    for j in enriched:
        if j.get("match_score", 0) < min_score:
            continue
        jid = models.job_upsert(uid, j)

        # Check if this job is new (not seen in a previous scan)
        with models._db() as c:
            row = c.execute(
                "SELECT status FROM jobs WHERE id=? AND user_id=?", (jid, uid)
            ).fetchone()
        is_new = bool(row and row["status"] == "new")

        if is_new:
            new_jobs_this_run.append(j)

        # Auto-apply if contact email found and job is new
        if is_new and j.get("email_found"):
            sent = mailer.send_application(
                j, user, profile, profile.get("resume_path")
            )
            if sent:
                models.job_mark_applied(uid, jid, j.get("email_found"))
            else:
                models.job_mark_applied(uid, jid)
        elif is_new:
            # No contact email — still mark as tracked so it's not re-processed
            models.job_mark_applied(uid, jid)

    # Send digest for ALL new jobs found this run (not just emailed ones)
    if new_jobs_this_run and user.get("notify_email", 1):
        mailer.notify_user_digest(user, profile, new_jobs_this_run)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_post():
    email    = (request.form.get("email") or "").lower().strip()
    password = request.form.get("password") or ""
    row      = models.user_by_email(email)
    if not row or not row.get("password_hash"):
        return render_template("login.html", error="Invalid email or password.")
    if not _bcrypt.verify(password, row["password_hash"]):
        return render_template("login.html", error="Invalid email or password.")
    login_user(User(row), remember=True)
    return redirect(url_for("dashboard"))


@app.route("/signup")
def signup_page():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/signup", methods=["POST"])
def signup_post():
    name     = (request.form.get("name") or "").strip()
    email    = (request.form.get("email") or "").lower().strip()
    password = request.form.get("password") or ""
    if not email or not password or len(password) < 8:
        return render_template(
            "signup.html",
            error="Email and a password of at least 8 characters are required."
        )
    if models.user_by_email(email):
        return render_template("signup.html", error="That email is already registered.")
    ph  = _bcrypt.hash(password)
    uid = models.user_create(email, ph, name)
    row = models.user_by_id(uid)
    login_user(User(row), remember=True)
    return redirect(url_for("onboard_page"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


# ── Onboarding ────────────────────────────────────────────────────────────────

@app.route("/onboard")
@login_required
def onboard_page():
    profile = models.profile_get(current_user.id)
    return render_template("onboard.html", profile=profile, user=current_user)


@app.route("/onboard", methods=["POST"])
@login_required
def onboard_post():
    roles_raw    = request.form.get("target_roles", "")
    keywords_raw = request.form.get("keywords", "")
    exclude_raw  = request.form.get("exclude_terms", "")

    def split(s):
        return [x.strip() for x in re.split(r"[,\n]+", s) if x.strip()]

    models.profile_update(
        current_user.id,
        location=request.form.get("location", "").strip(),
        target_roles=split(roles_raw),
        keywords=split(keywords_raw),
        exclude_terms=split(exclude_raw),
        min_score=int(request.form.get("min_score") or 10),
        email_from=request.form.get("email_from", "").strip(),
        email_password=request.form.get("email_password", ""),
        smtp_host=request.form.get("smtp_host", "smtp.gmail.com").strip(),
        smtp_port=int(request.form.get("smtp_port") or 587),
    )
    return redirect(url_for("dashboard"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    profile = models.profile_get(current_user.id)
    passkeys = models.passkeys_for_user(current_user.id)
    return render_template(
        "dashboard.html",
        user=current_user,
        profile=profile,
        passkeys=passkeys,
        rp_id=RP_ID,
    )


# ── User API ──────────────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    uid  = current_user.id
    s    = db_stats = models.db_stats(uid)
    profile = models.profile_get(uid)
    with _scan_lock:
        sc = dict(_scan_state)
    return jsonify({
        **s,
        **sc,
        "resume_name":       profile.get("resume_name"),
        "cover_letter_name": profile.get("cover_letter_name"),
        "email_configured":  bool(profile.get("email_from") and profile.get("email_password")),
    })


@app.route("/api/jobs")
@login_required
def api_jobs():
    return jsonify(models.jobs_all(current_user.id))


@app.route("/api/applied")
@login_required
def api_applied():
    return jsonify(models.jobs_applied(current_user.id))


@app.route("/api/interviews")
@login_required
def api_interviews():
    return jsonify(models.jobs_interviews(current_user.id))


@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    threading.Thread(target=_run_scan, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/download")
@login_required
def api_download():
    import csv, io
    jobs = models.jobs_all(current_user.id)
    if not jobs:
        return jsonify({"error": "No jobs yet"}), 404
    fields = ["title", "company", "location", "source", "match_score",
              "status", "url", "applied_at"]
    output = io.StringIO()
    w = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(jobs)
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobhawk_results.csv"},
    )


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f    = request.files["file"]
    ftype = request.form.get("type", "resume")
    safe  = re.sub(r"[^a-zA-Z0-9._-]", "_", f.filename)
    dest  = UPLOADS / f"{current_user.id}_{ftype}_{safe}"
    f.save(str(dest))

    # Parse resume to extract skills
    parsed_skills = []
    if ftype == "resume":
        result = scorer.parse_resume(str(dest))
        parsed_skills = result.get("skills", [])
        models.profile_update(
            current_user.id,
            resume_path=str(dest),
            resume_name=f.filename,
            parsed_skills=parsed_skills,
        )
    else:
        models.profile_update(
            current_user.id,
            cover_letter_path=str(dest),
            cover_letter_name=f.filename,
        )

    return jsonify({
        "ok": True,
        "name": f.filename,
        "type": ftype,
        "skills_found": len(parsed_skills),
    })


@app.route("/api/job/status", methods=["POST"])
@login_required
def api_job_status():
    data   = request.get_json(force=True) or {}
    jid    = data.get("id", "")
    status = data.get("status", "")
    notes  = data.get("notes", "")
    if not jid or not status:
        return jsonify({"ok": False}), 400
    models.job_update_status(current_user.id, jid, status, notes)
    # Send interview notification if applicable
    if status == "interview":
        user    = models.user_by_id(current_user.id)
        profile = models.profile_get(current_user.id)
        job = next((j for j in models.jobs_all(current_user.id) if j.get("id") == jid), {})
        if job:
            mailer.notify_interview(user, job)
    return jsonify({"ok": True})


@app.route("/api/profile", methods=["GET"])
@login_required
def api_profile_get():
    return jsonify(models.profile_get(current_user.id))


@app.route("/api/profile", methods=["POST"])
@login_required
def api_profile_post():
    data = request.get_json(force=True) or {}
    allowed = {
        "location", "target_roles", "keywords", "exclude_terms",
        "min_score", "email_from", "email_password", "smtp_host", "smtp_port",
    }
    update = {k: v for k, v in data.items() if k in allowed}
    if update:
        models.profile_update(current_user.id, **update)
    return jsonify({"ok": True})


# ── WebAuthn (Passkeys) ───────────────────────────────────────────────────────

def _wa_available():
    try:
        import webauthn  # noqa: F401
        return True
    except ImportError:
        return False


@app.route("/auth/passkey/register/begin", methods=["POST"])
@login_required
def passkey_register_begin():
    if not _wa_available():
        return jsonify({"error": "WebAuthn not installed"}), 500
    import webauthn
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    uid   = current_user.id
    user  = models.user_by_id(uid)
    existing = [
        webauthn.helpers.structs.PublicKeyCredentialDescriptor(id=pk["credential_id"])
        for pk in models.passkeys_for_user(uid)
    ]
    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(uid).encode(),
        user_name=user["email"],
        user_display_name=user.get("name") or user["email"],
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    session["wa_reg_challenge"] = options.challenge
    return jsonify(json.loads(webauthn.options_to_json(options)))


@app.route("/auth/passkey/register/complete", methods=["POST"])
@login_required
def passkey_register_complete():
    if not _wa_available():
        return jsonify({"error": "WebAuthn not installed"}), 500
    import webauthn
    from webauthn.helpers.structs import RegistrationCredential
    challenge = session.pop("wa_reg_challenge", None)
    if not challenge:
        return jsonify({"error": "No challenge in session"}), 400
    try:
        cred = RegistrationCredential.parse_raw(json.dumps(request.get_json(force=True)))
        verification = webauthn.verify_registration_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=WA_ORIGIN,
        )
        models.passkey_store(
            current_user.id,
            credential_id=verification.credential_id,
            public_key=verification.credential_public_key,
            label=request.json.get("label", "Passkey"),
        )
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("Passkey register failed: %s", e)
        return jsonify({"error": str(e)}), 400


@app.route("/auth/passkey/authenticate/begin", methods=["POST"])
def passkey_authenticate_begin():
    if not _wa_available():
        return jsonify({"error": "WebAuthn not installed"}), 500
    import webauthn
    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        user_verification=webauthn.helpers.structs.UserVerificationRequirement.PREFERRED,
    )
    session["wa_auth_challenge"] = options.challenge
    return jsonify(json.loads(webauthn.options_to_json(options)))


@app.route("/auth/passkey/authenticate/complete", methods=["POST"])
def passkey_authenticate_complete():
    if not _wa_available():
        return jsonify({"error": "WebAuthn not installed"}), 500
    import webauthn
    from webauthn.helpers.structs import AuthenticationCredential
    challenge = session.pop("wa_auth_challenge", None)
    if not challenge:
        return jsonify({"error": "No challenge"}), 400
    try:
        data = request.get_json(force=True)
        cred = AuthenticationCredential.parse_raw(json.dumps(data))
        # Lookup stored passkey by raw credential id bytes
        raw_id = webauthn.helpers.base64url_to_bytes(data["rawId"])
        pk     = models.passkey_by_credential_id(raw_id)
        if not pk:
            return jsonify({"error": "Passkey not found"}), 404
        verification = webauthn.verify_authentication_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=WA_ORIGIN,
            credential_public_key=pk["public_key"],
            credential_current_sign_count=pk["sign_count"],
        )
        models.passkey_update_sign_count(raw_id, verification.new_sign_count)
        user_row = models.user_by_id(pk["user_id"])
        if not user_row:
            return jsonify({"error": "User not found"}), 404
        login_user(User(user_row), remember=True)
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("Passkey auth failed: %s", e)
        return jsonify({"error": str(e)}), 400


# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(_run_scan, "interval", minutes=15, id="global_scan")
scheduler.start()

# Run one scan immediately on startup (in background)
threading.Thread(target=_run_scan, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
