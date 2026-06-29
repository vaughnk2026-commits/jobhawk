"""
JobHawk Web — Vaughn Krogman Automated Job Search
Scrapes 7 sources every 15 minutes. Auto-applies via email. SQLite tracking.
"""

import concurrent.futures
import csv
import datetime as dt
import json
import logging
import os
import re
import smtplib
import sqlite3
import threading
import xml.etree.ElementTree as ET
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

BASE    = Path(__file__).resolve().parent
DATA    = BASE / "data"
OUTPUT  = BASE / "output"
LOGS    = BASE / "logs"
PACKETS = OUTPUT / "application_packets"
UPLOADS = BASE / "uploads"
DB_PATH = DATA / "jobhawk.db"

for _d in [DATA, OUTPUT, LOGS, PACKETS, UPLOADS]:
    _d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOGS / "jobhawk.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


# ── SQLite ────────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                title       TEXT,
                company     TEXT,
                location    TEXT,
                url         TEXT,
                source      TEXT,
                match_score INTEGER DEFAULT 0,
                status      TEXT    DEFAULT 'new',
                email_found TEXT,
                applied_at  TEXT,
                notes       TEXT,
                first_seen  TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.commit()

init_db()

def db_upsert(job: Dict):
    jid = job.get("url") or f"{job.get('company','')}|{job.get('title','')}"
    with _db_lock:
        with _db() as c:
            c.execute("""
                INSERT OR IGNORE INTO jobs
                  (id, title, company, location, url, source, match_score, email_found)
                VALUES (?,?,?,?,?,?,?,?)
            """, (jid, job.get("title"), job.get("company"), job.get("location"),
                  job.get("url"), job.get("source"), job.get("match_score", 0),
                  job.get("email_found")))
            c.execute("UPDATE jobs SET match_score=? WHERE id=? AND status='new'",
                      (job.get("match_score", 0), jid))
            c.commit()
    return jid

def db_mark_applied(jid: str, email_found: Optional[str] = None):
    with _db_lock:
        with _db() as c:
            c.execute("""
                UPDATE jobs SET status='applied', applied_at=CURRENT_TIMESTAMP,
                               email_found=COALESCE(?,email_found)
                WHERE id=? AND status='new'
            """, (email_found, jid))
            c.commit()

def db_update_status(jid: str, status: str, notes: str = ""):
    with _db_lock:
        with _db() as c:
            c.execute("UPDATE jobs SET status=?, notes=? WHERE id=?", (status, notes, jid))
            c.commit()

def db_applied() -> List[Dict]:
    with _db() as c:
        rows = c.execute("""
            SELECT * FROM jobs
            WHERE status IN ('applied','interview','offer','rejected')
            ORDER BY applied_at DESC
        """).fetchall()
    return [dict(r) for r in rows]

def db_interviews() -> List[Dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE status='interview' ORDER BY applied_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]

def db_stats() -> Dict:
    with _db() as c:
        total     = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        applied   = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('applied','interview','offer','rejected')").fetchone()[0]
        interview = c.execute("SELECT COUNT(*) FROM jobs WHERE status='interview'").fetchone()[0]
    return {"total": total, "applied": applied, "interview": interview}


# ── State ─────────────────────────────────────────────────────────────────────

_state: Dict[str, Any] = {
    "last_run": None,
    "last_run_status": "Never run",
    "running": False,
    "job_count": 0,
    "applied_count": 0,
    "interview_count": 0,
    "run_count": 0,
    "resume_name": None,
    "resume_path": None,
    "cover_letter_name": None,
    "cover_letter_path": None,
}
_state_lock = threading.Lock()

_paths_file = DATA / "paths.json"
if _paths_file.exists():
    try:
        _state.update(json.loads(_paths_file.read_text()))
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()

def load_config() -> Dict[str, Any]:
    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
SKIP_DOMAINS = {"example.com", "noreply.com", "sentry.io", "email.com", "test.com"}

