"""
JobHawk — Email utilities.
Handles both outbound job applications and inbound digest notifications to users.
"""

import logging
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


SMTP_TIMEOUT = 8  # seconds — short to avoid hanging on Render


def _smtp_send(msg: MIMEMultipart, from_email: str, password: str,
               host: str = "smtp.gmail.com", port: int = 587) -> bool:
    import socket
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SMTP_TIMEOUT)
    # Resend SMTP requires username "resend", not the email address
    smtp_user = "resend" if "resend.com" in host else from_email
    try:
        if int(port) == 465:
            # SSL connection (Resend, etc.)
            with smtplib.SMTP_SSL(host, int(port), timeout=SMTP_TIMEOUT) as srv:
                srv.login(smtp_user, password)
                srv.send_message(msg)
        else:
            # STARTTLS connection (Gmail port 587, etc.)
            with smtplib.SMTP(host, int(port), timeout=SMTP_TIMEOUT) as srv:
                srv.ehlo()
                srv.starttls()
                srv.login(smtp_user, password)
                srv.send_message(msg)
        return True
    except Exception as e:
        log.warning("SMTP send failed (%s:%s): %s", host, port, e)
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)


# ── Job application email ─────────────────────────────────────────────────────

def send_application(
    job: Dict,
    user: Dict,
    profile: Dict,
    resume_path: Optional[str] = None,
) -> bool:
    """
    Send a job application email on behalf of the user.
    Uses the user's own SMTP credentials (email_from + email_password in profile).
    Returns True if sent successfully.
    """
    to_email = job.get("email_found")
    if not to_email:
        return False

    from_email = profile.get("email_from") or user.get("email", "")
    password   = profile.get("email_password") or os.environ.get("EMAIL_PASSWORD", "")
    smtp_host  = profile.get("smtp_host", "smtp.gmail.com")
    smtp_port  = int(profile.get("smtp_port", 587))

    if not from_email or not password:
        log.debug("No email credentials for user %s — skipping application", user.get("id"))
        return False

    name = user.get("name") or "Job Applicant"
    location = profile.get("location", "")
    roles = profile.get("target_roles", [])
    role_str = roles[0] if roles else "the role"

    msg = MIMEMultipart()
    msg["From"]    = f"{name} <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = f"Application — {job.get('title', '')} | {name}"

    body = (
        f"Dear Hiring Team at {job.get('company', 'your organization')},\n\n"
        f"I am writing to express my interest in the {job.get('title', role_str)} position.\n\n"
        f"With my background in {role_str}, I am confident in my ability to contribute "
        f"meaningfully to your team{' in ' + location if location else ''}. "
        f"I have a strong track record of delivering results and would welcome the "
        f"opportunity to discuss how my experience aligns with your needs.\n\n"
        f"Please find my resume attached. I look forward to hearing from you.\n\n"
        f"Sincerely,\n{name}\n{location}\n{from_email}"
    )
    msg.attach(MIMEText(body, "plain"))

    # Attach resume if available
    if resume_path:
        rp = Path(resume_path)
        if rp.exists():
            try:
                with open(rp, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", f'attachment; filename="{rp.name}"'
                )
                msg.attach(part)
            except Exception as e:
                log.warning("Could not attach resume: %s", e)

    sent = _smtp_send(msg, from_email, password, smtp_host, smtp_port)
    if sent:
        log.info("Application sent -> %s for '%s' @ %s (user %s)",
                 to_email, job.get("title"), job.get("company"), user.get("id"))
    return sent


# ── User digest notifications ─────────────────────────────────────────────────

def notify_user_digest(
    user: Dict,
    profile: Dict,
    applied_jobs: List[Dict],
    platform_email: Optional[str] = None,
    platform_password: Optional[str] = None,
):
    """
    Send the user a digest email listing the jobs that were applied to on their behalf.
    Uses platform email credentials (EMAIL_FROM / EMAIL_PASSWORD env vars) to send TO the user.
    """
    if not applied_jobs:
        return

    to_email = user.get("email")
    if not to_email:
        return

    # Platform sending credentials
    from_email = platform_email or os.environ.get("EMAIL_FROM", "")
    password   = platform_password or os.environ.get("EMAIL_PASSWORD", "")
    smtp_host  = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port  = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
    if not from_email or not password:
        log.warning("notify_user_digest: missing credentials — to=%s from=%s", to_email, bool(from_email))
        return

    name  = user.get("name") or "there"
    count = len(applied_jobs)

    lines = []
    for j in applied_jobs[:50]:   # cap list at 50
        lines.append(
            f"  • {j.get('title','?')} at {j.get('company','?')} "
            f"[{j.get('source','?')}] — {j.get('url','')}"
        )
    jobs_text = "\n".join(lines)

    body = (
        f"Hi {name},\n\n"
        f"JobHawk just applied to {count} job{'s' if count != 1 else ''} on your behalf:\n\n"
        f"{jobs_text}\n\n"
        f"Log in to your JobHawk dashboard to view all applications, mark interviews, "
        f"and track your progress.\n\n"
        f"— The JobHawk Team"
    )

    msg = MIMEMultipart()
    msg["From"]    = f"JobHawk <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = f"JobHawk applied to {count} job{'s' if count != 1 else ''} for you"
    msg.attach(MIMEText(body, "plain"))

    sent = _smtp_send(msg, from_email, password, smtp_host, smtp_port)
    if sent:
        log.info("Digest sent to %s (%d applied)", to_email, count)
    else:
        log.warning("Digest FAILED for %s", to_email)


def notify_interview(user: Dict, job: Dict,
                     platform_email: Optional[str] = None,
                     platform_password: Optional[str] = None):
    """Send the user an email when a job is moved to Interview status."""
    to_email = user.get("email")
    if not to_email:
        return

    from_email = platform_email or os.environ.get("EMAIL_FROM", "")
    password   = platform_password or os.environ.get("EMAIL_PASSWORD", "")
    smtp_host  = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port  = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
    if not from_email or not password:
        return

    name = user.get("name") or "there"
    body = (
        f"Hi {name},\n\n"
        f"Great news — you have an interview opportunity!\n\n"
        f"Role:    {job.get('title','?')}\nCompany: {job.get('company','?')}\n"
        f"Link:    {job.get('url','')}\n\n"
        f"Log in to your JobHawk dashboard to add notes and track next steps.\n\n"
        f"Good luck!\n— The JobHawk Team"
    )

    msg = MIMEMultipart()
    msg["From"]    = f"JobHawk <{from_email}>"
    msg["To"]      = to_email
    msg["Subject"] = f"Interview opportunity: {job.get('title','?')} at {job.get('company','?')}"
    msg.attach(MIMEText(body, "plain"))

    _smtp_send(msg, from_email, password, smtp_host, smtp_port)
