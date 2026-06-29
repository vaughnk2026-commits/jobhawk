"""
JobHawk — Resume parsing + per-user job scoring.
Location guard: non-remote jobs outside the user's location are excluded.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
SKIP_DOMAINS = {"example.com","noreply.com","sentry.io","email.com","test.com","domain.com"}

COMMON_SKILLS = [
    "F&I","finance manager","special finance","lender relations","menu selling",
    "warranty penetration","PVR","back-end gross","dealertrack","PBS","vinsolutions",
    "desking","lease","retail financing","credit rebuild",
    "sales manager","general sales manager","business development","CRM",
    "account executive","regional sales","dealer operations",
    "revenue growth","team leadership","KPI","P&L","forecasting",
    "python","javascript","react","sql","aws","azure","machine learning",
    "data analysis","project management","agile","scrum","product management",
    "customer service","negotiation","compliance","digital marketing",
    "social media","content","copywriting","excel","powerpoint","salesforce",
    "marketing","saas","implementation","onboarding",
]


def find_contact_email(text: str) -> Optional[str]:
    for m in EMAIL_RE.findall(text or ""):
        dom = m.split("@")[-1].lower()
        if dom not in SKIP_DOMAINS and "noreply" not in m.lower():
            return m
    return None


# ── Location guard ────────────────────────────────────────────────────────────

def _location_words(loc: str) -> List[str]:
    """Split a location string into meaningful tokens (length > 2)."""
    return [p.strip().lower() for p in re.split(r"[,/\s]+", loc or "") if len(p.strip()) > 2]


def location_ok(job: Dict, profile: Dict) -> bool:
    """
    Return True if the job is eligible for this user's location.

    Rules:
    - Remote jobs: always OK.
    - No location set in profile: allow everything.
    - Non-remote jobs: job location must contain at least one word
      from the user's configured location (city, province, country).
    """
    job_loc  = (job.get("location") or "").lower()
    is_remote = (
        job.get("remote")
        or "remote" in job_loc
        or "anywhere" in job_loc
        or "worldwide" in job_loc
        or not job_loc                 # unknown location → be permissive
    )
    if is_remote:
        return True

    user_loc = (profile.get("location") or "").strip()
    if not user_loc:
        return True   # user hasn't set a location — don't filter

    for word in _location_words(user_loc):
        if word in job_loc:
            return True

    return False


# ── Resume parsing ────────────────────────────────────────────────────────────

def parse_resume(file_path: str) -> Dict:
    path = Path(file_path)
    text = ""
    if not path.exists():
        return {"text": "", "skills": []}
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _parse_pdf(path)
    elif suffix in (".docx", ".doc"):
        text = _parse_docx(path)
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    return {"text": text, "skills": _extract_skills(text)}


def _parse_pdf(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception:
        return ""


def _parse_docx(path: Path) -> str:
    try:
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    except Exception:
        return ""


def _extract_skills(text: str) -> List[str]:
    tl = text.lower()
    return [s for s in COMMON_SKILLS if s.lower() in tl]


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(job: Dict, profile: Dict) -> int:
    blob = " ".join([
        job.get("title",""), job.get("description",""),
        job.get("location",""), job.get("company",""),
    ]).lower()
    score = 0
    for role in profile.get("target_roles", []):
        if role.lower() in blob: score += 20
    for skill in profile.get("parsed_skills", []):
        if skill.lower() in blob: score += 5
    for kw in profile.get("keywords", []):
        if kw.lower() in blob: score += 4
    user_loc = (profile.get("location") or "").lower()
    for word in _location_words(user_loc):
        if word in blob: score += 8
    if job.get("remote") or "remote" in blob: score += 3
    if "canada" in blob: score += 5
    for term in profile.get("exclude_terms", []):
        if term.lower() in blob: score -= 20
    return max(0, min(100, score))


def enrich_jobs(jobs: List[Dict], profile: Dict) -> List[Dict]:
    """
    Score jobs against user profile.
    Non-remote jobs outside the user's location are dropped entirely.
    """
    enriched = []
    for j in jobs:
        j = dict(j)
        if not location_ok(j, profile):
            continue                          # skip — wrong city/province
        j["match_score"] = score_job(j, profile)
        j["email_found"] = find_contact_email(j.get("description", ""))
        enriched.append(j)
    return sorted(enriched, key=lambda x: x.get("match_score", 0), reverse=True)