def find_email(text: str) -> Optional[str]:
    for m in EMAIL_RE.findall(text or ""):
        dom = m.split("@")[-1].lower()
        if dom not in SKIP_DOMAINS and "noreply" not in m.lower():
            return m
    return None


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_remotive() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", timeout=25)
        r.raise_for_status()
        for item in r.json().get("jobs", []):
            jobs.append({
                "source": "Remotive",
                "title": normalize(item.get("title")),
                "company": normalize(item.get("company_name")),
                "location": normalize(item.get("candidate_required_location") or "Remote"),
                "remote": True,
                "url": item.get("url"),
                "date_posted": normalize(item.get("publication_date")),
                "description": BeautifulSoup(item.get("description") or "", "html.parser").get_text(" "),
            })
    except Exception as e:
        log.exception("Remotive: %s", e)
    return jobs


def fetch_arbeitnow() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=25)
        r.raise_for_status()
        for item in r.json().get("data", []):
            loc = normalize(item.get("location"))
            is_remote = bool(item.get("remote")) or "remote" in loc.lower()
            desc = BeautifulSoup(item.get("description") or "", "html.parser").get_text(" ")
            jobs.append({
                "source": "Arbeitnow",
                "title": normalize(item.get("title")),
                "company": normalize(item.get("company_name")),
                "location": loc or "Remote",
                "remote": is_remote,
                "url": item.get("url"),
                "date_posted": str(item.get("created_at") or ""),
                "description": desc,
            })
    except Exception as e:
        log.exception("Arbeitnow: %s", e)
    return jobs


def fetch_remoteok() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://remoteok.io/api", timeout=25,
                         headers={"User-Agent": "JobHawk/1.0 (job search bot)"})
        r.raise_for_status()
        data = r.json()
        for item in (data[1:] if len(data) > 1 else []):
            if not isinstance(item, dict) or not item.get("position"):
                continue
            tags = " ".join(item.get("tags") or [])
            desc = BeautifulSoup(item.get("description") or "", "html.parser").get_text(" ")
            jobs.append({
                "source": "RemoteOK",
                "title": normalize(item.get("position")),
                "company": normalize(item.get("company")),
                "location": normalize(item.get("location") or "Remote"),
                "remote": True,
                "url": item.get("url") or f"https://remoteok.io/remote-jobs/{item.get('id','')}",
                "date_posted": str(item.get("date") or ""),
                "description": f"{desc} {tags}",
            })
    except Exception as e:
        log.exception("RemoteOK: %s", e)
    return jobs


