"""
JobHawk — Multi-user database layer (SQLite).
All tables scoped by user_id so each user's data is fully isolated.
"""

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(__file__).resolve().parent

# Use DATABASE_PATH env var if set.
# Otherwise try /var/data/ (Render persistent disk mount point),
# then fall back to local data/ directory.
_db_env = os.environ.get("DATABASE_PATH", "")
if _db_env:
    DB_PATH = Path(_db_env)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
else:
    for _candidate in [Path("/var/data"), BASE / "data"]:
        try:
            _candidate.mkdir(parents=True, exist_ok=True)
            # Quick write test
            _test = _candidate / ".writable"
            _test.write_text("1"); _test.unlink()
            DB_PATH = _candidate / "jobhawk.db"
            break
        except Exception:
            continue
    else:
        DB_PATH = BASE / "data" / "jobhawk.db"

_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                name          TEXT DEFAULT '',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                notify_email  INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS passkeys (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id),
                credential_id BLOB UNIQUE NOT NULL,
                public_key    BLOB NOT NULL,
                sign_count    INTEGER DEFAULT 0,
                label         TEXT DEFAULT 'Passkey',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS profiles (
                user_id           INTEGER PRIMARY KEY REFERENCES users(id),
                country           TEXT DEFAULT '',
                province          TEXT DEFAULT '',
                city              TEXT DEFAULT '',
                location          TEXT DEFAULT '',
                target_roles      TEXT DEFAULT '[]',
                keywords          TEXT DEFAULT '[]',
                exclude_terms     TEXT DEFAULT '["entry level","unpaid","internship","volunteer only"]',
                min_score         INTEGER DEFAULT 10,
                resume_path       TEXT DEFAULT '',
                resume_name       TEXT DEFAULT '',
                cover_letter_path TEXT DEFAULT '',
                cover_letter_name TEXT DEFAULT '',
                parsed_skills     TEXT DEFAULT '[]',
                email_from        TEXT DEFAULT '',
                email_password    TEXT DEFAULT '',
                smtp_host         TEXT DEFAULT 'smtp.gmail.com',
                smtp_port         INTEGER DEFAULT 587
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT NOT NULL,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                title       TEXT,
                company     TEXT,
                location    TEXT,
                url         TEXT,
                source      TEXT,
                match_score INTEGER DEFAULT 0,
                status      TEXT DEFAULT 'new',
                email_found TEXT,
                applied_at  TEXT,
                notes       TEXT,
                first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id, user_id)
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                jobs_found INTEGER DEFAULT 0,
                status     TEXT DEFAULT 'ok'
            );
        """)
        # Migrations: add new columns to existing databases
        for col, defval in [
            ("country",  "''"),
            ("province", "''"),
            ("city",     "''"),
        ]:
            try:
                c.execute(f"ALTER TABLE profiles ADD COLUMN {col} TEXT DEFAULT {defval}")
            except sqlite3.OperationalError:
                pass  # column already exists
        c.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except Exception:
        return []


# ── Users ─────────────────────────────────────────────────────────────────────

def user_create(email: str, password_hash: str, name: str = "") -> int:
    email = email.lower().strip()
    with _lock:
        with _db() as c:
            cur = c.execute(
                "INSERT INTO users (email, password_hash, name) VALUES (?,?,?)",
                (email, password_hash, name),
            )
            uid = cur.lastrowid
            c.execute("INSERT OR IGNORE INTO profiles (user_id) VALUES (?)", (uid,))
            c.commit()
    return uid


def user_by_email(email: str) -> Optional[Dict]:
    with _db() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email=?", (email.lower().strip(),)
        ).fetchone()
    return dict(row) if row else None


def user_by_id(uid: int) -> Optional[Dict]:
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def user_all_ids() -> List[int]:
    with _db() as c:
        rows = c.execute("SELECT id FROM users").fetchall()
    return [r["id"] for r in rows]


# ── Profiles ──────────────────────────────────────────────────────────────────

def profile_get(uid: int) -> Dict:
    with _db() as c:
        row = c.execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return {
            "user_id": uid, "country": "", "province": "", "city": "",
            "location": "", "target_roles": [], "keywords": [],
            "exclude_terms": [], "min_score": 10, "resume_path": "",
            "resume_name": "", "cover_letter_path": "", "cover_letter_name": "",
            "parsed_skills": [], "email_from": "", "email_password": "",
            "smtp_host": "smtp.gmail.com", "smtp_port": 587,
        }
    p = dict(row)
    for k in ("target_roles", "keywords", "exclude_terms", "parsed_skills"):
        p[k] = _json_list(p.get(k))
    return p


def profile_update(uid: int, **kwargs):
    for k in ("target_roles", "keywords", "exclude_terms", "parsed_skills"):
        if k in kwargs and isinstance(kwargs[k], list):
            kwargs[k] = json.dumps(kwargs[k])
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [uid]
    with _lock:
        with _db() as c:
            c.execute(f"UPDATE profiles SET {sets} WHERE user_id=?", vals)
            c.commit()


# ── Passkeys ──────────────────────────────────────────────────────────────────

def passkey_store(uid: int, credential_id: bytes, public_key: bytes, label: str = "Passkey"):
    with _lock:
        with _db() as c:
            c.execute(
                "INSERT OR REPLACE INTO passkeys (user_id, credential_id, public_key, label) VALUES (?,?,?,?)",
                (uid, credential_id, public_key, label),
            )
            c.commit()


def passkey_by_credential_id(credential_id: bytes) -> Optional[Dict]:
    with _db() as c:
        row = c.execute(
            "SELECT * FROM passkeys WHERE credential_id=?", (credential_id,)
        ).fetchone()
    return dict(row) if row else None


def passkey_update_sign_count(credential_id: bytes, sign_count: int):
    with _lock:
        with _db() as c:
            c.execute(
                "UPDATE passkeys SET sign_count=? WHERE credential_id=?",
                (sign_count, credential_id),
            )
            c.commit()


def passkeys_for_user(uid: int) -> List[Dict]:
    with _db() as c:
        rows = c.execute("SELECT * FROM passkeys WHERE user_id=?", (uid,)).fetchall()
    return [dict(r) for r in rows]


# ── Jobs ──────────────────────────────────────────────────────────────────────

def job_upsert(uid: int, job: Dict) -> str:
    jid = job.get("url") or f"{job.get('company', '')}|{job.get('title', '')}"
    with _lock:
        with _db() as c:
            c.execute(
                """INSERT OR IGNORE INTO jobs
                   (id, user_id, title, company, location, url, source, match_score, email_found)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    jid, uid,
                    job.get("title"), job.get("company"), job.get("location"),
                    job.get("url"), job.get("source"),
                    job.get("match_score", 0), job.get("email_found"),
                ),
            )
            c.execute(
                "UPDATE jobs SET match_score=? WHERE id=? AND user_id=? AND status='new'",
                (job.get("match_score", 0), jid, uid),
            )
            c.commit()
    return jid


