"""
JobHawk Web — Vaughn Krogman Job Search Dashboard
Run with: python app.py
Then open: http://localhost:5000
"""

import csv
import datetime as dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, send_file
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUTPUT = BASE / "output"
LOGS = BASE / "logs"
PACKETS = OUTPUT / "application_packets"

for folder in [DATA, OUTPUT, LOGS, PACKETS]:
    folder.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOGS / "jobhawk.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── shared state ──────────────────────────────────────────────────────────────
_state: Dict[str, Any] = {
    "last_run": None,
    "last_run_status": "Never run",
    "running": False,
    "job_count": 0,
    "packet_count": 0,
    "run_count": 0,
}
_state_lock = threading.Lock()


# ── core logic ────────────────────────────────────────────────────────────────

def load_config() -> Dict[str, Any]:
    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


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
        log.exception("Remotive fetch failed: %s", e)
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
            text = f"{item.get('title','')} {item.get('company_name','')} {loc} {desc}".lower()
            if is_remote or "canada" in text or "calgary" in text or "alberta" in text:
                jobs.append({
                    "source": "Arbeitnow",
                    "title": normalize(item.get("title")),
                    "company": normalize(item.get("company_name")),
                    "location": loc or "Remote/Various",
                    "remote": is_remote,
                    "url": item.get("url"),
                    "date_posted": str(item.get("created_at") or ""),
                    "description": desc,
                })
    except Exception as e:
        log.exception("Arbeitnow fetch failed: %s", e)
    return jobs