def fetch_weworkremotely() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://weworkremotely.com/remote-jobs.rss", timeout=25,
                         headers={"User-Agent": "JobHawk/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            def txt(tag):
                el = item.find(tag)
                return (el.text or "") if el is not None else ""
            raw = txt("title")
            parts = raw.split(":", 1)
            company = normalize(parts[0]) if len(parts) > 1 else ""
            title   = normalize(parts[1]) if len(parts) > 1 else normalize(raw)
            desc = BeautifulSoup(txt("description"), "html.parser").get_text(" ")
            jobs.append({
                "source": "WeWorkRemotely",
                "title": title, "company": company,
                "location": "Remote", "remote": True,
                "url": txt("link") or txt("guid"),
                "date_posted": normalize(txt("pubDate")),
                "description": desc,
            })
    except Exception as e:
        log.exception("WeWorkRemotely: %s", e)
    return jobs


def fetch_jobicy() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://jobicy.com/?feed=job_feed", timeout=25,
                         headers={"User-Agent": "JobHawk/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            def txt(tag):
                el = item.find(tag)
                return (el.text or "") if el is not None else ""
            title = normalize(txt("title"))
            url   = txt("link") or txt("guid")
            desc  = BeautifulSoup(txt("description"), "html.parser").get_text(" ")
            company = ""
            for child in item:
                if "company" in child.tag.lower():
                    company = normalize(child.text or ""); break
            if not company and " @ " in title:
                parts = title.split(" @ ", 1)
                title, company = normalize(parts[0]), normalize(parts[1])
            jobs.append({
                "source": "Jobicy",
                "title": title, "company": company or "—",
                "location": "Remote", "remote": True,
                "url": url, "date_posted": normalize(txt("pubDate")),
                "description": desc,
            })
    except Exception as e:
        log.exception("Jobicy: %s", e)
    return jobs


def fetch_indeed() -> List[Dict]:
    jobs = []
    try:
        cfg = load_config()
        queries = cfg.get("sources", {}).get("indeed_queries", [
            "automotive finance manager Canada",
            "sales manager Calgary Alberta",
            "business development manager Alberta",
        ])
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for q in queries:
            try:
                params = urlencode({"q": q, "l": "Calgary, Alberta", "sort": "date", "radius": "100"})
                r = requests.get(f"https://www.indeed.com/rss?{params}", timeout=20, headers=hdrs)
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    def txt(tag):
                        el = item.find(tag)
                        return (el.text or "") if el is not None else ""
                    title = normalize(txt("title"))
                    src_el = item.find("source")
                    company = normalize(src_el.text if src_el is not None and src_el.text else "")
                    desc = BeautifulSoup(txt("description"), "html.parser").get_text(" ")
                    jobs.append({
                        "source": "Indeed",
                        "title": title, "company": company or "—",
                        "location": normalize(txt("location") or "Calgary, Alberta"),
                        "remote": "remote" in title.lower() or "remote" in desc.lower(),
                        "url": txt("link"),
                        "date_posted": normalize(txt("pubDate")),
                        "description": desc,
                    })
            except Exception as eq:
                log.warning("Indeed query '%s': %s", q, eq)
    except Exception as e:
        log.exception("Indeed: %s", e)
    return jobs


def fetch_linkedin() -> List[Dict]:
    jobs = []
    try:
        cfg = load_config()
        searches = cfg.get("sources", {}).get("linkedin_searches", [
            {"keywords": "automotive finance manager", "location": "Calgary, Alberta, Canada"},
            {"keywords": "sales manager", "location": "Alberta, Canada"},
        ])
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        for s in searches:
            try:
                params = {"keywords": s["keywords"], "location": s["location"],
                          "f_TPR": "r86400", "start": 0}
                r = requests.get(
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params=params, timeout=20, headers=hdrs
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                for card in soup.find_all("li"):
                    title_el = card.find("h3")
                    company_el = card.find("h4")
                    loc_el = card.find("span", class_=lambda c: c and "location" in (c or ""))
                    link_el = card.find("a", href=True)
                    if not title_el or not title_el.get_text(strip=True):
                        continue
                    url = link_el["href"].split("?")[0] if link_el else ""
                    jobs.append({
                        "source": "LinkedIn",
                        "title": normalize(title_el.get_text()),
                        "company": normalize(company_el.get_text()) if company_el else "—",
                        "location": normalize(loc_el.get_text()) if loc_el else s["location"],
                        "remote": "remote" in normalize(title_el.get_text()).lower(),
                        "url": url,
                        "date_posted": "",
                        "description": normalize(card.get_text()),
                    })
            except Exception as es:
                log.warning("LinkedIn '%s': %s", s["keywords"], es)
    except Exception as e:
        log.exception("LinkedIn: %s", e)
    return jobs


def search_jobs(cfg) -> List[Dict]:
    sc = cfg.get("sources", {})
    fetcher_map = {
        "remotive":       (fetch_remotive,      sc.get("remotive", True)),
        "arbeitnow":      (fetch_arbeitnow,      sc.get("arbeitnow", True)),
        "remoteok":       (fetch_remoteok,       sc.get("remoteok", True)),
        "weworkremotely": (fetch_weworkremotely, sc.get("weworkremotely", True)),
        "jobicy":         (fetch_jobicy,         sc.get("jobicy", True)),
        "indeed":         (fetch_indeed,         sc.get("indeed", True)),
        "linkedin":       (fetch_linkedin,       sc.get("linkedin", True)),
    }
    active = [fn for _, (fn, enabled) in fetcher_map.items() if enabled]
    jobs: List[Dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as ex:
        futures = {ex.submit(fn): fn.__name__ for fn in active}
        for fut in concurrent.futures.as_completed(futures, timeout=90):
            name = futures[fut]
            try:
                result = fut.result()
                jobs.extend(result)
                log.info("%s -> %d jobs", name, len(result))
            except Exception as e:
                log.exception("Fetcher %s: %s", name, e)
    seen, deduped = set(), []
    for j in jobs:
        key = j.get("url") or f"{j.get('company')}|{j.get('title')}"
        if key not in seen:
            seen.add(key)
            deduped.append(j)
    (DATA / "jobs_raw.json").write_text(
        json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Total unique jobs: %d", len(deduped))
    return deduped


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(job: Dict, cfg: Dict) -> int:
    blob = f"{job.get('title','')} {job.get('description','')} {job.get('location','')}".lower()
    score = 0
    for role in cfg.get("roles", {}).get("primary", []):
        if role.lower() in blob: score += 18
    for role in cfg.get("roles", {}).get("secondary", []):
        if role.lower() in blob: score += 10
    for kw in cfg.get("keywords", {}).get("strongest", []):
        if kw.lower() in blob: score += 4
    for kw in cfg.get("keywords", {}).get("support", []):
        if kw.lower() in blob: score += 2
    if job.get("remote"): score += 8
    if any(x in blob for x in ["canada", "calgary", "alberta"]): score += 10
    for bad in cfg.get("search", {}).get("excluded_terms", []):
        if bad.lower() in blob: score -= 20
    return max(0, min(100, score))


def score_and_save(cfg, jobs: List[Dict]) -> List[Dict]:
    for j in jobs:
        j["match_score"] = score_job(j, cfg)
        j["email_found"] = find_email(j.get("description", ""))
    jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    fields = ["match_score", "title", "company", "location", "remote",
              "source", "date_posted", "url", "email_found"]
    with open(DATA / "jobs_scored.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for j in jobs:
            w.writerow({k: j.get(k, "") for k in fields})
    return jobs


# ── Email auto-apply ──────────────────────────────────────────────────────────

def send_application(job: Dict, resume_path: str, cfg: Dict) -> bool:
    to_email = job.get("email_found")
    if not to_email:
        return False
    ecfg = cfg.get("email", {})
    from_email = ecfg.get("from_email") or os.environ.get("EMAIL_FROM", "")
    password   = os.environ.get("EMAIL_PASSWORD") or ecfg.get("password", "")
    if not from_email or not password:
        return False
    try:
        msg = MIMEMultipart()
        msg["From"]    = from_email
        msg["To"]      = to_email
        msg["Subject"] = f"Application — {job.get('title')} | Vaughn Krogman"
        body = (
            f"Dear Hiring Team at {job.get('company', '')},\n\n"
            f"I am writing to apply for the {job.get('title', '')} position.\n\n"
            "I bring 20+ years of automotive finance, sales leadership, special finance, "
            "lender relations, CRM, and dealership growth experience. I would welcome "
            "the opportunity to contribute immediate value to your team.\n\n"
            "Please find my resume attached. I look forward to discussing this opportunity.\n\n"
            "Sincerely,\nVaughn Krogman\nCalgary, Alberta\n"
            "825-779-1000 | vaughnk2025@gmail.com"
        )
        msg.attach(MIMEText(body, "plain"))
        if resume_path and Path(resume_path).exists():
            with open(resume_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="{Path(resume_path).name}"')
            msg.attach(part)
        smtp_host = ecfg.get("smtp_host", "smtp.gmail.com")
        smtp_port = int(ecfg.get("smtp_port", 587))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as srv:
            srv.starttls()
            srv.login(from_email, password)
            srv.send_message(msg)
        log.info("Email sent -> %s for %s @ %s", to_email, job.get("title"), job.get("company"))
        return True
    except Exception as e:
        log.exception("Email send failed: %s", e)
        return False


def auto_apply(scored_jobs: List[Dict], cfg: Dict) -> int:
    min_score = cfg.get("search", {}).get("min_score_to_apply", 10)
    with _state_lock:
        resume_path = _state.get("resume_path")
    count = 0
    for job in scored_jobs:
        if job.get("match_score", 0) < min_score:
            continue
        jid = db_upsert(job)
        with _db() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
        if row and row["status"] != "new":
            continue
        email_sent = send_application(job, resume_path or "", cfg)
        db_mark_applied(jid, email_found=job.get("email_found") if email_sent else None)
        count += 1
    return count


# ── Main run loop ─────────────────────────────────────────────────────────────

def run_all():
    with _state_lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["last_run_status"] = "Running..."
    try:
        cfg    = load_config()
        jobs   = search_jobs(cfg)
        scored = score_and_save(cfg, jobs)
        auto_apply(scored, cfg)
        stats  = db_stats()
        now    = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _state_lock:
            _state["last_run"]         = now
            _state["last_run_status"]  = f"Done at {now}"
            _state["job_count"]        = len(scored)
            _state["applied_count"]    = stats["applied"]
            _state["interview_count"]  = stats["interview"]
            _state["run_count"]       += 1
        log.info("run_all done: %d jobs", len(scored))
    except Exception as e:
        log.exception("run_all failed: %s", e)
        with _state_lock:
            _state["last_run_status"] = f"Error: {e}"
    finally:
        with _state_lock:
            _state["running"] = False


def load_all_jobs() -> List[Dict]:
    path = DATA / "jobs_scored.csv"
    if not path.exists():
        return []
    jobs = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["match_score"] = int(row.get("match_score") or 0)
            jobs.append(row)
    return sorted(jobs, key=lambda x: x["match_score"], reverse=True)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _state_lock:
        s = dict(_state)
    s.update(db_stats())
    return jsonify(s)


@app.route("/api/jobs")
def api_jobs():
    return jsonify(load_all_jobs())


@app.route("/api/applied")
def api_applied():
    return jsonify(db_applied())


@app.route("/api/interviews")
def api_interviews():
    return jsonify(db_interviews())


@app.route("/api/run", methods=["POST"])
def api_run():
    threading.Thread(target=run_all, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/download")
def api_download():
    path = DATA / "jobs_scored.csv"
    if not path.exists():
        return jsonify({"error": "No data yet"}), 404
    return send_file(str(path), mimetype="text/csv",
                     as_attachment=True, download_name="jobhawk_results.csv")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f  = request.files["file"]
    ft = request.form.get("type", "resume")
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", f.filename)
    dest = UPLOADS / f"{ft}_{safe}"
    f.save(str(dest))
    with _state_lock:
        if ft == "resume":
            _state["resume_name"] = f.filename
            _state["resume_path"] = str(dest)
        else:
            _state["cover_letter_name"] = f.filename
            _state["cover_letter_path"] = str(dest)
    try:
        _paths_file.write_text(json.dumps({
            "resume_name":       _state.get("resume_name"),
            "resume_path":       _state.get("resume_path"),
            "cover_letter_name": _state.get("cover_letter_name"),
            "cover_letter_path": _state.get("cover_letter_path"),
        }))
    except Exception:
        pass
    return jsonify({"ok": True, "name": f.filename, "type": ft})


@app.route("/api/job/status", methods=["POST"])
def api_job_status():
    data   = request.get_json(force=True)
    jid    = data.get("id", "")
    status = data.get("status", "")
    notes  = data.get("notes", "")
    if not jid or not status:
        return jsonify({"ok": False}), 400
    db_update_status(jid, status, notes)
    stats = db_stats()
    with _state_lock:
        _state["applied_count"]   = stats["applied"]
        _state["interview_count"] = stats["interview"]
    return jsonify({"ok": True})


# ── Scheduler — every 15 minutes ─────────────────────────────────────────────

def _bg_run():
    log.info("Scheduled scan")
    run_all()


scheduler = BackgroundScheduler()
scheduler.add_job(_bg_run, "interval", minutes=15, id="jobhawk_scan")
scheduler.start()

import threading as _th
_th.Thread(target=_bg_run, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
