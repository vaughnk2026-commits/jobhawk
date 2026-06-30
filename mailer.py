"""
JobHawk — Email utilities. Short timeouts to avoid hanging on cloud hosts.
"""

import logging
import os
import smtplib
import socket
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

SMTP_TIMEOUT = 8   # seconds — short to avoid hanging on Render


def _smtp_send(msg, from_email, password, host="smtp.gmail.com", port=587) -> bool:
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SMTP_TIMEOUT)
    try:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(from_email, password)
            srv.send_message(msg)
        return True
    except Exception as e:
        log.warning("SMTP send failed (%s:%s): %s", host, port, e)
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)


def send_application(job, user, profile, resume_path=None) -> bool:
    to_email = job.get("email_found")
    if not to_email:
        return False
    from_email = profile.get("email_from") or user.get("email", "")
    password   = profile.get("email_password") or os.environ.get("EMAIL_PASSWORD", "")
    if not from_email or not password:
        return False

    name = user.get("name") or "Job Applicant"
    roles = profile.get("target_roles", [])
    role_str = roles[0] if roles else "the posted role"
    loc = profile.get("location", "")

    msg = MIMEMultipart()
    msg["From"]    = f"{name} <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = f"Application — {job.get('title','')} | {name}"
    body = (
        f"Dear Hiring Team at {job.get('company','your organization')},\n\n"
        f"I am writing to express my interest in the {job.get('title', role_str)} position.\n\n"
        f"With my background in {role_str}, I am confident in my ability to deliver "
        f"meaningful results{' in ' + loc if loc else ''}. "
        f"I would welcome the opportunity to discuss how my experience aligns with your needs.\n\n"
        f"Please find my resume attached. I look forward to hearing from you.\n\n"
        f"Sincerely,\n{name}\n{loc}\n{from_email}"
    )
    msg.attach(MIMEText(body, "plain"))

    if resume_path:
        rp = Path(resume_path)
        if rp.exists():
            try:
                with open(rp, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{rp.name}"')
                msg.attach(part)
            except Exception as e:
                log.warning("Resume attach failed: %s", e)

    sent = _smtp_send(msg, from_email, password,
                      profile.get("smtp_host","smtp.gmail.com"),
                      int(profile.get("smtp_port", 587)))
    if sent:
        log.info("Applied -> %s for '%s' @ %s", to_email, job.get("title"), job.get("company"))
    return sent


def notify_scan_results(user: Dict, profile: Dict, found_jobs: List[Dict],
                        applied_jobs: List[Dict], opt_out_url: str = "") -> bool:
    """
    Send the user a scan-results email every time jobs are found.
    Uses the user's own SMTP credentials (email_from / email_password).
    Falls back to EMAIL_FROM / EMAIL_PASSWORD env vars.
    Always sends — regardless of whether auto-apply ran.
    """
    to_email   = user.get("email", "")
    from_email = (profile.get("email_from") or "").strip() or os.environ.get("EMAIL_FROM", "")
    password   = (profile.get("email_password") or "").strip() or os.environ.get("EMAIL_PASSWORD", "")
    smtp_host  = profile.get("smtp_host", "smtp.gmail.com") or "smtp.gmail.com"
    smtp_port  = int(profile.get("smtp_port") or 587)

    if not to_email or not from_email or not password:
        log.debug("notify_scan_results: missing credentials — skipping (to=%s from=%s)", to_email, from_email)
        return False

    name          = user.get("name") or "there"
    count_found   = len(found_jobs)
    count_applied = len(applied_jobs)

    # Subject line
    subject = f"JobHawk: {count_found} job{'s' if count_found != 1 else ''} found for you"
    if count_applied:
        subject += f" · {count_applied} applied"

    # Body
    top = found_jobs[:25]
    lines = []
    for i, j in enumerate(top, 1):
        remote_tag = " [Remote]" if j.get("remote") or "remote" in (j.get("location") or "").lower() else ""
        applied_tag = " ✅ Applied" if j in applied_jobs else ""
        lines.append(
            f"  {i:2}. {j.get('title','?')} @ {j.get('company','?')}{remote_tag}{applied_tag}\n"
            f"       {j.get('url','')}"
        )

    body_parts = [
        f"Hi {name},",
        "",
        f"JobHawk just completed a scan and found {count_found} job{'s' if count_found != 1 else ''} matching your profile.",
    ]

    if count_applied:
        body_parts.append(
            f"✅ Automatically applied to {count_applied} job{'s' if count_applied != 1 else ''} on your behalf "
            f"(contact email was found in the posting)."
        )
    else:
        body_parts.append(
            "No contact emails were found in these postings, so no auto-applications were sent this run. "
            "You can apply directly via the links below."
        )

    body_parts += [
        "",
        f"Top {len(top)} matches (sorted by your profile match score):",
        "",
    ]
    body_parts.extend(lines)
    body_parts += [
        "",
        f"View all jobs and track interviews on your dashboard:",
        "https://jobhawk-sbp1.onrender.com/dashboard",
        "",
        "— JobHawk",
    ]

    if opt_out_url:
        body_parts += [
            "",
            "─────────────────────────────────",
            f"To stop receiving these scan-result emails, click here:",
            opt_out_url,
        ]

    body = "\n".join(body_parts)

    msg = MIMEMultipart()
    msg["From"]    = f"JobHawk <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    sent = _smtp_send(msg, from_email, password, smtp_host, smtp_port)
    if sent:
        log.info("Scan digest sent to %s (%d found, %d applied)", to_email, count_found, count_applied)
    return sent


def notify_interview(user, job, platform_email=None, platform_password=None):
    to_email   = user.get("email")
    from_email = platform_email or os.environ.get("EMAIL_FROM","")
    password   = platform_password or os.environ.get("EMAIL_PASSWORD","")
    if not to_email or not from_email or not password: return
    name = user.get("name") or "there"
    body = (f"Hi {name},\n\nInterview opportunity:\n\n"
            f"Role:    {job.get('title','?')}\nCompany: {job.get('company','?')}\n"
            f"Link:    {job.get('url','')}\n\nGood luck!\n— JobHawk")
    msg = MIMEMultipart()
    msg["From"]    = f"JobHawk <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = f"Interview: {job.get('title','?')} at {job.get('company','?')}"
    msg.attach(MIMEText(body, "plain"))
    _smtp_send(msg, from_email, password)