def search_jobs(cfg) -> List[Dict]:
    jobs = []
    if cfg["sources"].get("remotive"):
        jobs.extend(fetch_remotive())
    if cfg["sources"].get("arbeitnow"):
        jobs.extend(fetch_arbeitnow())

    for url in cfg["sources"].get("manual_search_urls", []):
        jobs.append({
            "source": "Manual Search URL",
            "title": "Search results feed",
            "company": "Various",
            "location": "Canada/Remote",
            "remote": True,
            "url": url,
            "date_posted": dt.datetime.now().isoformat(),
            "description": "Manual job-board search URL.",
        })

    seen, deduped = set(), []
    for j in jobs:
        key = (j.get("url") or "", j.get("title") or "", j.get("company") or "")
        if key not in seen:
            seen.add(key)
            deduped.append(j)

    deduped = deduped[:cfg["search"].get("max_results_per_run", 500)]
    (DATA / "jobs_raw.json").write_text(json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Fetched %s jobs", len(deduped))
    return deduped


def score_job(job: Dict, cfg: Dict) -> int:
    blob = f"{job.get('title','')} {job.get('description','')} {job.get('location','')}".lower()
    score = 0
    for role in cfg["roles"]["primary"]:
        if role.lower() in blob:
            score += 18
    for role in cfg["roles"]["secondary"]:
        if role.lower() in blob:
            score += 10
    for kw in cfg["keywords"]["strongest"]:
        if kw.lower() in blob:
            score += 4
    for kw in cfg["keywords"].get("support", []):
        if kw.lower() in blob:
            score += 2
    if job.get("remote"):
        score += 10
    if "canada" in blob or "calgary" in blob or "alberta" in blob:
        score += 10
    for bad in cfg["search"].get("excluded_terms", []):
        if bad.lower() in blob:
            score -= 25
    return max(0, min(100, score))


def score_jobs(cfg, jobs: List[Dict]) -> List[Dict]:
    for j in jobs:
        j["match_score"] = score_job(j, cfg)
        j["status"] = "New"
    jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)

    fields = ["match_score", "title", "company", "location", "remote", "source", "date_posted", "url", "status"]
    with open(DATA / "jobs_scored.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for j in jobs:
            writer.writerow({k: j.get(k, "") for k in fields})
    return jobs


def choose_summary(job: Dict) -> str:
    try:
        variants = yaml.safe_load((BASE / "templates" / "resume_summary_variants.yaml").read_text(encoding="utf-8"))
    except Exception:
        return "See attached resume."
    text = f"{job.get('title','')} {job.get('description','')}".lower()
    if any(x in text for x in ["f&i", "finance manager", "finance director", "special finance", "lender"]):
        return variants.get("automotive_finance", "")
    if any(x in text for x in ["general sales", "sales manager", "team", "dealership sales"]):
        return variants.get("sales_leadership", "")
    if any(x in text for x in ["saas", "software", "account executive", "demo", "crm"]):
        return variants.get("automotive_saas", "")
    if any(x in text for x in ["business development", "partnership", "pipeline"]):
        return variants.get("business_development", "")
    return variants.get("remote_sales" if job.get("remote") else "sales_leadership", "")


def create_packets(cfg, jobs: List[Dict]) -> int:
    min_score = cfg["search"].get("min_score_to_package", 65)
    count = 0
    for j in jobs:
        if j["match_score"] < min_score:
            continue
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", f"{j.get('company','company')}_{j.get('title','job')}").strip("_")[:80]
        folder = PACKETS / safe
        folder.mkdir(parents=True, exist_ok=True)
        cover = f"""Dear Hiring Manager,

I am applying for the {j.get('title')} role with {j.get('company')}. My background aligns strongly with this position because I bring more than 20 years of automotive finance, sales leadership, special finance, lender relations, business development, CRM, and technology-driven dealership growth experience.

What separates me from a typical candidate is that I understand dealership operations from the floor, the finance office, and the technology side. I have led finance departments, coached teams, built credit rebuild programs, improved warranty and product penetration, and developed CRM and lead-generation workflows that improve conversion and profitability.

For this role, I would bring immediate value through disciplined pipeline management, strong negotiation, lender and client relationship development, process improvement, and a results-first approach to revenue growth.

Sincerely,
Vaughn Krogman
Calgary, Alberta
825-779-1000
vaughnk2025@gmail.com
"""
        (folder / "tailored_summary.txt").write_text(choose_summary(j), encoding="utf-8")
        (folder / "cover_letter.txt").write_text(cover, encoding="utf-8")
        (folder / "job_url.txt").write_text(j.get("url") or "", encoding="utf-8")
        count += 1
    return count


def run_all():
    with _state_lock:
        if _state["running"]:
            return {"error": "Already running"}
        _state["running"] = True
        _state["last_run_status"] = "Running..."

    try:
        cfg = load_config()
        jobs = search_jobs(cfg)
        scored = score_jobs(cfg, jobs)
        packets = create_packets(cfg, scored)

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _state_lock:
            _state["last_run"] = now
            _state["last_run_status"] = f"Completed at {now}"
            _state["job_count"] = len(scored)
            _state["packet_count"] = packets
            _state["run_count"] += 1
        log.info("run_all complete: %s jobs, %s packets", len(scored), packets)
        return {"jobs": len(scored), "packets": packets, "timestamp": now}
    except Exception as e:
        log.exception("run_all failed: %s", e)
        with _state_lock:
            _state["last_run_status"] = f"Error: {e}"
        return {"error": str(e)}
    finally:
        with _state_lock:
            _state["running"] = False


def load_scored_jobs() -> List[Dict]:
    path = DATA / "jobs_scored.csv"
    if not path.exists():
        return []
    jobs = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["match_score"] = int(row.get("match_score") or 0)
            jobs.append(row)
    return sorted(jobs, key=lambda x: x["match_score"], reverse=True)


# ── HTML dashboard ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobHawk — Vaughn Krogman</title>
<style>
  :root {
    --bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;
    --accent:#4f8ef7;--accent2:#22d3a4;--warn:#f59e0b;
    --text:#e2e8f0;--muted:#8892a4;
    --high:#22d3a4;--mid:#f59e0b;--low:#ef4444;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
  header{background:var(--card);border-bottom:1px solid var(--border);padding:18px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
  .logo{display:flex;align-items:center;gap:10px}
  .logo-icon{font-size:28px}
  .logo h1{font-size:22px;font-weight:700}
  .logo span{color:var(--accent)}
  .header-meta{color:var(--muted);font-size:13px}
  .actions{display:flex;gap:10px;align-items:center}
  .btn{padding:9px 20px;border-radius:8px;border:none;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s}
  .btn:hover{opacity:.85;transform:translateY(-1px)}
  .btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
  .btn-primary{background:var(--accent);color:#fff}
  .btn-secondary{background:var(--border);color:var(--text)}
  .status-bar{background:var(--card);border-bottom:1px solid var(--border);padding:10px 32px;display:flex;gap:32px;flex-wrap:wrap;font-size:13px}
  .stat{display:flex;flex-direction:column;gap:2px}
  .stat-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .stat-value{font-weight:600;font-size:15px}
  .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent2);margin-right:6px;animation:pulse 1.5s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
  main{padding:24px 32px;max-width:1400px;margin:0 auto}
  .filters{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;align-items:flex-end}
  .filter-group{display:flex;flex-direction:column;gap:4px}
  .filter-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  select,input{background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:7px 12px;font-size:13px;outline:none}
  select:focus,input:focus{border-color:var(--accent)}
  .range-row{display:flex;gap:8px;align-items:center}
  .range-val{font-size:13px;font-weight:600;color:var(--accent);min-width:28px}
  #count-badge{font-size:13px;color:var(--muted);padding:6px 14px;background:var(--card);border:1px solid var(--border);border-radius:6px;align-self:flex-end}
  .jobs-grid{display:grid;gap:12px}
  .job-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;display:grid;grid-template-columns:64px 1fr auto;gap:16px;align-items:start;transition:border-color .15s}
  .job-card:hover{border-color:var(--accent)}
  .score-ring{width:56px;height:56px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:800;flex-shrink:0}
  .score-high{background:rgba(34,211,164,.15);color:var(--high);border:2px solid var(--high)}
  .score-mid{background:rgba(245,158,11,.15);color:var(--mid);border:2px solid var(--mid)}
  .score-low{background:rgba(239,68,68,.15);color:var(--low);border:2px solid var(--low)}
  .job-info{overflow:hidden}
  .job-title{font-size:16px;font-weight:700;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .job-company{font-size:14px;color:var(--accent);font-weight:600;margin-bottom:6px}
  .job-meta{display:flex;gap:8px;flex-wrap:wrap}
  .tag{font-size:11px;padding:3px 8px;border-radius:99px;background:var(--border);color:var(--muted)}
  .tag-remote{background:rgba(79,142,247,.15);color:var(--accent)}
  .tag-source{background:rgba(34,211,164,.1);color:var(--accent2)}
  .job-actions{display:flex;flex-direction:column;gap:8px;align-items:flex-end}
  .apply-btn{background:var(--accent);color:#fff;border:none;border-radius:7px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block;transition:opacity .15s;white-space:nowrap}
  .apply-btn:hover{opacity:.85}
  .date-tag{font-size:11px;color:var(--muted);text-align:right}
  .empty{text-align:center;padding:60px 20px;color:var(--muted)}
  .empty-icon{font-size:48px;margin-bottom:12px}
  .empty h2{font-size:20px;margin-bottom:8px;color:var(--text)}
  .toast{position:fixed;bottom:24px;right:24px;background:var(--card);border:1px solid var(--accent2);border-radius:10px;padding:14px 20px;font-size:14px;font-weight:600;color:var(--accent2);display:none;z-index:999;animation:slideIn .3s ease}
  @keyframes slideIn{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
  @media(max-width:700px){header,.status-bar,main{padding-left:16px;padding-right:16px}.job-card{grid-template-columns:48px 1fr}.job-actions{display:none}}
</style>
</head>
<body>
<header>
  <div class="logo">
    <span class="logo-icon">🦅</span>
    <div>
      <h1>Job<span>Hawk</span></h1>
      <div class="header-meta">Vaughn Krogman · Calgary, AB · 825-779-1000</div>
    </div>
  </div>
  <div class="actions">
    <button class="btn btn-secondary" onclick="downloadCSV()">⬇ Export CSV</button>
    <button class="btn btn-primary" id="run-btn" onclick="runNow()">▶ Run Now</button>
  </div>
</header>

<div class="status-bar">
  <div class="stat"><span class="stat-label">Last Run</span><span class="stat-value" id="last-run">—</span></div>
  <div class="stat"><span class="stat-label">Status</span><span class="stat-value" id="run-status">—</span></div>
  <div class="stat"><span class="stat-label">Jobs Found</span><span class="stat-value" id="job-count">—</span></div>
  <div class="stat"><span class="stat-label">Packets Ready</span><span class="stat-value" id="packet-count">—</span></div>
  <div class="stat"><span class="stat-label">Next Auto-Run</span><span class="stat-value"><span class="pulse"></span><span id="next-run">4h</span></span></div>
</div>

<main>
  <div class="filters">
    <div class="filter-group">
      <span class="filter-label">Min Score</span>
      <div class="range-row">
        <input type="range" id="min-score" min="0" max="100" value="0" step="5" oninput="filterJobs()">
        <span class="range-val" id="min-score-val">0</span>
      </div>
    </div>
    <div class="filter-group">
      <span class="filter-label">Source</span>
      <select id="filter-source" onchange="filterJobs()">
        <option value="">All Sources</option>
        <option>Remotive</option>
        <option>Arbeitnow</option>
        <option>Manual Search URL</option>
      </select>
    </div>
    <div class="filter-group">
      <span class="filter-label">Remote</span>
      <select id="filter-remote" onchange="filterJobs()">
        <option value="">All</option>
        <option value="true">Remote only</option>
      </select>
    </div>
    <div class="filter-group">
      <span class="filter-label">Search</span>
      <input type="text" id="search-box" placeholder="Title, company…" oninput="filterJobs()">
    </div>
    <span id="count-badge">— jobs</span>
  </div>

  <div class="jobs-grid" id="jobs-grid">
    <div class="empty">
      <div class="empty-icon">🔍</div>
      <h2>Loading…</h2>
      <p>Scanning job boards automatically on startup</p>
    </div>
  </div>
</main>

<div class="toast" id="toast"></div>

<script>
let allJobs = [];
let countdown = 14400; // 4 hours in seconds

async function fetchStatus() {
  try {
    const d = await (await fetch('/api/status')).json();
    document.getElementById('last-run').textContent = d.last_run || '—';
    document.getElementById('run-status').textContent = d.last_run_status || '—';
    document.getElementById('job-count').textContent = d.job_count ?? '—';
    document.getElementById('packet-count').textContent = d.packet_count ?? '—';
    const btn = document.getElementById('run-btn');
    btn.disabled = d.running;
    btn.textContent = d.running ? '⏳ Running…' : '▶ Run Now';
    if (d.running) setTimeout(fetchStatus, 3000);
  } catch(e) {}
}

async function fetchJobs() {
  try {
    allJobs = await (await fetch('/api/jobs')).json();
    filterJobs();
  } catch(e) {}
}

function scoreClass(s) {
  return s >= 65 ? 'score-high' : s >= 40 ? 'score-mid' : 'score-low';
}

function filterJobs() {
  const minScore = parseInt(document.getElementById('min-score').value);
  document.getElementById('min-score-val').textContent = minScore;
  const src = document.getElementById('filter-source').value;
  const remoteOnly = document.getElementById('filter-remote').value === 'true';
  const q = document.getElementById('search-box').value.toLowerCase();

  const filtered = allJobs.filter(j => {
    if (j.match_score < minScore) return false;
    if (src && j.source !== src) return false;
    if (remoteOnly && j.remote !== 'True' && j.remote !== true) return false;
    if (q && !`${j.title} ${j.company}`.toLowerCase().includes(q)) return false;
    return true;
  });

  document.getElementById('count-badge').textContent = filtered.length + ' jobs';
  const grid = document.getElementById('jobs-grid');

  if (!filtered.length) {
    grid.innerHTML = '<div class="empty"><div class="empty-icon">🔍</div><h2>No matches</h2><p>Adjust filters or run a fresh scan</p></div>';
    return;
  }

  grid.innerHTML = filtered.slice(0, 300).map(j => `
    <div class="job-card">
      <div class="score-ring ${scoreClass(j.match_score)}">${j.match_score}</div>
      <div class="job-info">
        <div class="job-title" title="${j.title}">${j.title || '(untitled)'}</div>
        <div class="job-company">${j.company || '—'}</div>
        <div class="job-meta">
          <span class="tag">📍 ${j.location || 'Unknown'}</span>
          ${(j.remote === 'True' || j.remote === true) ? '<span class="tag tag-remote">🌐 Remote</span>' : ''}
          <span class="tag tag-source">${j.source}</span>
          ${j.date_posted ? '<span class="tag">📅 ' + (j.date_posted || '').substring(0,10) + '</span>' : ''}
        </div>
      </div>
      <div class="job-actions">
        ${j.url ? '<a class="apply-btn" href="' + j.url + '" target="_blank" rel="noopener">Apply →</a>' : ''}
        <span class="date-tag">Score ${j.match_score}/100</span>
      </div>
    </div>
  `).join('');
}

async function runNow() {
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  showToast('🚀 Scan started…');
  try {
    const d = await (await fetch('/api/run', {method:'POST'})).json();
    if (d.error) { showToast('⚠ ' + d.error, 4000); }
    else {
      showToast('✅ Done — ' + d.jobs + ' jobs, ' + d.packets + ' packets', 4000);
      await fetchJobs();
      countdown = 14400;
    }
  } catch(e) { showToast('⚠ Request failed', 4000); }
  await fetchStatus();
}

function downloadCSV() { window.location.href = '/api/export'; }

function showToast(msg, ms=3000) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', ms);
}

function fmtCountdown(s) {
  if (s >= 3600) return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  if (s >= 60) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return s + 's';
}

setInterval(async () => {
  countdown = Math.max(0, countdown - 1);
  document.getElementById('next-run').textContent = fmtCountdown(countdown);
  if (countdown === 0) { countdown = 14400; await fetchStatus(); await fetchJobs(); }
}, 1000);

// Poll status every 5s while running
setInterval(fetchStatus, 5000);

(async () => {
  await fetchStatus();
  await fetchJobs();
})();
</script>
</body>
</html>
"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/api/jobs")
def api_jobs():
    return jsonify(load_scored_jobs())


@app.route("/api/run", methods=["POST"])
def api_run():
    return jsonify(run_all())


@app.route("/api/export")
def api_export():
    path = DATA / "jobs_scored.csv"
    if not path.exists():
        return jsonify({"error": "No data yet. Run a scan first."}), 404
    return send_file(str(path), as_attachment=True, download_name="jobhawk_results.csv")


# ── scheduler & startup ───────────────────────────────────────────────────────

# Start scheduler when module loads (works with both gunicorn and direct run)
_scheduler = BackgroundScheduler(daemon=True)
_scheduler.add_job(run_all, "interval", hours=4, id="jobhawk_auto")
_scheduler.start()

# Run first scan in background immediately on startup
threading.Thread(target=run_all, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  🦅  JobHawk Web — Vaughn Krogman")
    print("=" * 55)
    print(f"  Dashboard:  http://localhost:{port}")
    print("  Auto-run:   every 4 hours")
    print("  Press Ctrl+C 