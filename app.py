"""
JobHawk SaaS — Multi-user job search automation platform.
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
    Flask, Response, jsonify, redirect, render_template,
    request, session, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user,
    login_required, login_user, logout_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

import models
import scrapers
import scorer
import mailer

load_dotenv()

BASE    = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
LOGS    = BASE / "logs"
for _d in [UPLOADS, LOGS]:
    _d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOGS / "jobhawk.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
# SECRET_KEY MUST be set as an env var on Render for sessions to persist across restarts.
# Without it, a new random key is generated on each deploy, logging out all users.
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["REMEMBER_COOKIE_DURATION"] = dt.timedelta(days=30)

login_manager = LoginManager(app)
login_manager.login_view  = "login_page"
login_manager.login_message = "Please sign in to continue."

RP_ID     = os.environ.get("RP_ID", "localhost")
RP_NAME   = "JobHawk"
WA_ORIGIN = os.environ.get("WA_ORIGIN", f"https://{RP_ID}")

models.init_db()

# ── Flask-Login ───────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, row):
        self.id    = row["id"]
        self.email = row["email"]
        self.name  = row.get("name") or ""

@login_manager.user_loader
def load_user(uid):
    row = models.user_by_id(int(uid))
    return User(row) if row else None

# ── Scan state ────────────────────────────────────────────────────────────────

_scan_state = {
    "running": False, "last_ran": None,
    "last_status": "Never run", "run_count": 0,
}
_scan_lock  = threading.Lock()
_stop_event = threading.Event()    # set() to request scan cancellation

MAX_APPLY_PER_RUN = 30   # cap applications per scan to avoid SMTP hangs

# ── Background scan ───────────────────────────────────────────────────────────

def _run_scan():
    with _scan_lock:
        if _scan_state["running"]:
            return
        _scan_state["running"]     = True
        _scan_state["last_status"] = "Fetching jobs..."
    _stop_event.clear()
    try:
        log.info("Scan started")
        raw_jobs = scrapers.fetch_all_jobs()
        log.info("Fetched %d raw jobs — processing users", len(raw_jobs))
        with _scan_lock:
            _scan_state["last_status"] = f"Scoring {len(raw_jobs)} jobs..."
        for uid in models.user_all_ids():
            if _stop_event.is_set():
                log.info("Scan cancelled by user")
                break
            try:
                _process_user(uid, raw_jobs)
            except Exception as ue:
                log.exception("User %s error: %s", uid, ue)
        stopped = _stop_event.is_set()
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _scan_lock:
            _scan_state["last_ran"]    = now
            _scan_state["last_status"] = (
                f"Stopped at {now}" if stopped else f"Done — {len(raw_jobs)} jobs found"
            )
            _scan_state["run_count"]  += 1
        log.info("Scan %s: %d jobs", "stopped" if stopped else "done", len(raw_jobs))
    except Exception as e:
        log.exception("Scan error: %s", e)
        with _scan_lock:
            _scan_state["last_status"] = f"Error: {e}"
    finally:
        with _scan_lock:
            _scan_state["running"] = False


def _process_user(uid, raw_jobs):
    user       = models.user_by_id(uid)
    profile    = models.profile_get(uid)
    has_resume = bool(profile.get("resume_name"))
    enriched   = scorer.enrich_jobs(raw_jobs, profile)
    min_score  = int(profile.get("min_score") or 10)
    applied    = []
    apply_cap  = MAX_APPLY_PER_RUN

    for j in enriched:
        if j.get("match_score", 0) < min_score:
            continue
        jid = models.job_upsert(uid, j)   # always store — even without a resume
        if not has_resume or apply_cap <= 0:
            continue   # skip auto-apply if no resume or cap hit
        with models._db() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=? AND user_id=?",
                            (jid, uid)).fetchone()
        if row and row["status"] == "new":
            sent = mailer.send_application(j, user, profile, profile.get("resume_path"))
            models.job_mark_applied(uid, jid, j.get("email_found") if sent else None)
            if sent:
                applied.append(j)
                apply_cap -= 1

    if applied and user.get("notify_email"):
        # Try platform email first, fall back to user's own SMTP credentials
        p_email = os.environ.get("EMAIL_FROM", "")
        p_pass  = os.environ.get("EMAIL_PASSWORD", "")
        if not p_email or not p_pass:
            p_email = profile.get("email_from", "")
            p_pass  = profile.get("email_password", "")
        if p_email and p_pass:
            mailer.notify_user_digest(user, profile, applied, p_email, p_pass)


# ── Keep-alive (prevents Render free tier from spinning down) ─────────────────

def _keep_alive():
    try:
        import requests as _req
        host = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        if host:
            _req.get(f"{host}/ping", timeout=5)
            log.info("Keep-alive ping sent to %s", host)
    except Exception as e:
        log.debug("Keep-alive ping failed: %s", e)

@app.route("/ping")
def ping():
    return "ok", 200

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return redirect(url_for("dashboard") if current_user.is_authenticated else url_for("login_page"))

@app.route("/login", methods=["GET"])
def login_page():
    return redirect(url_for("dashboard")) if current_user.is_authenticated else render_template("login.html")

@app.route("/login", methods=["POST"])
def login_post():
    email    = (request.form.get("email") or "").lower().strip()
    password = request.form.get("password") or ""
    row      = models.user_by_email(email)
    if not row or not row.get("password_hash") or not check_password_hash(row["password_hash"], password):
        return render_template("login.html", error="Invalid email or password.")
    login_user(User(row), remember=True)
    return redirect(url_for("dashboard"))

@app.route("/signup", methods=["GET"])
def signup_page():
    return redirect(url_for("dashboard")) if current_user.is_authenticated else render_template("signup.html")

@app.route("/signup", methods=["POST"])
def signup_post():
    name     = (request.form.get("name") or "").strip()
    email    = (request.form.get("email") or "").lower().strip()
    password = request.form.get("password") or ""
    if not email or not password or len(password) < 8:
        return render_template("signup.html",
            error="Email and a password of at least 8 characters are required.")
    if models.user_by_email(email):
        return render_template("signup.html", error="That email is already registered.")
    uid = models.user_create(email, generate_password_hash(password), name)
    login_user(User(models.user_by_id(uid)), remember=True)
    return redirect(url_for("onboard_page"))

@app.route("/logout")
@login_required
def logout():
    logout_user(); return redirect(url_for("login_page"))

# ── Onboarding ────────────────────────────────────────────────────────────────

@app.route("/onboard", methods=["GET"])
@login_required
def onboard_page():
    return render_template("onboard.html",
        profile=models.profile_get(current_user.id), user=current_user)

@app.route("/onboard", methods=["POST"])
@login_required
def onboard_post():
    def split(s):
        return [x.strip() for x in re.split(r"[,\n]+", s or "") if x.strip()]
    country  = request.form.get("country", "").strip()
    province = request.form.get("province", "").strip()
    city     = request.form.get("city", "").strip()
    # Build legacy location string for backward compat with scoring
    location = ", ".join(p for p in [city, province, country] if p)
    models.profile_update(current_user.id,
        country       = country,
        province      = province,
        city          = city,
        location      = location,
        target_roles  = split(request.form.get("target_roles","")),
        keywords      = split(request.form.get("keywords","")),
        exclude_terms = split(request.form.get("exclude_terms","")),
        min_score     = int(request.form.get("min_score") or 10),
        email_from    = request.form.get("email_from","").strip(),
        email_password= request.form.get("email_password",""),
        smtp_host     = request.form.get("smtp_host","smtp.gmail.com").strip(),
        smtp_port     = int(request.form.get("smtp_port") or 587),
    )
    return redirect(url_for("dashboard"))

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html",
        user=current_user,
        profile=models.profile_get(current_user.id),
        passkeys=models.passkeys_for_user(current_user.id),
        rp_id=RP_ID)

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    uid   = current_user.id
    stats = models.db_stats(uid)
    prof  = models.profile_get(uid)
    with _scan_lock:
        sc = dict(_scan_state)
    return jsonify({**stats, **sc,
        "resume_name":       prof.get("resume_name"),
        "cover_letter_name": prof.get("cover_letter_name"),
        "email_configured":  bool(prof.get("email_from") and prof.get("email_password")),
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
    fields = ["title","company","location","source","match_score","status","url","applied_at"]
    out = io.StringIO()
    w   = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader(); w.writerows(jobs); out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobhawk_results.csv"})

@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f     = request.files["file"]
    ftype = request.form.get("type","resume")
    safe  = re.sub(r"[^a-zA-Z0-9._-]", "_", f.filename)
    dest  = UPLOADS / f"{current_user.id}_{ftype}_{safe}"
    f.save(str(dest))
    parsed_skills = []
    if ftype == "resume":
        result = scorer.parse_resume(str(dest))
        parsed_skills = result.get("skills", [])
        models.profile_update(current_user.id,
            resume_path=str(dest), resume_name=f.filename, parsed_skills=parsed_skills)
    else:
        models.profile_update(current_user.id,
            cover_letter_path=str(dest), cover_letter_name=f.filename)
    return jsonify({"ok": True, "name": f.filename, "type": ftype,
                    "skills_found": len(parsed_skills)})

@app.route("/api/job/status", methods=["POST"])
@login_required
def api_job_status():
    data   = request.get_json(force=True) or {}
    jid    = data.get("id","")
    status = data.get("status","")
    if not jid or not status:
        return jsonify({"ok": False}), 400
    models.job_update_status(current_user.id, jid, status, data.get("notes",""))
    if status == "interview":
        user = models.user_by_id(current_user.id)
        for j in models.jobs_all(current_user.id):
            if j.get("id") == jid:
                mailer.notify_interview(user, j); break
    return jsonify({"ok": True})

@app.route("/api/profile", methods=["GET"])
@login_required
def api_profile_get():
    return jsonify(models.profile_get(current_user.id))

@app.route("/api/profile", methods=["POST"])
@login_required
def api_profile_post():
    data = request.get_json(force=True) or {}
    allowed = {"country","province","city","location","target_roles","keywords",
               "exclude_terms","min_score","email_from","email_password",
               "smtp_host","smtp_port"}
    update = {k: v for k, v in data.items() if k in allowed}
    if update:
        models.profile_update(current_user.id, **update)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    _stop_event.set()
    with _scan_lock:
        _scan_state["last_status"] = "Stopping..."
    return jsonify({"ok": True})

# ── Passkeys ──────────────────────────────────────────────────────────────────

def _wa():
    try:
        import webauthn; return webauthn
    except ImportError:
        return None

def _parse_cred(cls, data):
    raw = json.dumps(data)
    try:
        return cls.model_validate_json(raw)
    except AttributeError:
        return cls.parse_raw(raw)

@app.route("/auth/passkey/register/begin", methods=["POST"])
@login_required
def passkey_register_begin():
    wa = _wa()
    if not wa: return jsonify({"error": "WebAuthn not available"}), 503
    try:
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
            ResidentKeyRequirement, UserVerificationRequirement, PublicKeyCredentialDescriptor)
        uid = current_user.id
        ur  = models.user_by_id(uid)
        ex  = [PublicKeyCredentialDescriptor(id=pk["credential_id"])
               for pk in models.passkeys_for_user(uid)]
        opts = wa.generate_registration_options(
            rp_id=RP_ID, rp_name=RP_NAME,
            user_id=str(uid).encode(), user_name=ur["email"],
            user_display_name=ur.get("name") or ur["email"],
            exclude_credentials=ex,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED),
        )
        session["wa_reg_challenge"] = opts.challenge
        return jsonify(json.loads(wa.options_to_json(opts)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/auth/passkey/register/complete", methods=["POST"])
@login_required
def passkey_register_complete():
    wa = _wa()
    if not wa: return jsonify({"error": "WebAuthn not available"}), 503
    challenge = session.pop("wa_reg_challenge", None)
    if not challenge: return jsonify({"error": "No challenge"}), 400
    try:
        from webauthn.helpers.structs import RegistrationCredential
        v = wa.verify_registration_response(
            credential=_parse_cred(RegistrationCredential, request.get_json(force=True)),
            expected_challenge=challenge, expected_rp_id=RP_ID, expected_origin=WA_ORIGIN)
        models.passkey_store(current_user.id, v.credential_id, v.credential_public_key)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/auth/passkey/authenticate/begin", methods=["POST"])
def passkey_authenticate_begin():
    wa = _wa()
    if not wa: return jsonify({"error": "WebAuthn not available"}), 503
    try:
        from webauthn.helpers.structs import UserVerificationRequirement
        opts = wa.generate_authentication_options(rp_id=RP_ID,
            user_verification=UserVerificationRequirement.PREFERRED)
        session["wa_auth_challenge"] = opts.challenge
        return jsonify(json.loads(wa.options_to_json(opts)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/auth/passkey/authenticate/complete", methods=["POST"])
def passkey_authenticate_complete():
    wa = _wa()
    if not wa: return jsonify({"error": "WebAuthn not available"}), 503
    challenge = session.pop("wa_auth_challenge", None)
    if not challenge: return jsonify({"error": "No challenge"}), 400
    try:
        from webauthn.helpers.structs import AuthenticationCredential
        data = request.get_json(force=True)
        cred = _parse_cred(AuthenticationCredential, data)
        try:
            raw_id = wa.helpers.base64url_to_bytes(data["rawId"])
        except AttributeError:
            from webauthn.helpers.bytes_helper import base64url_to_bytes
            raw_id = base64url_to_bytes(data["rawId"])
        pk = models.passkey_by_credential_id(raw_id)
        if not pk: return jsonify({"error": "Passkey not found"}), 404
        v = wa.verify_authentication_response(
            credential=cred, expected_challenge=challenge,
            expected_rp_id=RP_ID, expected_origin=WA_ORIGIN,
            credential_public_key=pk["public_key"],
            credential_current_sign_count=pk["sign_count"])
        models.passkey_update_sign_count(raw_id, v.new_sign_count)
        ur = models.user_by_id(pk["user_id"])
        if not ur: return jsonify({"error": "User not found"}), 404
        login_user(User(ur), remember=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ── Scheduler — every 15 min scan + every 14 min keep-alive ──────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(_run_scan,    "interval", minutes=15, id="global_scan")
scheduler.add_job(_keep_alive,  "interval", minutes=14, id="keep_alive")
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
