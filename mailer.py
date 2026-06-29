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


def notify_user_digest(user, profile, applied_jobs, platform_email=None, platform_password=None):
    if not applied_jobs: return
    to_email  = user.get("email")
    from_email = platform_email or os.environ.get("EMAIL_FROM","")
    password   = platform_password or os.environ.get("EMAIL_PASSWORD","")
    if not to_email or not from_email or not password: return
    name  = user.get("name") or "there"
    count = len(applied_jobs)
    lines = [f"  • {j.get('title','?')} at {j.get('company','?')} [{j.get('source','?')}] — {j.get('url','')}"
             for j in applied_jobs[:50]]
    body = (f"Hi {name},\n\nJobHawk applied to {count} job{'s' if count!=1 else ''} on your behalf:\n\n"
            + "\n".join(lines)
            + "\n\nLog in to your dashboard to track progress and mark interviews.\n\n— JobHawk")
    msg = MIMEMultipart()
    msg["From"]    = f"JobHawk <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = f"JobHawk applied to {count} job{'s' if count!=1 else ''} for you"
    msg.attach(MIMEText(body, "plain"))
    _smtp_send(msg, from_email, password)


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