def job_mark_applied(uid: int, jid: str, email_found: Optional[str] = None):
    with _lock:
        with _db() as c:
            c.execute(
                """UPDATE jobs SET status='applied', applied_at=CURRENT_TIMESTAMP,
                              email_found=COALESCE(?,email_found)
                   WHERE id=? AND user_id=? AND status='new'""",
                (email_found, jid, uid),
            )
            c.commit()


def job_update_status(uid: int, jid: str, status: str, notes: str = ""):
    with _lock:
        with _db() as c:
            c.execute(
                "UPDATE jobs SET status=?, notes=? WHERE id=? AND user_id=?",
                (status, notes, jid, uid),
            )
            c.commit()


def jobs_all(uid: int) -> List[Dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY match_score DESC",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def jobs_applied(uid: int) -> List[Dict]:
    with _db() as c:
        rows = c.execute(
            """SELECT * FROM jobs WHERE user_id=?
               AND status IN ('applied','interview','offer','rejected')
               ORDER BY applied_at DESC""",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def jobs_interviews(uid: int) -> List[Dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE user_id=? AND status='interview' ORDER BY applied_at DESC",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def jobs_new_since_last_notify(uid: int, since_ts: str) -> List[Dict]:
    """Jobs that were applied to after since_ts (for digest emails)."""
    with _db() as c:
        rows = c.execute(
            """SELECT * FROM jobs WHERE user_id=? AND status='applied'
               AND applied_at > ? ORDER BY applied_at DESC""",
            (uid, since_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def db_stats(uid: int) -> Dict:
    with _db() as c:
        total     = c.execute("SELECT COUNT(*) FROM jobs WHERE user_id=?", (uid,)).fetchone()[0]
        applied   = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id=? AND status IN ('applied','interview','offer','rejected')",
            (uid,),
        ).fetchone()[0]
        interview = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id=? AND status='interview'", (uid,)
        ).fetchone()[0]
    return {"total": total, "applied": applied, "interview": interview}
