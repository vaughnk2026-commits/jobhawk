"""
JobHawk — Resume parsing + per-user job scoring.
"""

import re
from pathlib import Path
from typing import Dict, List

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
SKIP_DOMAINS = {"example.com", "noreply.com", "sentry.io", "email.com", "test.com", "domain.com"}

COMMON_SKILLS = [
    # Automotive / F&I
    "F&I", "finance manager", "special finance", "lender relations", "menu selling",
    "warranty penetration", "PVR", "back-end gross", "dealertrack", "PBS",
    "vinsolutions", "desking", "lease", "retail financing", "credit rebuild",
    # Sales & leadership
    "sales manager", "general sales manager", "business development",
    "CRM", "account executive", "regional sales", "dealer operations",
    "revenue growth", "team leadership", "KPI", "P&L", "forecasting",
    # Tech
    "python", "javascript", "react", "sql", "aws", "azure", "machine learning",
    "data analysis", "project management", "agile", "scrum", "product management",
    # General
    "customer service", "negotiation", "compliance", "digital marketing",
    "social media", "content", "copywriting", "excel", "powerpoint", "salesforce",
    "marketing", "saas", "implementation", "onboarding",
]


def find_contact_email(text: str):
    """Extract the first plausible contact email from job description text."""
    for m in EMAIL_RE.findall(text or ""):
        dom = m.split("@")[-1].lower()
        if dom not in SKIP_DOMAINS and "noreply" not in m.lower():
            return m
    return None


# ── Resume parsing ────────────────────────────────────────────────────────────

def parse_resume(file_path: str) -> Dict:
    """Extract raw text + skills list from a resume file (PDF or DOCX)."""
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

    return {
        "text": text,
        "skills": _extract_skills(text),
    }


def _parse_pdf(path: Path) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception as e:
        return ""


def _parse_docx(path: Path) -> str:
    try:
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        return ""


def _extract_skills(text: str) -> List[str]:
    text_lower = text.lower()
    found = []
    for skill in COMMON_SKILLS:
        if skill.lower() in text_lower:
            found.append(skill)
    return found


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(job: Dict, profile: Dict) -> int:
    """
    Score a job against a user profile (0-100).
    Higher = better match. Uses target roles, parsed skills, keywords, location.
    """
    blob = " ".join([
        job.get("title", ""),
        job.get("description", ""),
        job.get("location", ""),
        job.get("company", ""),
    ]).lower()

    score = 0

    # Target roles — highest weight
    for role in profile.get("target_roles", []):
        if role.lower() in blob:
            score += 20

    # Skills extracted from their resume
    for skill in profile.get("parsed_skills", []):
        if skill.lower() in blob:
            score += 5

    # User's custom keywords
    for kw in profile.get("keywords", []):
        if kw.lower() in blob:
            score += 4

    # Location match
    user_loc = (profile.get("location") or "").lower()
    if user_loc:
        for word in user_loc.split(","):
            word = word.strip()
            if word and word in blob:
                score += 8

    # Remote / Canada bonus
    if job.get("remote") or "remote" in blob:
        score += 3
    if "canada" in blob:
        score += 5

    # Penalise excluded terms
    for term in profile.get("exclude_terms", []):
        if term.lower() in blob:
            score -= 20

    return max(0, min(100, score))


def enrich_jobs(jobs: List[Dict], profile: Dict) -> List[Dict]:
    """Add match_score + email_found to each job for a specific user profile."""
    enriched = []
    for j in jobs:
        j = dict(j)
        j["match_score"] = score_job(j, profile)
        j["email_found"] = find_contact_email(j.get("description", ""))
        enriched.append(j)
    return sorted(enriched, key=lambda x: x.get("match_score", 0), reverse=True)
