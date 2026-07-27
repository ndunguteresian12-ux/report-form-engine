import os
import re
import uuid
import random
import secrets
import urllib.parse
import logging
import bcrypt
import psycopg2
import requests as http_requests
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from supabase import create_client
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine

# Ensure load_dotenv is called immediately
load_dotenv()
# --- Security: Direct bcrypt implementation ---
def get_password_hash(password: str) -> str:
    """Hashes a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a password against a stored hash."""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

# --- Logging & Initialization ---
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("cbe_engine")

# Support contact shown to school admins/staff so they have someone to call
# when something goes wrong — set these in Render's Environment tab. If
# unset, the support line just doesn't show (no broken/empty contact info).
SUPPORT_PHONE = (os.getenv("SUPPORT_PHONE") or "").strip() or None
SUPPORT_EMAIL = (os.getenv("SUPPORT_EMAIL") or "").strip() or None

# "Backup Now" button config — triggers the existing GitHub Actions backup
# workflow (scripts/backup_db.py + .github/workflows/db-backup.yml) on demand
# via GitHub's API, instead of waiting for its nightly schedule. Reuses the
# same pg_dump + Supabase upload pipeline already set up; pg_dump itself
# isn't available inside this Render service, so triggering the workflow
# that already has it (via GitHub Actions' own runner) is the reliable path.
GITHUB_PAT = (os.getenv("GITHUB_PAT") or "").strip() or None
GITHUB_REPO = (os.getenv("GITHUB_REPO") or "").strip() or None  # format: "username/repo-name"
GITHUB_BACKUP_WORKFLOW_FILE = (os.getenv("GITHUB_BACKUP_WORKFLOW_FILE") or "db-backup.yml").strip()

TOAST_CONTAINER_HTML = """
<div id="toast-container" style="position:fixed; top:20px; right:20px; z-index:9999; display:flex; flex-direction:column; gap:8px;"></div>
<script>
function showToast(message, type) {
    var container = document.getElementById('toast-container');
    if (!container) return;
    var colors = {success: '#059669', error: '#dc2626', warning: '#d97706'};
    var toast = document.createElement('div');
    toast.style.cssText = "background:" + (colors[type] || colors.success) + ";color:white;padding:12px 20px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,0.18);font-family:'Plus Jakarta Sans',sans-serif;font-size:13px;font-weight:600;opacity:0;transform:translateX(24px);transition:all 0.3s ease;max-width:320px;";
    toast.textContent = message;
    container.appendChild(toast);
    requestAnimationFrame(function() { toast.style.opacity = '1'; toast.style.transform = 'translateX(0)'; });
    setTimeout(function() {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(24px)';
        setTimeout(function() { toast.remove(); }, 300);
    }, 4200);
}
</script>
"""

def toast_trigger(message: str, toast_type: str = "success") -> str:
    """Returns a script tag that shows a toast on page load. Use for
    transient one-time confirmations (e.g. 'Student added') — not for
    persistent warnings someone needs to actually act on, which should stay
    as a real, visible banner instead."""
    safe_message = esc(message).replace("'", "\\'")
    return f"<script>document.addEventListener('DOMContentLoaded', function() {{ showToast('{safe_message}', '{toast_type}'); }});</script>"

def support_contact_html() -> str:
    """A small 'need help?' line for dashboard footers. Returns an empty
    string if no support contact has been configured."""
    if not SUPPORT_PHONE and not SUPPORT_EMAIL:
        return ""
    parts = []
    if SUPPORT_PHONE:
        parts.append(f"📞 <a href='tel:{esc(SUPPORT_PHONE)}' class='underline hover:text-slate-500'>{esc(SUPPORT_PHONE)}</a>")
    if SUPPORT_EMAIL:
        parts.append(f"✉️ <a href='mailto:{esc(SUPPORT_EMAIL)}' class='underline hover:text-slate-500'>{esc(SUPPORT_EMAIL)}</a>")
    return f"<p class='text-center text-[11px] text-slate-400 pb-2'>Need help? {' &nbsp;·&nbsp; '.join(parts)}</p>"

ELIMU_HUB_ICON_DATA_URI = "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iOTYiIGhlaWdodD0iOTYiIHZpZXdCb3g9IjAgMCA5NiA5NiIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KICA8ZGVmcz4KICAgIDxsaW5lYXJHcmFkaWVudCBpZD0iaHViR3JhZGllbnRJY29uIiB4MT0iMCUiIHkxPSIwJSIgeDI9IjEwMCUiIHkyPSIxMDAlIj4KICAgICAgPHN0b3Agb2Zmc2V0PSIwJSIgc3RvcC1jb2xvcj0iIzBkOTQ4OCIvPgogICAgICA8c3RvcCBvZmZzZXQ9IjEwMCUiIHN0b3AtY29sb3I9IiM0ZjQ2ZTUiLz4KICAgIDwvbGluZWFyR3JhZGllbnQ+CiAgPC9kZWZzPgoKICA8cmVjdCB3aWR0aD0iOTYiIGhlaWdodD0iOTYiIHJ4PSIyMCIgZmlsbD0iI0Y3RjlGOCIvPgoKICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSg3LCA1KSI+CiAgICA8cG9seWdvbiBwb2ludHM9IjQ0LDIgODIsMjMgODIsNjUgNDQsODYgNiw2NSA2LDIzIgogICAgICBmaWxsPSJub25lIiBzdHJva2U9InVybCgjaHViR3JhZGllbnRJY29uKSIgc3Ryb2tlLXdpZHRoPSI1LjUiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4KCiAgICA8ZyB0cmFuc2Zvcm09InRyYW5zbGF0ZSg0NCwgNDQpIj4KICAgICAgPHBhdGggZD0iTTAsLTYgQyAtMTQsLTE0IC0yNiwtMTIgLTI2LC0xMiBMIC0yNiwxNCBDIC0yNiwxNCAtMTQsMTIgMCwyMCBaIgogICAgICAgIGZpbGw9InVybCgjaHViR3JhZGllbnRJY29uKSIgb3BhY2l0eT0iMC45MiIvPgogICAgICA8cGF0aCBkPSJNMCwtNiBDIDE0LC0xNCAyNiwtMTIgMjYsLTEyIEwgMjYsMTQgQyAyNiwxNCAxNCwxMiAwLDIwIFoiCiAgICAgICAgZmlsbD0idXJsKCNodWJHcmFkaWVudEljb24pIiBvcGFjaXR5PSIwLjc1Ii8+CiAgICAgIDxsaW5lIHgxPSIwIiB5MT0iLTYiIHgyPSIwIiB5Mj0iMjAiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMS44IiBvcGFjaXR5PSIwLjYiLz4KICAgIDwvZz4KCiAgICA8Y2lyY2xlIGN4PSI0NCIgY3k9IjIiIHI9IjMuNiIgZmlsbD0iIzRmNDZlNSIvPgogICAgPGNpcmNsZSBjeD0iODIiIGN5PSIyMyIgcj0iMy42IiBmaWxsPSIjMGQ5NDg4Ii8+CiAgICA8Y2lyY2xlIGN4PSI4MiIgY3k9IjY1IiByPSIzLjYiIGZpbGw9IiMwZDk0ODgiLz4KICAgIDxjaXJjbGUgY3g9IjQ0IiBjeT0iODYiIHI9IjMuNiIgZmlsbD0iIzRmNDZlNSIvPgogICAgPGNpcmNsZSBjeD0iNiIgY3k9IjY1IiByPSIzLjYiIGZpbGw9IiMwZDk0ODgiLz4KICAgIDxjaXJjbGUgY3g9IjYiIGN5PSIyMyIgcj0iMy42IiBmaWxsPSIjMGQ5NDg4Ii8+CiAgPC9nPgo8L3N2Zz4K"

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip() or None
# Supabase renamed its API keys for newer projects: the old "service_role"
# key (needed here for server-side writes past bucket RLS) is now called
# the "secret" key. Accept whichever name is actually present, so schools
# don't have to duplicate the same value under a second env var name.
SUPABASE_KEY = (
    os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or ""
).strip() or None
supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

if supabase_client:
    logger.info("Supabase Storage configured — logo uploads will be persisted to the cloud.")
else:
    logger.warning(
        "Supabase Storage NOT configured (need SUPABASE_URL, and one of SUPABASE_KEY / SUPABASE_SECRET_KEY / SUPABASE_SERVICE_ROLE_KEY). "
        "Logo uploads will fall back to local disk, which is NOT persistent on Render."
    )

# Tracks the most recent Supabase Storage error so an admin can self-diagnose
# via /admin/system/diagnostics/{school_id} without needing server log access.
_last_storage_error = None

# --- SMS provider configuration (Africa's Talking) ---
# Set these on Render to enable real SMS delivery for password-reset codes.
# Until then, reset codes are only logged server-side (a clearly-labeled
# simulation) so the feature can be tested end-to-end without a live account.
AT_USERNAME = (os.getenv("AFRICASTALKING_USERNAME") or "").strip() or None
AT_API_KEY = (os.getenv("AFRICASTALKING_API_KEY") or "").strip() or None
AT_SENDER_ID = (os.getenv("AFRICASTALKING_SENDER_ID") or "").strip() or None
_sms_configured = bool(AT_USERNAME and AT_API_KEY)

if _sms_configured:
    logger.info("Africa's Talking SMS configured — password-reset codes will be sent via real SMS.")
else:
    logger.warning(
        "Africa's Talking SMS NOT configured (AFRICASTALKING_USERNAME / AFRICASTALKING_API_KEY missing). "
        "Password-reset codes will only be logged server-side (simulated SMS) until configured."
    )

# Tracks the most recent SMS send error/result, same self-diagnosis pattern as Supabase Storage.
_last_sms_error = None

def send_sms(phone_number: str, message: str) -> bool:
    """Sends an SMS via Africa's Talking if configured; otherwise logs the
    message as a simulated send. Returns True if a real send succeeded or a
    simulated send was logged, False only on a genuine sending failure."""
    global _last_sms_error

    if not _sms_configured:
        logger.info(f"[SIMULATED SMS] To: {phone_number} | Message: {message}")
        return True

    try:
        response = http_requests.post(
            "https://api.africastalking.com/version1/messaging",
            headers={
                "apiKey": AT_API_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "username": AT_USERNAME,
                "to": phone_number,
                "message": message,
                **({"from": AT_SENDER_ID} if AT_SENDER_ID else {}),
            },
            timeout=10,
        )
        response.raise_for_status()
        _last_sms_error = None
        return True
    except Exception as sms_err:
        _last_sms_error = f"{type(sms_err).__name__}: {sms_err}"
        logger.error(f"SMS send failed: {_last_sms_error}")
        return False


app = FastAPI(title="Kenyan CBE Multi-Tenant Enterprise Engine")

# --- CSRF protection (Origin/Referer validation) ---
# Deliberately NOT a per-form-token approach: retrofitting a hidden token
# field into every one of this app's 50+ existing forms risks silently
# breaking a live school's ability to submit some form if even one is
# missed. Origin/Referer validation requires ZERO changes to any existing
# HTML — it's a pure server-side check that only rejects the one thing a
# real CSRF attack actually looks like: a state-changing request whose
# Origin/Referer explicitly names a DIFFERENT site. If the header is
# simply absent (some legitimate clients omit it), the request is let
# through rather than risking a false block on traffic we didn't
# anticipate — this only ever blocks requests that positively prove
# they came from elsewhere, never requests we're merely unsure about.
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

def _hostname_from_url(url_str: str):
    try:
        return urllib.parse.urlparse(url_str).hostname
    except Exception:
        return None

@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    if request.method not in CSRF_SAFE_METHODS:
        expected_host = request.headers.get("host", "").split(":")[0].lower()
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and expected_host:
            actual_host = _hostname_from_url(origin)
            if actual_host and actual_host.lower() != expected_host:
                logger.warning(f"Blocked cross-site request: {request.method} {request.url.path} from origin/referer host '{actual_host}' (expected '{expected_host}')")
                return _branded_error_page(403, "This request could not be verified as coming from this site. Please go back and try again.")
    return await call_next(request)

# --- Security headers ---
# Adds standard hardening headers to every response. This only *adds*
# headers — it never blocks, rewrites, or rejects a request — so it can't
# break any existing page, form, or session for schools already live.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # Only sent when the request actually arrived over HTTPS — Render
    # terminates TLS in front of the app, so this is safe in production and
    # simply won't fire during local HTTP development.
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

def _branded_error_page(status_code: int, message: str) -> HTMLResponse:
    return HTMLResponse(status_code=status_code, content=f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | {status_code}</title>
        <link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F7F9F8] min-h-screen flex items-center justify-center p-4">
        <div class="bg-white p-8 rounded-2xl border shadow-md max-w-md w-full text-center">
            <img src="{ELIMU_HUB_ICON_DATA_URI}" class="w-14 h-14 mx-auto mb-4 rounded-2xl" alt="">
            <p class="text-5xl font-black text-slate-200 mb-2">{status_code}</p>
            <p class="text-sm text-slate-600 mb-6">{esc(message)}</p>
            <a href="/login" class="bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-2.5 px-6 rounded-xl text-sm transition inline-block">Back to Login</a>
        </div>
    </body>
    </html>
    """)

@app.exception_handler(HTTPException)
async def branded_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        return _branded_error_page(404, "That page doesn't exist, or the link may be out of date.")
    return _branded_error_page(exc.status_code, str(exc.detail))

@app.exception_handler(Exception)
async def branded_unhandled_exception_handler(request: Request, exc: Exception):
    # Log the real error server-side (with full detail for debugging), but
    # never show a raw stack trace to the browser — that's both unfriendly
    # and a minor information-leak risk.
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    return _branded_error_page(500, "Something went wrong on our end. The team behind Elimu Hub has been notified — please try again shortly.")

# --- Configuration Constants ---
ALLOWED_LOGO_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5MB
UPLOAD_DIR = "uploads"

# Ensure the directory exists
import os
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Serve locally-saved logos (fallback path only — Supabase Storage is primary
# and durable; this local mount is a safety net and will NOT survive a
# redeploy on Render since local disk is ephemeral there).
app.mount(f"/{UPLOAD_DIR}", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- Shared helpers (DB pool, auth checks, subject ordering) ---
# Moved to shared.py so timetable_routes.py can use them too without a
# circular import between it and main.py.
from shared import (
    esc,
    get_db_connection,
    require_school_session,
    require_admin_session,
    get_dashboard_url,
    require_superadmin_session,
    sort_subjects_for_display,
    abbreviate_subject,
    with_query_param,
    full_student_name,
)


# --- Login rate limiting (brute-force protection) ---
# DB-backed (not an in-memory dict) so it works correctly across multiple
# gunicorn worker processes, which don't share memory.
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_MINUTES = 15

def is_login_rate_limited(cur, identifier: str) -> bool:
    """Returns True if this identifier (email, lowercased) has had too many
    failed login attempts within the recent window."""
    cur.execute(f"""
        SELECT COUNT(*) AS cnt FROM login_attempts
        WHERE identifier = %s AND attempted_at > NOW() - INTERVAL '{LOGIN_RATE_LIMIT_WINDOW_MINUTES} minutes';
    """, (identifier,))
    return cur.fetchone()['cnt'] >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS

def record_failed_login(cur, identifier: str):
    cur.execute("INSERT INTO login_attempts (identifier) VALUES (%s);", (identifier,))
    # Opportunistic cleanup so this table doesn't grow unbounded.
    cur.execute("DELETE FROM login_attempts WHERE attempted_at < NOW() - INTERVAL '1 day';")

def clear_failed_logins(cur, identifier: str):
    cur.execute("DELETE FROM login_attempts WHERE identifier = %s;", (identifier,))

# 2. Bootstrap Function
def bootstrap_database_schema():
    """Initializes tables and populates base data."""
    logger.info("Bootstrapping database schema...")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Create Tables
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schools (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    sub_county VARCHAR(255) NOT NULL,
                    physical_address VARCHAR(255) NOT NULL,
                    logo_url VARCHAR(512),
                    wallet_balance NUMERIC(12, 2) DEFAULT 0.00,
                    theme_color VARCHAR(50) DEFAULT 'emerald',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW(),
                    terms_accepted_at TIMESTAMP
                );

                -- Safe, idempotent migration for schools that already existed
                -- before this version — they default to 'active' so nobody
                -- already using the system gets locked out.
                ALTER TABLE schools ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active';
                ALTER TABLE schools ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE schools ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP;

                CREATE TABLE IF NOT EXISTS school_settings (
                    school_id INTEGER PRIMARY KEY REFERENCES schools(id) ON DELETE CASCADE,
                    active_year INTEGER DEFAULT 2026,
                    active_term VARCHAR(20) DEFAULT 'Term 1',
                    active_cycle VARCHAR(20) DEFAULT 'End Term',
                    opening_date VARCHAR(50) DEFAULT 'To Be Announced',
                    closing_date VARCHAR(50) DEFAULT 'To Be Announced',
                    is_single_stream BOOLEAN DEFAULT FALSE
                );

                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) NOT NULL,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    is_verified BOOLEAN DEFAULT TRUE,
                    full_name VARCHAR(255),
                    tsc_number VARCHAR(100),
                    phone_number VARCHAR(50)
                );

                -- Safe, idempotent migrations for the fields above on a table that
                -- already existed in production before this version.
                ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS tsc_number VARCHAR(100);
                ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(50);

                CREATE TABLE IF NOT EXISTS password_resets (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    reset_code VARCHAR(10) NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS login_attempts (
                    id SERIAL PRIMARY KEY,
                    identifier VARCHAR(255) NOT NULL,
                    attempted_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_login_attempts_identifier ON login_attempts (identifier, attempted_at);

                CREATE TABLE IF NOT EXISTS platform_announcements (
                    id SERIAL PRIMARY KEY,
                    message TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    actor_label VARCHAR(255),
                    action VARCHAR(100) NOT NULL,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_audit_log_school_time ON audit_log (school_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS classes (
                    id SERIAL PRIMARY KEY,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS students (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    class_id INTEGER REFERENCES classes(id) ON DELETE CASCADE,
                    admission_number VARCHAR(100) NOT NULL,
                    first_name VARCHAR(100) NOT NULL,
                    last_name VARCHAR(100) NOT NULL,
                    stream VARCHAR(50) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    status VARCHAR(50) DEFAULT 'ACTIVE',
                    knec_lan VARCHAR(100) DEFAULT 'N/A',
                    UNIQUE(school_id, admission_number)
                );
                ALTER TABLE students ADD COLUMN IF NOT EXISTS middle_name VARCHAR(100);

                CREATE TABLE IF NOT EXISTS learning_areas (
                    id SERIAL PRIMARY KEY,
                    education_level VARCHAR(100) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    UNIQUE(education_level, name)
                );

                CREATE TABLE IF NOT EXISTS student_scores (
                    id SERIAL PRIMARY KEY,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    cycle_name VARCHAR(50) NOT NULL,
                    raw_score NUMERIC(5, 2) NOT NULL,
                    entered_by_user_id INTEGER REFERENCES users(id),
                    UNIQUE(student_id, learning_area_id, cycle_name)
                );

                -- Some Junior School subjects (English, Kiswahili, Integrated
                -- Science) are assessed as two separate papers, each with its
                -- own "out of" max — e.g. Paper 1 out of 30, Paper 2 out of
                -- 50. This table stores that raw paper-level detail purely
                -- for re-editing/audit purposes; the BLENDED percentage
                -- computed from it is written into student_scores.raw_score
                -- as normal, so report cards, rankings, and every other
                -- existing report keep working completely unchanged — they
                -- never need to know a subject was entered as two papers.
                CREATE TABLE IF NOT EXISTS paper_based_scores (
                    id SERIAL PRIMARY KEY,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    cycle_name VARCHAR(50) NOT NULL,
                    paper1_marks NUMERIC(6, 2),
                    paper1_max NUMERIC(6, 2),
                    paper2_marks NUMERIC(6, 2),
                    paper2_max NUMERIC(6, 2),
                    UNIQUE(student_id, learning_area_id, cycle_name)
                );
            """)

            # Populate Classes
            classes_payload = [
                (1, 'Grade 1', 'Lower Primary'), (2, 'Grade 2', 'Lower Primary'), (3, 'Grade 3', 'Lower Primary'),
                (4, 'Grade 4', 'Upper Primary'), (5, 'Grade 5', 'Upper Primary'), (6, 'Grade 6', 'Upper Primary'),
                (7, 'Grade 7', 'Junior School'), (8, 'Grade 8', 'Junior School'), (9, 'Grade 9', 'Junior School'),
            ]
            for class_id, grade, level in classes_payload:
                cur.execute("INSERT INTO classes (id, grade_name, education_level) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING;", (class_id, grade, level))

            # Populate Subjects
            subjects_payload = [
                ('Junior School', 'Mathematics'), ('Junior School', 'English'), ('Junior School', 'Kiswahili'),
                ('Junior School', 'Creative arts and sports.'), ('Junior School', 'Integrated science.'),
                ('Junior School', 'Agriculture'), ('Junior School', 'Social studies'),
                ('Junior School', 'Christian religious education'), ('Junior School', 'pretechnical studies.'),
                ('Upper Primary', 'Mathematics'), ('Upper Primary', 'English'), ('Upper Primary', 'Kiswahili'),
                ('Upper Primary', 'Creative arts and sports.'), ('Upper Primary', 'Integrated science.'),
                ('Upper Primary', 'Agriculture'), ('Upper Primary', 'Social studies'),
                ('Upper Primary', 'Christian religious education'),
                ('Lower Primary', 'MATHEMATICS'), ('Lower Primary', 'ENGLISH'),
                ('Lower Primary', 'LUGHA'), ('Lower Primary', 'INTEGRATED SCIENCE'),
            ]
            for lvl, name in subjects_payload:
                cur.execute("INSERT INTO learning_areas (education_level, name) VALUES (%s, %s) ON CONFLICT (education_level, name) DO NOTHING;", (lvl, name))

            # One-time cleanup migration: remove the old Lower Primary subject set
            # that was seeded before this change. NOTE: learning_areas.id cascades
            # to student_scores, so this also deletes any scores already recorded
            # against the old Lower Primary subjects.
            cur.execute("""
                DELETE FROM learning_areas
                WHERE education_level = 'Lower Primary'
                AND name NOT IN ('MATHEMATICS', 'ENGLISH', 'LUGHA', 'INTEGRATED SCIENCE');
            """)

            # Indexes on columns hit by frequent WHERE/JOIN clauses. Several
            # tables already get useful leftmost-prefix coverage from their
            # UNIQUE constraints (e.g. students(school_id, admission_number),
            # student_scores(student_id, learning_area_id, cycle_name)) — these
            # add explicit coverage for the access patterns those don't reach.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_students_class_id ON students (class_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_students_school_status ON students (school_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scores_area_cycle ON student_scores (learning_area_id, cycle_name);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_school_role ON users (school_id, role);")

            conn.commit()
            logger.info("Database initialized successfully.")

def bootstrap_super_admin():
    """Creates (or updates the password of) a platform super admin account
    from environment variables. There is deliberately no public signup form
    for this role — set SUPERADMIN_EMAIL and SUPERADMIN_PASSWORD on Render
    to control who can access the super admin portal."""
    email = (os.getenv("SUPERADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("SUPERADMIN_PASSWORD") or ""

    if not email or not password:
        logger.warning(
            "SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD not set — the super admin "
            "portal has no account to log into until these are configured."
        )
        return

    hashed_password = get_password_hash(password[:72])
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE users SET password_hash = %s, role = 'superadmin', is_verified = TRUE WHERE id = %s;",
                    (hashed_password, existing[0]),
                )
            else:
                cur.execute("""
                    INSERT INTO users (email, password_hash, role, school_id, is_verified)
                    VALUES (%s, %s, 'superadmin', NULL, TRUE);
                """, (email, hashed_password))
            conn.commit()
    logger.info(f"Super admin account ready: {email}")

# 3. Call it on startup
bootstrap_database_schema()
bootstrap_super_admin()

# --- Timetabling module (extracted to its own file — see timetable_routes.py) ---
from timetable_routes import router as timetable_router, bootstrap_timetable_schema
bootstrap_timetable_schema()
app.include_router(timetable_router)

# --- Finance module (extracted to its own file — see finance_routes.py) ---
from finance_routes import router as finance_router, bootstrap_finance_schema
bootstrap_finance_schema()
app.include_router(finance_router)

# --- Core Business & CBE Analytics Helper Logic ---
def log_audit_action(cur, request: Request, school_id: int, action: str, details: str = ""):
    """Records an entry in the audit log, using the same cursor/transaction
    as the action it's logging, so they commit together. Never raises — a
    logging failure must never be allowed to block the real action."""
    try:
        user_id = request.cookies.get("session_user_id")
        actor_label = None
        if user_id:
            cur.execute("SELECT full_name, email FROM users WHERE id = %s;", (user_id,))
            row = cur.fetchone()
            if row:
                try:
                    actor_label = row['full_name'] or row['email']
                except (TypeError, KeyError):
                    actor_label = row[0] or row[1]
        cur.execute(
            "INSERT INTO audit_log (school_id, user_id, actor_label, action, details) VALUES (%s, %s, %s, %s, %s);",
            (school_id, user_id, actor_label, action, details)
        )
    except Exception as e:
        logger.warning(f"Audit log entry failed (non-fatal, action was not blocked): {e}")

def evaluate_performance_metrics(score: float) -> dict:
    try:
        val = float(score)
    except (TypeError, ValueError):
        return {"pld": "N/A", "points": 0, "desc": "No Evaluation"}

    # Exclusive upper bounds (except the final 100) so every tier connects
    # directly to the next with zero gaps. The previous version used
    # inclusive bounds on both ends of every tier (e.g. 76<=val<=89 then
    # 90<=val<=100), which left every boundary — 19/20, 29/30, ..., 89/90 —
    # with a gap that swallowed any fractional score landing exactly there
    # (e.g. 89.3), silently misclassifying it as "Out of Range" with 0
    # points. This became much more likely to actually trigger once scores
    # started being computed as weighted averages (naturally fractional)
    # rather than always whole-number exam marks.
    if 0 <= val < 20:
        return {"pld": "BE2", "points": 1, "desc": "Below Expectations"}
    elif 20 <= val < 30:
        return {"pld": "BE1", "points": 2, "desc": "Below Expectations"}
    elif 30 <= val < 40:
        return {"pld": "AE2", "points": 3, "desc": "Approaching Expectations"}
    elif 40 <= val < 50:
        return {"pld": "AE1", "points": 4, "desc": "Approaching Expectations"}
    elif 50 <= val < 60:
        return {"pld": "ME2", "points": 5, "desc": "Meeting Expectations"}
    elif 60 <= val < 76:
        return {"pld": "ME1", "points": 6, "desc": "Meeting Expectations"}
    elif 76 <= val < 90:
        return {"pld": "EE2", "points": 7, "desc": "Exceeding Expectations"}
    elif 90 <= val <= 100:
        return {"pld": "EE1", "points": 8, "desc": "Exceeding Expectations"}
    return {"pld": "N/A", "points": 0, "desc": "Out of Range"}

POINTS_TO_PLD = {8: "EE1", 7: "EE2", 6: "ME1", 5: "ME2", 4: "AE1", 3: "AE2", 2: "BE1", 1: "BE2"}

# These Junior School subjects are assessed as two separate papers (each with
# its own "out of" max), rather than a single combined mark. Matched against
# the lowercased, stripped subject name so it's resilient to minor naming
# differences (e.g. a trailing period).
PAPER_BASED_SUBJECTS = {"english", "kiswahili", "integrated science"}

def is_paper_based_subject(subject_name: str, education_level: str) -> bool:
    """Paper 1 / Paper 2 assessment is a Junior School thing only — English,
    Kiswahili, and Integrated Science also exist at Lower/Upper Primary
    under similar names, so the education_level check is required, not
    just a name match."""
    if (education_level or "").strip() != "Junior School":
        return False
    return (subject_name or "").strip().lower().rstrip(".") in PAPER_BASED_SUBJECTS

def generate_teacher_comment(first_name: str, pld: str) -> str:
    """Class teacher remark, tailored to the learner's overall performance level for the term."""
    comments = {
        "EE1": f"{first_name} has posted an outstanding performance this term, consistently exceeding expectations across most learning areas. Keep up the excellent work!",
        "EE2": f"{first_name} has performed very well this term, exceeding expectations in several learning areas. A commendable effort.",
        "ME1": f"{first_name} has met expectations well this term, showing a solid grasp of most learning areas. Continued consistency will bring even better results.",
        "ME2": f"{first_name} is meeting expectations, with a fair grasp of most learning areas. More consistent practice will help build fluency.",
        "AE1": f"{first_name} is approaching expectations this term. With more focused effort and practice, noticeable improvement is achievable.",
        "AE2": f"{first_name} is approaching expectations but needs closer support in several learning areas to build a stronger foundation.",
        "BE1": f"{first_name} is currently below expectations this term. Close follow-up at home and school is recommended to help {first_name} catch up.",
        "BE2": f"{first_name} needs significant support this term. Please work closely with the class teacher to put a structured improvement plan in place.",
    }
    return comments.get(pld, f"{first_name}'s performance has been recorded for this term; continued effort is encouraged.")

def generate_headteacher_comment(pld: str) -> str:
    """Headteacher endorsement, tailored to the learner's overall performance level for the term."""
    comments = {
        "EE1": "An excellent term's work. Keep raising the bar.",
        "EE2": "A very good term's performance. Well done.",
        "ME1": "A good, steady term's performance. Keep it up.",
        "ME2": "A fair term's performance. Aim higher next term.",
        "AE1": "Effort is visible; more focus is needed to fully meet expectations.",
        "AE2": "More effort and support are needed going forward.",
        "BE1": "Performance requires urgent attention. Let's work together to support this learner.",
        "BE2": "Performance is a serious concern. Immediate intervention is required.",
    }
    return comments.get(pld, "Performance has been noted; let's continue supporting this learner's growth.")



# (SUBJECT_DISPLAY_ORDER, SUBJECT_ABBREVIATIONS, sort_subjects_for_display,
# abbreviate_subject now live in shared.py and are imported above.)

def fetch_theme_styles(color_name: str):
    themes = {
        'emerald': {'bg': 'bg-emerald-800', 'hover': 'hover:bg-emerald-900', 'text': 'text-emerald-700', 'border': 'border-emerald-600', 'hex': '#046A38'},
        'blue': {'bg': 'bg-blue-800', 'hover': 'hover:bg-blue-900', 'text': 'text-blue-700', 'border': 'border-blue-600', 'hex': '#1e40af'},
        'indigo': {'bg': 'bg-indigo-800', 'hover': 'hover:bg-indigo-900', 'text': 'text-indigo-700', 'border': 'border-indigo-600', 'hex': '#3730a3'},
        'purple': {'bg': 'bg-purple-800', 'hover': 'hover:bg-purple-900', 'text': 'text-purple-700', 'border': 'border-purple-600', 'hex': '#6b21a8'}
    }
    return themes.get(color_name, themes['emerald'])

# --- Authentication & Entry Routes ---
@app.get("/", response_class=HTMLResponse)
def landing_root():
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
def login_portal():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Multi-Tenant Hub Gateway</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-900 flex items-center justify-center h-screen font-sans">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md border-t-8 border-emerald-700">
            <img src="{ELIMU_HUB_ICON_DATA_URI}" alt="Elimu Hub" class="w-14 h-14 mx-auto mb-3 rounded-2xl shadow-sm" />
            <h2 class="text-2xl font-black text-center text-slate-800 mb-2">Elimu Hub</h2>
            <p class="text-xs text-center text-slate-400 mb-6">Enterprise Institutional Gateway Node</p>
            
            <form action="/api/v1/auth/login" method="post" class="space-y-4">
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Access Email</label>
                    <input type="email" name="email" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Security Passphrase</label>
                    <input type="password" name="password" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <button type="submit" class="w-full bg-emerald-700 text-white p-3.5 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-lg">Authenticate Instance</button>
            </form>

            <div class="mt-3 text-center">
                <a href="/forgot-password" class="text-xs text-slate-400 hover:text-slate-600 hover:underline">Forgot your password?</a>
            </div>
            
            <div class="mt-6 border-t pt-4 text-center">
                <p class="text-xs text-slate-500">
                    New Institution? 
                    <a href="/register" class="text-emerald-700 font-bold hover:underline ml-1">Register Self-Service Account Node</a>
                </p>
            </div>
        </div>
    </body>
    </html>
    """

@app.post("/api/v1/auth/login")
def process_login(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    safe_password = password[:72]

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if is_login_rate_limited(cur, email):
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many failed login attempts for this account. Please wait {LOGIN_RATE_LIMIT_WINDOW_MINUTES} minutes and try again."
                )

            # Querying using the correct column 'email'
            cur.execute("SELECT * FROM users WHERE email = %s;", (email,))
            user = cur.fetchone()
            
            if user:
                is_valid = False
                try:
                    is_valid = verify_password(safe_password, user['password_hash'])
                except Exception:
                    is_valid = False
                
                # Logic for password migration
                if not is_valid and user['password_hash'] == password:
                    hashed_password = get_password_hash(safe_password)
                    cur.execute("""
                        UPDATE users 
                        SET password_hash = %s 
                        WHERE id = %s;
                    """, (hashed_password, user['id']))
                    conn.commit()
                    is_valid = True
                
                if is_valid:
                    clear_failed_logins(cur, email)
                    conn.commit()

                    if user['role'] == 'superadmin':
                        response = RedirectResponse(url="/superadmin/dashboard", status_code=303)
                    else:
                        # Admin and staff both belong to a school — check that
                        # school hasn't been paused or is still awaiting approval.
                        cur.execute("SELECT status FROM schools WHERE id = %s;", (user['school_id'],))
                        school_row = cur.fetchone()
                        school_status = school_row['status'] if school_row else 'active'

                        if school_status == 'pending':
                            raise HTTPException(
                                status_code=403,
                                detail="Your school's registration is still awaiting approval from the platform administrator. You'll be able to log in once it's approved."
                            )
                        if school_status == 'deactivated':
                            raise HTTPException(
                                status_code=403,
                                detail="This school's account has been deactivated. Please contact the platform administrator."
                            )

                        if user['role'] == 'admin':
                            response = RedirectResponse(url=f"/admin/dashboard/{user['school_id']}", status_code=303)
                        else:
                            if not user['is_verified']:
                                raise HTTPException(status_code=403, detail="Access Denied: Staff verification pending admin approval.")
                            response = RedirectResponse(url=f"/staff/dashboard/{user['school_id']}?user_id={user['id']}", status_code=303)
                    
                    response.set_cookie(
                        key="session_school_id",
                        value=str(user['school_id']) if user['school_id'] is not None else "0",
                        httponly=True,
                        samesite="lax",
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        max_age=60 * 60 * 24 * 7,
                    )
                    response.set_cookie(
                        key="session_role",
                        value=str(user['role']),
                        httponly=True,
                        samesite="lax",
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        max_age=60 * 60 * 24 * 7,
                    )
                    response.set_cookie(
                        key="session_user_id",
                        value=str(user['id']),
                        httponly=True,
                        samesite="lax",
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        max_age=60 * 60 * 24 * 7,
                    )
                    return response

            # Either the user wasn't found, or the password was wrong — record
            # a failed attempt against this email either way (this also
            # naturally rate-limits repeated guesses against unknown emails).
            record_failed_login(cur, email)
            conn.commit()
    
    # If no user found or password invalid
    raise HTTPException(status_code=401, detail="Invalid credentials")
                    
    raise HTTPException(status_code=401, detail="Invalid credential combination provided.")

REGISTRATION_BG_IMAGE_B64 = "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"

@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(sent: str = None):
    notice_html = ""
    if sent == "1":
        notice_html = """
        <div class="bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-3 rounded-lg mb-4">
            If that email is on file with a phone number, a reset code has been sent via SMS. Enter it on the next screen.
        </div>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Forgot Password</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-900 flex items-center justify-center h-screen font-sans">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md border-t-8 border-emerald-700">
            <h2 class="text-xl font-black text-slate-800 mb-1">Forgot Password</h2>
            <p class="text-xs text-slate-400 mb-6">Enter the email on your account. We'll text a reset code to the phone number on file.</p>
            {notice_html}
            <form action="/api/v1/auth/forgot-password" method="post" class="space-y-4">
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Account Email</label>
                    <input type="email" name="email" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <button type="submit" class="w-full bg-emerald-700 text-white p-3.5 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-lg">Send Reset Code</button>
            </form>
            <div class="mt-4 text-center">
                <a href="/login" class="text-xs text-slate-400 hover:text-slate-600 hover:underline">← Back to login</a>
            </div>
        </div>
    </body>
    </html>
    """

@app.post("/api/v1/auth/forgot-password")
def forgot_password_submit(email: str = Form(...)):
    email = email.strip().lower()

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, phone_number FROM users WHERE email = %s;", (email,))
            user = cur.fetchone()

            if user and user.get('phone_number'):
                # Invalidate any previous unused codes for this account first.
                cur.execute("DELETE FROM password_resets WHERE user_id = %s AND used = FALSE;", (user['id'],))

                reset_code = f"{random.randint(0, 999999):06d}"
                expires_at = datetime.utcnow() + timedelta(minutes=15)
                cur.execute("""
                    INSERT INTO password_resets (user_id, reset_code, expires_at)
                    VALUES (%s, %s, %s);
                """, (user['id'], reset_code, expires_at))
                conn.commit()

                send_sms(
                    user['phone_number'],
                    f"Your Elimu Hub password reset code is {reset_code}. It expires in 15 minutes. "
                    f"If you didn't request this, ignore this message."
                )

    # Always the same response, regardless of whether the email was found —
    # this avoids revealing which emails have accounts on the system.
    return RedirectResponse(url=f"/reset-password?email={urllib.parse.quote(email)}&sent=1", status_code=303)

@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(email: str = "", sent: str = None):
    notice_html = ""
    if sent == "1":
        notice_html = """
        <div class="bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-3 rounded-lg mb-4">
            If that email is on file with a phone number, a reset code has been sent via SMS.
        </div>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Reset Password</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-900 flex items-center justify-center h-screen font-sans">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md border-t-8 border-emerald-700">
            <h2 class="text-xl font-black text-slate-800 mb-1">Reset Password</h2>
            <p class="text-xs text-slate-400 mb-6">Enter the 6-digit code we texted you, along with your new password.</p>
            {notice_html}
            <form action="/api/v1/auth/reset-password" method="post" class="space-y-4">
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Account Email</label>
                    <input type="email" name="email" value="{esc(email)}" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">6-Digit Reset Code</label>
                    <input type="text" name="reset_code" maxlength="6" pattern="[0-9]{{6}}" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none tracking-widest text-center font-mono text-lg" required>
                </div>
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">New Password</label>
                    <input type="password" name="new_password" minlength="6" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <button type="submit" class="w-full bg-emerald-700 text-white p-3.5 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-lg">Reset Password</button>
            </form>
            <div class="mt-4 text-center">
                <a href="/forgot-password" class="text-xs text-slate-400 hover:text-slate-600 hover:underline">Didn't get a code? Request again</a>
            </div>
        </div>
    </body>
    </html>
    """

@app.post("/api/v1/auth/reset-password")
def reset_password_submit(email: str = Form(...), reset_code: str = Form(...), new_password: str = Form(...)):
    email = email.strip().lower()
    reset_code = reset_code.strip()

    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=400, detail="Invalid email or reset code.")

            cur.execute("""
                SELECT id FROM password_resets
                WHERE user_id = %s AND reset_code = %s AND used = FALSE AND expires_at > NOW()
                ORDER BY id DESC LIMIT 1;
            """, (user['id'], reset_code))
            reset_row = cur.fetchone()
            if not reset_row:
                raise HTTPException(status_code=400, detail="That reset code is invalid or has expired. Please request a new one.")

            hashed_password = get_password_hash(new_password[:72])
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s;", (hashed_password, user['id']))
            cur.execute("UPDATE password_resets SET used = TRUE WHERE id = %s;", (reset_row['id'],))
            conn.commit()

    return HTMLResponse("""
    <script>
        alert('Password reset successfully! Please log in with your new password.');
        window.location.href='/login';
    </script>
    """)

@app.get("/terms", response_class=HTMLResponse)
def terms_and_conditions_page():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Terms and Conditions</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-100 min-h-screen py-10 px-6">
        <div class="max-w-3xl mx-auto bg-white rounded-2xl border shadow-xs p-8 space-y-5 text-sm text-slate-700 leading-relaxed">
            <h1 class="text-2xl font-black text-slate-900">Terms and Conditions</h1>
            <p class="text-xs text-slate-400">Last updated: 2026</p>

            <p class="text-xs bg-amber-50 border border-amber-200 text-amber-800 rounded-lg p-3">
                <b>Note to the school operator:</b> this is a starting template, not a substitute for legal advice.
                Please have it reviewed by a lawyer familiar with Kenyan data protection law (the Data Protection Act, 2019)
                before relying on it, especially given that this system stores learners' personal and academic records.
            </p>

            <div>
                <h2 class="font-bold text-slate-900 mb-1">1. Acceptance of Terms</h2>
                <p>By registering a school on this platform, you confirm that you are authorized to act on behalf of your institution and agree to be bound by these Terms and Conditions.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">2. Account Registration and Approval</h2>
                <p>New school accounts are reviewed before activation. Access to the dashboard, student records, and reporting tools is granted only once your school's registration has been approved by the platform administrator.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">3. Data You Provide</h2>
                <p>Your school is responsible for the accuracy of student, staff, and academic data entered into the system. You confirm that you have the necessary consent from parents/guardians and staff to store and process this data for the purpose of academic reporting.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">4. Use of Student Images and Data</h2>
                <p>Where your school uploads a logo, photos, or other identifying content, you confirm that you hold appropriate rights and consent to use that content within the system.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">5. Account Security</h2>
                <p>You are responsible for keeping your administrator and staff login credentials confidential. Notify the platform administrator immediately if you suspect unauthorized access to your account.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">6. Service Availability</h2>
                <p>The platform is provided on an "as available" basis. While reasonable efforts are made to keep the service running and data backed up, no guarantee of uninterrupted availability is made.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">7. Suspension and Termination</h2>
                <p>The platform administrator may suspend or deactivate a school's account for violations of these terms, non-payment, misuse of the platform, or at their discretion with reasonable notice where practical.</p>
            </div>
            <div>
                <h2 class="font-bold text-slate-900 mb-1">8. Changes to These Terms</h2>
                <p>These terms may be updated from time to time. Continued use of the platform after changes are posted constitutes acceptance of the revised terms.</p>
            </div>

            <div class="pt-4 border-t">
                <a href="/register" class="text-emerald-700 font-bold hover:underline text-xs">← Back to registration</a>
            </div>
        </div>
    </body>
    </html>
    """

@app.get("/register", response_class=HTMLResponse)
def public_registration_portal():
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Create School Tenant Account</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="flex items-center justify-center min-h-screen font-sans p-6 bg-slate-900 bg-cover bg-center" style="background-image: linear-gradient(rgba(15,23,42,0.80), rgba(15,23,42,0.88)), url('data:image/jpeg;base64,{REGISTRATION_BG_IMAGE_B64}');">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-xl border-t-8 border-emerald-700">
            <h2 class="text-2xl font-black text-slate-800">Register Institutional Tenant</h2>
            <p class="text-xs text-slate-400 mb-6">Setup your completely isolated enterprise report engine node instance.</p>
            
            <form action="/api/v1/tenant/register" method="post" enctype="multipart/form-data" class="space-y-4 text-xs">
                <div class="bg-slate-50 p-4 rounded-xl border space-y-3">
                    <h3 class="font-black text-slate-700 uppercase tracking-wide">🏫 School Profile Information</h3>
                    <div>
                        <label class="block font-bold text-slate-600">Official School Name</label>
                        <input type="text" name="school_name" placeholder="e.g. Kilimani Academy" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block font-bold text-slate-600">Sub-County Jurisdiction</label>
                            <input type="text" name="sub_county" placeholder="e.g. Dagoretti" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                        </div>
                        <div>
                            <label class="block font-bold text-slate-600">Physical Location Address</label>
                            <input type="text" name="physical_address" placeholder="e.g. Yaya Centre, Nairobi" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                        </div>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-600">Official School Logo Image File</label>
                        <input type="file" name="logo_file" class="w-full p-2 border rounded-lg mt-1 bg-white" accept="image/*">
                    </div>
                </div>

                <div class="bg-slate-50 p-4 rounded-xl border space-y-3">
                    <h3 class="font-black text-slate-700 uppercase tracking-wide">🔒 Super-Admin Account Security Credentials</h3>
                    <div>
                        <label class="block font-bold text-slate-600">Administrator Full Name</label>
                        <input type="text" name="admin_full_name" placeholder="e.g. Francis Mwangi" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-600">Primary Administrator Username (Email Address)</label>
                        <input type="email" name="admin_email" placeholder="admin@school.ac.ke" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-600">Secure Access Passphrase Password</label>
                        <input type="password" name="admin_password" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-600">Administrator Phone Number</label>
                        <input type="tel" name="admin_phone_number" placeholder="07XXXXXXXX" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                        <p class="text-[10px] text-slate-400 mt-1">Used only for password-reset codes via SMS.</p>
                    </div>
                </div>

                <div class="bg-white p-4 rounded-xl border">
                    <label class="flex items-start gap-2.5 cursor-pointer">
                        <input type="checkbox" name="accept_terms" value="1" class="mt-0.5 w-4 h-4 text-emerald-600 border-slate-300 rounded focus:ring-emerald-500 cursor-pointer" required>
                        <span class="text-xs text-slate-600">
                            I have read and agree to the
                            <a href="/terms" target="_blank" class="text-emerald-700 font-bold hover:underline">Terms and Conditions</a>
                            on behalf of this institution.
                        </span>
                    </label>
                </div>

                <div class="flex items-center justify-between pt-2">
                    <a href="/login" class="text-slate-500 font-bold hover:underline">Already have an institution? Log in</a>
                    <button type="submit" class="bg-emerald-700 text-white px-6 py-3 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-md">Create Account & Boot Engine</button>
                </div>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/api/v1/tenant/register")
async def register_new_tenant_pipeline(
    school_name: str = Form(...),
    sub_county: str = Form(...),
    physical_address: str = Form(...),
    admin_full_name: str = Form(...),
    admin_email: str = Form(...),
    admin_password: str = Form(...),
    admin_phone_number: str = Form(...),
    accept_terms: str = Form(None),
    logo_file: UploadFile = File(None)
):
    school_name = school_name.strip()
    sub_county = sub_county.strip()
    physical_address = physical_address.strip()
    admin_full_name = admin_full_name.strip()
    admin_phone_number = admin_phone_number.strip()
    admin_email = admin_email.strip().lower()

    if not school_name or not sub_county or not physical_address:
        raise HTTPException(status_code=400, detail="School name, sub-county, and address are all required.")
    if not admin_full_name:
        raise HTTPException(status_code=400, detail="Administrator full name is required.")
    if len(admin_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if not accept_terms:
        raise HTTPException(status_code=400, detail="You must agree to the Terms and Conditions to register.")

    logo_resolved_url = None
    if logo_file and logo_file.filename:
        file_extension = os.path.splitext(logo_file.filename)[1].lower()
        if file_extension not in ALLOWED_LOGO_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Unsupported logo file type. Use PNG, JPG, GIF, or WEBP.")

        contents = await logo_file.read()
        if len(contents) > MAX_LOGO_SIZE_BYTES:
            raise HTTPException(status_code=400, detail="Logo file is too large (5MB max).")

        # Create a URL-safe structured unique filename node
        safe_filename = f"logo_{uuid.uuid4().hex}{file_extension}"

        # 1. Primary Cloud Architecture Path: Process via Supabase Client Gate
        if supabase_client:
            try:
                # Expects a storage bucket explicitly configured to PUBLIC access named 'logos'
                supabase_client.storage.from_("logos").upload(
                    path=safe_filename,
                    file=contents,
                    file_options={"content-type": logo_file.content_type}
                )
                # Capture absolute public URL reference network asset string
                logo_resolved_url = supabase_client.storage.from_("logos").get_public_url(safe_filename)
            except Exception as storage_err:
                global _last_storage_error
                _last_storage_error = f"{type(storage_err).__name__}: {storage_err}"
                logger.error(f"Supabase Cloud upload failed, reverting locally: {storage_err}")
        if not logo_resolved_url:
            local_path = f"{UPLOAD_DIR}/{safe_filename}"
            try:
                with open(local_path, "wb") as f:
                    f.write(contents)
                logo_resolved_url = f"/{local_path}"
            except OSError as io_err:
                logger.error(f"Failed to save uploaded logo locally: {io_err}")
                raise HTTPException(status_code=500, detail="Could not save the uploaded logo. Please try again.")

    safe_password = admin_password[:72]
    hashed_password = get_password_hash(safe_password)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email = %s;", (admin_email,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Registration Refused: Email already allocated.")
            
            cur.execute("""
                INSERT INTO schools (name, sub_county, physical_address, logo_url, wallet_balance, theme_color, status, terms_accepted_at)
                VALUES (%s, %s, %s, %s, 0.00, 'emerald', 'pending', NOW()) RETURNING id;
            """, (school_name, sub_county, physical_address, logo_resolved_url))
            new_school_id = cur.fetchone()['id']

            cur.execute("""
                INSERT INTO school_settings (school_id, active_year, active_term, active_cycle, closing_date, opening_date)
                VALUES (%s, 2026, 'Term 1', 'End Term', '2026-04-10', '2026-05-04');
            """, (new_school_id,))

            cur.execute("""
                INSERT INTO users (email, password_hash, role, school_id, is_verified, phone_number, full_name)
                VALUES (%s, %s, 'admin', %s, TRUE, %s, %s);
            """, (admin_email, hashed_password, new_school_id, admin_phone_number, admin_full_name))

            conn.commit()

    return HTMLResponse("""
    <script>
        alert('Institutional Registration Complete! Dynamic Tenant Configuration Created Successfully.');
        window.location.href='/login';
    </script>
    """)


@app.get("/admin/school/update-logo/{school_id}", response_class=HTMLResponse)
def update_school_logo_form(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

    logo_src = school.get('logo_url')
    current_logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        current_logo_html = f"""
        <div class="mb-4">
            <p class="text-xs font-bold text-slate-600 mb-2">Current Logo</p>
            <img src='{final_src}' class="w-24 h-24 object-contain border rounded-xl p-1 bg-slate-50" />
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Update School Logo</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-900 flex items-center justify-center min-h-screen font-sans p-6">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md border-t-8 border-emerald-700">
            <h2 class="text-xl font-black text-slate-800 mb-1">Update School Logo</h2>
            <p class="text-xs text-slate-400 mb-6">{esc(school['name'])}</p>

            {current_logo_html}

            <form action="/api/v1/school/update-logo/{school_id}" method="post" enctype="multipart/form-data" class="space-y-4">
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">New Logo Image File</label>
                    <input type="file" name="logo_file" accept="image/*" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                </div>
                <div class="flex items-center justify-between pt-2">
                    <a href="/admin/dashboard/{school_id}" class="text-slate-500 font-bold hover:underline text-xs">← Back to Dashboard</a>
                    <button type="submit" class="bg-emerald-700 text-white px-6 py-3 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-md text-xs">Upload Logo</button>
                </div>
            </form>
            <a href="/admin/system/diagnostics/{school_id}" class="block text-center text-[10px] text-slate-400 hover:text-slate-600 hover:underline mt-4">Logo not saving? Check storage diagnostics →</a>
        </div>
    </body>
    </html>
    """


@app.post("/api/v1/school/update-logo/{school_id}")
async def update_school_logo_submit(school_id: int, request: Request, logo_file: UploadFile = File(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    if not logo_file or not logo_file.filename:
        raise HTTPException(status_code=400, detail="A logo image file is required.")

    file_extension = os.path.splitext(logo_file.filename)[1].lower()
    if file_extension not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported logo file type. Use PNG, JPG, GIF, or WEBP.")

    contents = await logo_file.read()
    if len(contents) > MAX_LOGO_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Logo file is too large (5MB max).")

    safe_filename = f"logo_{uuid.uuid4().hex}{file_extension}"
    logo_resolved_url = None

    # 1. Primary Cloud Architecture Path: Process via Supabase Client Gate
    if supabase_client:
        try:
            supabase_client.storage.from_("logos").upload(
                path=safe_filename,
                file=contents,
                file_options={"content-type": logo_file.content_type}
            )
            logo_resolved_url = supabase_client.storage.from_("logos").get_public_url(safe_filename)
        except Exception as storage_err:
            global _last_storage_error
            _last_storage_error = f"{type(storage_err).__name__}: {storage_err}"
            logger.error(f"Supabase Cloud upload failed, reverting locally: {storage_err}")

    # 2. Fallback Pipeline Path: Write asset block to local server disk
    if not logo_resolved_url:
        local_path = f"{UPLOAD_DIR}/{safe_filename}"
        try:
            with open(local_path, "wb") as f:
                f.write(contents)
            logo_resolved_url = f"/{local_path}"
        except OSError as io_err:
            logger.error(f"Failed to save uploaded logo locally: {io_err}")
            raise HTTPException(status_code=500, detail="Could not save the uploaded logo. Please try again.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE schools SET logo_url = %s WHERE id = %s;", (logo_resolved_url, school_id))
            conn.commit()

    storage_flag = "cloud" if logo_resolved_url.startswith("http") else "local"
    return RedirectResponse(url=f"/admin/dashboard/{school_id}?logo_storage={storage_flag}", status_code=303)


@app.get("/admin/dashboard/{school_id}", response_class=HTMLResponse)
def administrative_dashboard(school_id: int, request: Request, logo_storage: str = None, student_added: str = None, staff_added: str = None):  
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    # Use the connection context manager directly
    with get_db_connection() as conn:
    # Use RealDictCursor to get dictionary-like rows
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            admin_user_id = request.cookies.get("session_user_id")
            admin_name = None
            if admin_user_id:
                cur.execute("SELECT full_name FROM users WHERE id = %s AND school_id = %s AND role = 'admin';", (admin_user_id, school_id))
                admin_row = cur.fetchone()
                if admin_row:
                    admin_name = admin_row['full_name']
        
            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            
            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.middle_name, s.last_name, s.stream, 
                       c.grade_name, c.education_level
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC, s.admission_number ASC;
            """, (school_id,))
            students = cur.fetchall()
            
            cur.execute("SELECT id, email, is_verified, full_name, tsc_number, phone_number FROM users WHERE school_id = %s AND role='staff' ORDER BY full_name NULLS LAST, email ASC;", (school_id,))
            staff_members = cur.fetchall()
            
            cur.execute("""
                SELECT DISTINCT c.id, c.grade_name, s.stream, c.education_level
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC;
            """, (school_id,))
            classes = cur.fetchall()

            cur.execute("SELECT COUNT(*) AS cnt FROM timetable_periods WHERE school_id = %s;", (school_id,))
            has_timetable_periods = cur.fetchone()['cnt'] > 0

            cur.execute("SELECT message FROM platform_announcements WHERE is_active = TRUE ORDER BY created_at DESC LIMIT 3;")
            active_announcements = cur.fetchall()

            # School-wide average score per exam cycle, for the trend chart —
            # one simple aggregate query, no per-student loop.
            cur.execute("""
                SELECT sc.cycle_name, AVG(sc.raw_score) AS avg_score, COUNT(DISTINCT sc.student_id) AS student_count
                FROM student_scores sc
                JOIN students s ON sc.student_id = s.id
                WHERE s.school_id = %s
                GROUP BY sc.cycle_name;
            """, (school_id,))
            trend_rows = {r['cycle_name']: r for r in cur.fetchall()}

    if not school:
        raise HTTPException(status_code=404, detail="Institution Tenant Context Missed.")
    
    st = settings or {
        'active_term': 'Term 1', 
        'active_cycle': 'End Term', 
        'opening_date': '', 
        'closing_date': '', 
        'is_single_stream': False
    }
    is_single_stream = st.get('is_single_stream', False)

    # Group students by class+stream for stat cards and the sidebar roster panel
    grouped_students = {}
    class_group_order = []
    for s in students:
        disp_stream = "Single Stream" if (not s['stream'] or s['stream'].upper() == "SINGLE STREAM") else s['stream']
        key = (s['grade_name'], disp_stream, s['education_level'], s['stream'])
        if key not in grouped_students:
            grouped_students[key] = []
            class_group_order.append(key)
        grouped_students[key].append(s)

    total_students = len(students)
    total_grades = len({c['grade_name'] for c in classes})
    total_sections = len(classes)
    total_levels = len({c['education_level'] for c in classes})
    total_staff = len(staff_members)
    active_staff = len([m for m in staff_members if m['is_verified']])

    checklist_items = [
        (bool(classes), "Register your first student", f"/admin/student/new/{school_id}"),
        (bool(staff_members), "Add a staff account", f"/staff/register-panel/{school_id}"),
        (bool(school.get('logo_url')), "Upload your school logo", f"/admin/school/update-logo/{school_id}"),
        (has_timetable_periods, "Set up your timetable periods & bell times", f"/timetable/periods/{school_id}"),
    ]
    incomplete_items = [item for item in checklist_items if not item[0]]
    onboarding_html = ""
    if incomplete_items:
        checklist_rows = "".join(
            f"""<a href="{url}" class="flex items-center gap-3 py-2 hover:bg-indigo-50/60 rounded-lg px-2 -mx-2 transition">
                <span class="w-5 h-5 rounded-full border-2 border-indigo-300 shrink-0"></span>
                <span class="text-sm font-semibold text-slate-700">{label}</span>
                <span class="ml-auto text-indigo-600 text-xs font-bold">Set up →</span>
            </a>"""
            for _, label, url in incomplete_items
        )
        onboarding_html = f"""
        <div class="bg-white rounded-2xl border border-indigo-100 shadow-xs p-5">
            <h2 class="text-xs font-bold uppercase tracking-wider text-indigo-700 mb-1">🚀 Getting Started</h2>
            <p class="text-xs text-slate-400 mb-3">A few things left to finish setting up your school.</p>
            {checklist_rows}
        </div>
        """

    announcement_banners_html = "".join(
        f"""<div class="bg-gradient-to-r from-indigo-600 to-violet-600 text-white rounded-2xl px-5 py-3 flex items-center gap-3 shadow-xs">
            <span class="text-lg">📣</span>
            <p class="text-sm font-semibold">{esc(a['message'])}</p>
        </div>"""
        for a in active_announcements
    )

    trend_chart_html = ""
    cycles_with_data = [c for c in ["Opener", "Midterm", "End Term"] if c in trend_rows]
    if cycles_with_data:
        bar_width = 100
        gap = 40
        chart_bars = ""
        for i, cycle in enumerate(cycles_with_data):
            avg = float(trend_rows[cycle]['avg_score'])
            bar_height = max(4, (avg / 100) * 140)
            x = 20 + i * (bar_width + gap)
            chart_bars += f"""
            <rect x="{x}" y="{160 - bar_height}" width="{bar_width}" height="{bar_height}" rx="6" fill="url(#trendGrad)" />
            <text x="{x + bar_width/2}" y="{160 - bar_height - 8}" text-anchor="middle" font-size="13" font-weight="bold" fill="#1e293b" font-family="'Plus Jakarta Sans',sans-serif">{avg:.1f}%</text>
            <text x="{x + bar_width/2}" y="180" text-anchor="middle" font-size="11" fill="#64748b" font-family="'Plus Jakarta Sans',sans-serif">{cycle}</text>
            """
        chart_width = 20 + len(cycles_with_data) * (bar_width + gap)
        trend_chart_html = f"""
        <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5">
            <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">📈 Performance Trend This Term</h2>
            <svg viewBox="0 0 {chart_width} 195" style="width:100%; max-width:480px; height:auto;">
                <defs>
                    <linearGradient id="trendGrad" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stop-color="#4f46e5"/>
                        <stop offset="100%" stop-color="#0d9488"/>
                    </linearGradient>
                </defs>
                <line x1="10" y1="160" x2="{chart_width - 10}" y2="160" stroke="#e2e8f0" stroke-width="1"/>
                {chart_bars}
            </svg>
        </div>
        """

    def _stat_card(label, value, accent_hex, sub=None):
        sub_html = f"<p class='text-[10px] text-slate-400 mt-0.5'>{sub}</p>" if sub else ""
        return f"""
        <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:{accent_hex};">
            <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">{label}</p>
            <p class="text-2xl font-black text-slate-900 mt-1">{value}</p>
            {sub_html}
        </div>
        """

    stats_html = f"""
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
        {_stat_card("Total Students", total_students, "#4f46e5")}
        {_stat_card("Grade Cohorts", total_grades, "#7c3aed")}
        {_stat_card("Class Sections", total_sections, "#0d9488")}
        {_stat_card("Education Segments", total_levels, "#d97706")}
        {_stat_card("Staff", f"{active_staff}/{total_staff}", "#e11d48", sub="active / total")}
    </div>
    """

    # Sidebar: collapsible, printable per-class roster panels
    roster_sections = []
    for (grade_name, disp_stream, education_level, raw_stream) in class_group_order:
        group_students = grouped_students[(grade_name, disp_stream, education_level, raw_stream)]
        is_stream_blank = not raw_stream or raw_stream.strip() == "" or raw_stream.upper() == "SINGLE STREAM"
        stream_param = "SINGLE STREAM" if is_stream_blank else raw_stream

        encoded_grade = urllib.parse.quote(grade_name)
        encoded_stream = urllib.parse.quote(stream_param)
        encoded_level = urllib.parse.quote(education_level)

        title_label = grade_name if is_stream_blank else f"{grade_name} — {esc(disp_stream)}"

        rows_html = "".join(f"""
            <li class='flex justify-between items-center gap-2 py-1.5 border-b border-slate-50 last:border-0'>
                <span class='text-slate-700 truncate'>{esc(st['first_name'])} {esc(st['middle_name']) + ' ' if st.get('middle_name') else ''}{esc(st['last_name'])}
                    <span class='text-slate-400 font-mono text-[10px] block'>#{esc(st['admission_number'])}</span>
                </span>
                <span class='flex items-center gap-2 shrink-0'>
                    <a href='/admin/student/edit/{school_id}/{st['id']}' class='text-slate-500 hover:text-slate-800 text-[10px] font-bold'>Edit</a>
                    <a href='/admin/scores/manage/{school_id}?student_id={st['id']}' class='text-blue-600 hover:text-blue-800 text-[10px] font-bold'>Scores →</a>
                </span>
            </li>
        """ for st in group_students)

        roster_sections.append(f"""
        <details class='border border-slate-100 rounded-xl overflow-hidden'>
            <summary class='cursor-pointer list-none flex items-center justify-between px-3 py-2.5 bg-slate-50 hover:bg-slate-100 transition'>
                <span class='text-xs font-bold text-slate-700'>{title_label}</span>
                <span class='flex items-center gap-2'>
                    <span class='text-[10px] bg-white border border-slate-200 px-1.5 py-0.5 rounded-full font-bold text-slate-500'>{len(group_students)}</span>
                    <a href='/admin/students/roster/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' onclick='event.stopPropagation()' class='text-[10px] text-emerald-700 font-bold hover:underline'>🖨 Print</a>
                </span>
            </summary>
            <ul class='px-3 py-2 text-xs max-h-56 overflow-y-auto'>
                {rows_html or "<li class='text-slate-400 italic py-2'>No students</li>"}
            </ul>
        </details>
        """)
    roster_sidebar_html = "".join(roster_sections)

    # Staff panel: list with activate/deactivate/delete controls
    staff_rows = []
    for m in staff_members:
        is_active = m['is_verified']
        status_badge = (
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>Active</span>"
            if is_active else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Pending</span>"
        )
        toggle_label = "Deactivate" if is_active else "Activate"
        toggle_classes = "text-amber-700 hover:text-amber-900" if is_active else "text-emerald-700 hover:text-emerald-900"
        display_name = m.get('full_name') or "(Name not set — added before this feature)"
        tsc_display = m.get('tsc_number') or "—"
        phone_display = m.get('phone_number') or "—"
        staff_rows.append(f"""
            <div class='py-2.5 border-b border-slate-50 last:border-0'>
                <div class="flex items-center justify-between gap-2">
                    <p class="text-xs font-bold text-slate-800 truncate">{esc(display_name)}</p>
                    {status_badge}
                </div>
                <p class="text-[10px] text-slate-500 mt-0.5">TSC No: <span class="font-semibold text-slate-600">{esc(tsc_display)}</span> • {esc(phone_display)}</p>
                <p class="text-[10px] text-slate-400 truncate">{esc(m['email'])}</p>
                <div class="flex items-center gap-3 shrink-0 text-[10px] font-bold mt-1.5">
                    <form action="/api/v1/staff/toggle-status/{m['id']}/{school_id}" method="post">
                        <button type="submit" class="{toggle_classes}">{toggle_label}</button>
                    </form>
                    <form action="/api/v1/staff/delete/{m['id']}/{school_id}" method="post" onsubmit="return confirm('Remove {esc(display_name)} permanently? This cannot be undone.');">
                        <button type="submit" class="text-rose-600 hover:text-rose-800">Delete</button>
                    </form>
                </div>
            </div>
        """)
    staff_panel_html = "".join(staff_rows)

    # Dynamic Class Cards Generation
    class_blocks = []
    for c in classes:
        is_stream_blank = not c['stream'] or c['stream'].strip() == "" or c['stream'].upper() == "SINGLE STREAM"
        
        if is_stream_blank:
            display_title = c['grade_name']
            stream_param = "SINGLE STREAM"
        else:
            display_title = f"{c['grade_name']} — Stream: {esc(c['stream'])}"
            stream_param = c['stream']
        
        encoded_grade = urllib.parse.quote(c['grade_name'])
        encoded_stream = urllib.parse.quote(stream_param)
        encoded_level = urllib.parse.quote(c['education_level'])
        
        class_blocks.append(f"""
            <div class='bg-white border border-slate-200/80 p-5 rounded-2xl shadow-xs hover:shadow-md transition-all flex flex-col justify-between group'>
                <div>
                    <span class='text-[10px] bg-slate-100 text-slate-600 px-2.5 py-1 rounded-md font-bold uppercase tracking-wider'>{c['education_level']}</span>
                    <h3 class='text-base font-black text-slate-800 mt-2.5 group-hover:text-slate-900'>{display_title}</h3>
                </div>
                <div class='grid grid-cols-2 sm:grid-cols-3 gap-2 mt-5'>
                    <a href='/staff/bulk-entry/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' class='bg-indigo-900 hover:bg-indigo-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Bulk Entry</a>
                    <a href='/admin/students/roster/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-slate-700 hover:bg-slate-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Class List</a>
                    <a href='/api/v1/reports/bulk-print/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-emerald-600 text-white text-center text-xs py-2 rounded-xl font-semibold hover:bg-emerald-700 transition shadow-xs'>Bulk Print</a>
                    <a href='/admin/reports/merit-list/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}' target='_blank' class='bg-violet-700 hover:bg-violet-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Merit List</a>
                    <a href='/admin/reports/subject-analysis/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-amber-600 hover:bg-amber-700 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Subj. Analysis</a>
                    <a href='/admin/reports/top10/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-rose-600 hover:bg-rose-700 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Top 10</a>
                    <a href='/admin/reports/top-subject/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-cyan-700 hover:bg-cyan-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Top/Subject</a>
                </div>
            </div>
        """)
    class_blocks_html = "".join(class_blocks)

    # Robust Logo configuration injection
    logo_html = ""
    logo_src = school.get('logo_url')
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"""
        <a href='/admin/school/update-logo/{school_id}' class='w-11 h-11 rounded-xl bg-slate-50 border border-slate-200 flex items-center justify-center p-1.5 shadow-2xs hover:border-emerald-400 transition' title='Update school logo'>
            <img src='{final_src}' class='max-w-full max-h-full object-contain' />
        </a>
        """
    else:
        logo_html = f"""
        <a href='/admin/school/update-logo/{school_id}' class='w-11 h-11 rounded-xl bg-slate-50 border border-dashed border-slate-300 flex items-center justify-center text-slate-400 hover:border-emerald-400 hover:text-emerald-600 transition text-[9px] font-bold text-center leading-tight' title='Add school logo'>
            ADD<br/>LOGO
        </a>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html class="h-full">
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Control Deck - {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F7F8FB] text-slate-800 antialiased min-h-full flex flex-col relative">
        {TOAST_CONTAINER_HTML}

        <header class="bg-white border-b border-slate-200/80 px-4 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3 sticky top-0 z-40 backdrop-blur-md bg-white/90 shadow-2xs">
            <div class="flex items-center space-x-4">
                {logo_html}
                <div>
                    <h1 class="text-base font-bold text-slate-900 tracking-tight">{esc(school['name'])}</h1>
                    <p class="text-xs text-slate-500">{esc(school['physical_address'])} • {esc(school['sub_county'])} Sub-County</p>
                    {f'<p class="text-[11px] text-indigo-700 font-bold mt-0.5">Welcome, {esc(admin_name.split(" ")[0])}</p>' if admin_name else ''}
                </div>
            </div>
            <div class="flex items-center flex-wrap gap-2 text-xs font-semibold">
                <span class="bg-gradient-to-r from-amber-500 to-amber-600 text-white px-3 py-2 rounded-xl shadow-xs">💰 KSh {float(school['wallet_balance']):,.2f}</span>
                <span class="bg-gradient-to-r from-indigo-800 to-indigo-900 text-white px-3 py-2 rounded-xl shadow-xs">{st['active_term']} • {st['active_cycle']}</span>
                <a href="/timetable/dashboard/{school_id}" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">📅 Timetable</a>
                <a href="/admin/reports/marks-supervision/{school_id}" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">🔍 Marks Supervision</a>
                <a href="/admin/audit-log/{school_id}" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">📋 Activity Log</a>
                <a href="/admin/school/profile/{school_id}" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">🏫 School Profile</a>
                <a href="/finance/dashboard/{school_id}" class="bg-amber-50 hover:bg-amber-100 text-amber-700 border border-amber-200 px-3 py-2 rounded-xl transition">💰 Finance</a>
                <a href="/logout" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">Log Out</a>
            </div>
        </header>

        {"<div class='bg-amber-50 border-b border-amber-200 text-amber-800 text-xs px-8 py-2.5 text-center font-semibold'>⚠️ That logo was saved to temporary server storage, not cloud storage — it will likely disappear the next time the server restarts. Check that SUPABASE_URL and a Supabase secret/service key (SUPABASE_KEY, SUPABASE_SECRET_KEY, or SUPABASE_SERVICE_ROLE_KEY) are set correctly on Render, and that a public 'logos' bucket exists in Supabase, then re-upload.</div>" if logo_storage == "local" else ""}
        {toast_trigger("Student registered successfully!") if student_added else ""}
        {toast_trigger("Staff account created — activate it from the Staff panel once ready.") if staff_added else ""}

        <div class="flex flex-col lg:flex-row flex-1 w-full max-w-[1600px] mx-auto">

            <!-- ============ LEFT SIDEBAR ============ -->
            <aside class="w-full lg:w-80 shrink-0 lg:sticky lg:top-[73px] lg:h-[calc(100vh-73px)] lg:overflow-y-auto border-r border-slate-200/70 bg-white px-5 py-6 space-y-6">

                <!-- Classes quick nav -->
                <div>
                    <h2 class="text-[11px] font-bold uppercase tracking-wider text-indigo-700 flex items-center gap-1.5 mb-2.5">
                        <span class="w-2 h-2 rounded-full bg-indigo-600"></span> Classes
                    </h2>
                    <div class="space-y-1 max-h-40 overflow-y-auto pr-1">
                        {"".join(f"<a href='/staff/bulk-entry/{school_id}?grade_name={urllib.parse.quote(c['grade_name'])}&stream={urllib.parse.quote('SINGLE STREAM' if (not c['stream'] or c['stream'].strip()=='' or c['stream'].upper()=='SINGLE STREAM') else c['stream'])}&education_level={urllib.parse.quote(c['education_level'])}' class='block text-xs font-semibold text-slate-600 hover:text-indigo-700 hover:bg-indigo-50 rounded-lg px-2.5 py-1.5 transition truncate'>{esc(c['grade_name'] if (not c['stream'] or c['stream'].strip()=='' or c['stream'].upper()=='SINGLE STREAM') else c['grade_name'] + ' — ' + c['stream'])}</a>" for c in classes) or "<p class='text-slate-400 text-xs italic px-2.5'>No classes yet.</p>"}
                    </div>
                </div>

                <!-- Staff -->
                <div class="pt-5 border-t border-slate-100">
                    <div class="flex items-center justify-between mb-2.5">
                        <h2 class="text-[11px] font-bold uppercase tracking-wider text-violet-700 flex items-center gap-1.5">
                            <span class="w-2 h-2 rounded-full bg-violet-600"></span> Staff
                        </h2>
                        <a href="/staff/register-panel/{school_id}" class="bg-violet-700 hover:bg-violet-800 text-white text-[10px] px-2.5 py-1 rounded-lg font-bold transition">+ Add</a>
                    </div>
                    <div class="max-h-56 overflow-y-auto pr-1 space-y-0.5">
                        {staff_panel_html or "<p class='text-slate-400 text-xs italic px-1 py-3'>No staff accounts yet.</p>"}
                    </div>
                </div>

                <!-- Class Rosters -->
                <div class="pt-5 border-t border-slate-100">
                    <h2 class="text-[11px] font-bold uppercase tracking-wider text-teal-700 flex items-center gap-1.5 mb-2.5">
                        <span class="w-2 h-2 rounded-full bg-teal-600"></span> Class Rosters
                    </h2>
                    <div class="space-y-2 max-h-72 overflow-y-auto pr-1">
                        {roster_sidebar_html or "<p class='text-slate-400 text-xs italic px-1 py-3'>No classes with students yet.</p>"}
                    </div>
                </div>

                <!-- System Wallet -->
                <div class="pt-5 border-t border-slate-100">
                    <h2 class="text-[11px] font-bold uppercase tracking-wider text-amber-700 flex items-center gap-1.5 mb-2.5">
                        <span class="w-2 h-2 rounded-full bg-amber-500"></span> System Wallet
                    </h2>
                    <div class="bg-gradient-to-br from-amber-50 to-white border border-amber-100 rounded-xl p-3 mb-3">
                        <p class="text-[10px] text-amber-700 font-bold uppercase tracking-wide">Current Balance</p>
                        <p class="text-lg font-black text-slate-900">KSh {float(school['wallet_balance']):,.2f}</p>
                    </div>
                    <form action="/api/v1/wallet/stkpush/{school_id}" method="post" class="space-y-2.5">
                        <div>
                            <label class="text-[11px] font-semibold text-slate-500 block mb-1">Lipa na M-PESA Phone Number</label>
                            <input type="text" name="phone_number" placeholder="07XXXXXXXX" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-amber-400" required>
                        </div>
                        <div>
                            <label class="text-[11px] font-semibold text-slate-500 block mb-1">Topup Amount (KSh)</label>
                            <input type="number" name="amount" value="500" min="10" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-amber-400" required>
                        </div>
                        <button type="submit" class="w-full bg-amber-500 hover:bg-amber-600 text-white text-xs py-2.5 rounded-xl font-semibold transition shadow-xs cursor-pointer">🚀 Request STK Push</button>
                    </form>
                </div>

                <!-- Settings -->
                <div class="pt-5 border-t border-slate-100">
                    <h2 class="text-[11px] font-bold uppercase tracking-wider text-slate-500 flex items-center gap-1.5 mb-2.5">
                        <span class="w-2 h-2 rounded-full bg-slate-500"></span> Settings
                    </h2>
                    <form action="/api/v1/settings/update/{school_id}" method="post" class="space-y-3">
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Academic Term</label>
                                <select name="active_term" class="w-full border border-slate-200 bg-white p-2 rounded-xl text-xs font-semibold text-slate-800 outline-none focus:border-slate-400">
                                    <option value="Term 1" {"selected" if st['active_term'] == 'Term 1' else ""}>Term 1</option>
                                    <option value="Term 2" {"selected" if st['active_term'] == 'Term 2' else ""}>Term 2</option>
                                    <option value="Term 3" {"selected" if st['active_term'] == 'Term 3' else ""}>Term 3</option>
                                </select>
                            </div>
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Assessment Phase</label>
                                <select name="active_cycle" class="w-full border border-slate-200 bg-white p-2 rounded-xl text-xs font-semibold text-slate-800 outline-none focus:border-slate-400">
                                    <option value="Opener" {"selected" if st['active_cycle'] == 'Opener' else ""}>Opener Exam</option>
                                    <option value="Midterm" {"selected" if st['active_cycle'] == 'Midterm' else ""}>Midterm Exam</option>
                                    <option value="End Term" {"selected" if st['active_cycle'] == 'End Term' else ""}>End Term Synthesis</option>
                                </select>
                            </div>
                        </div>

                        <div>
                            <label class="text-[11px] font-semibold text-slate-500 block mb-1">Theme Branding Color</label>
                            <select name="theme_color" class="w-full border border-slate-200 p-2 rounded-xl text-xs font-semibold bg-white outline-none focus:border-slate-400">
                                <option value="emerald" {"selected" if school.get('theme_color') == 'emerald' else ""}>Emerald Dynamic Green</option>
                                <option value="indigo" {"selected" if school.get('theme_color') == 'indigo' else ""}>Indigo Corporate Blue</option>
                                <option value="slate" {"selected" if school.get('theme_color') == 'slate' else ""}>Slate Minimalistic Gray</option>
                            </select>
                        </div>

                        <div class="bg-slate-50 p-3 rounded-xl border border-slate-100 flex items-center justify-between">
                            <div>
                                <label class="text-xs font-bold text-slate-800 block">Single Stream Mode</label>
                                <span class="text-[10px] text-slate-400 block">Hides class sorting columns</span>
                            </div>
                            <input type="checkbox" name="is_single_stream" value="true" {"checked" if is_single_stream else ""} class="w-4 h-4 text-emerald-600 border-slate-300 rounded focus:ring-emerald-500 cursor-pointer">
                        </div>

                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Opening Date</label>
                                <input type="date" name="opening_date" value="{esc(st['opening_date'])}" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-slate-400">
                            </div>
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Closing Date</label>
                                <input type="date" name="closing_date" value="{esc(st['closing_date'])}" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-slate-400">
                            </div>
                        </div>
                        <button type="submit" class="w-full bg-slate-800 hover:bg-slate-900 text-white text-xs py-2.5 rounded-xl font-semibold transition shadow-xs cursor-pointer">Commit Engine Settings</button>
                    </form>

                    <div class="mt-3 pt-3 border-t border-slate-100">
                        <form action="/api/v1/school/promote-classes/{school_id}" method="post"
                              onsubmit="return confirm('CRITICAL WARNING: Are you sure you want to promote all active student cohorts up 1 Grade Level? Grade 9 cohorts will safely move into Graduated Status.');">
                            <button type="submit" class="w-full bg-amber-50 border border-amber-200/80 text-amber-700 text-xs py-2.5 rounded-xl font-semibold hover:bg-amber-100/70 transition cursor-pointer">
                                🔄 Advance All Classes 1 Year
                            </button>
                        </form>
                    </div>
                </div>
            </aside>

            <!-- ============ CENTER: PERFORMANCE ANALYSIS & INTERACTIVE CARDS ============ -->
            <main class="flex-1 p-4 sm:p-8 space-y-8 min-w-0">
                {announcement_banners_html}
                {onboarding_html}
                {stats_html}
                {trend_chart_html}
                <div>
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400">🏫 Classroom Cohorts Grouping</h2>
                        <a href="/admin/student/new/{school_id}" class="bg-indigo-900 hover:bg-indigo-800 text-white text-xs px-3.5 py-2 rounded-xl font-semibold transition shadow-xs">+ Register New Student</a>
                    </div>
                    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                        {class_blocks_html or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No registered student profiles logged inside streams.</p>"}
                    </div>
                </div>
                {support_contact_html()}
                <p class="text-center text-[11px] text-slate-300 pt-6 pb-2">Powered by <img src="{ELIMU_HUB_ICON_DATA_URI}" class="inline w-4 h-4 align-text-bottom rounded" alt=""> <span class="font-bold text-slate-400">Elimu Hub</span></p>
            </main>
        </div>
    </body>
    </html>
    """)


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_school_id")
    response.delete_cookie("session_role")
    response.delete_cookie("session_user_id")
    return response


@app.get("/superadmin/dashboard", response_class=HTMLResponse)
def superadmin_dashboard(request: Request, backup_started: str = None, backup_error: str = None):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT sc.*,
                    (SELECT COUNT(*) FROM students st WHERE st.school_id = sc.id AND (st.status IS NULL OR st.status != 'GRADUATED')) AS student_count,
                    (SELECT COUNT(*) FROM users u WHERE u.school_id = sc.id AND u.role = 'staff') AS staff_count,
                    (SELECT u.full_name FROM users u WHERE u.school_id = sc.id AND u.role = 'admin' ORDER BY u.id ASC LIMIT 1) AS admin_full_name,
                    (SELECT u.email FROM users u WHERE u.school_id = sc.id AND u.role = 'admin' ORDER BY u.id ASC LIMIT 1) AS admin_email,
                    (SELECT u.phone_number FROM users u WHERE u.school_id = sc.id AND u.role = 'admin' ORDER BY u.id ASC LIMIT 1) AS admin_phone
                FROM schools sc
                ORDER BY
                    CASE sc.status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
                    sc.created_at DESC;
            """)
            schools = cur.fetchall()

            cur.execute("SELECT * FROM platform_announcements ORDER BY created_at DESC LIMIT 20;")
            announcements = cur.fetchall()

    total_schools = len(schools)
    pending_count = len([s for s in schools if s['status'] == 'pending'])
    active_count = len([s for s in schools if s['status'] == 'active'])
    deactivated_count = len([s for s in schools if s['status'] == 'deactivated'])
    total_students_all_schools = sum(s['student_count'] for s in schools)
    total_staff_all_schools = sum(s['staff_count'] for s in schools)

    status_styles = {
        'pending': ("bg-amber-50 text-amber-700 border-amber-200", "Pending Approval"),
        'active': ("bg-emerald-50 text-emerald-700 border-emerald-200", "Active"),
        'deactivated': ("bg-rose-50 text-rose-700 border-rose-200", "Deactivated"),
    }

    rows_html = ""
    for s in schools:
        style_class, status_label = status_styles.get(s['status'], ("bg-slate-50 text-slate-700 border-slate-200", s['status']))
        action_buttons = ""
        if s['status'] == 'pending':
            action_buttons += f"""
                <form action="/api/v1/superadmin/school/approve/{s['id']}" method="post" class="inline">
                    <button type="submit" class="text-emerald-700 hover:text-emerald-900 font-bold">Approve</button>
                </form>
            """
        elif s['status'] == 'active':
            action_buttons += f"""
                <form action="/api/v1/superadmin/school/deactivate/{s['id']}" method="post" class="inline" onsubmit="return confirm('Deactivate {esc(s['name'])}? Its admin and staff will be unable to log in until reactivated.');">
                    <button type="submit" class="text-amber-700 hover:text-amber-900 font-bold">Deactivate</button>
                </form>
            """
        elif s['status'] == 'deactivated':
            action_buttons += f"""
                <form action="/api/v1/superadmin/school/reactivate/{s['id']}" method="post" class="inline">
                    <button type="submit" class="text-emerald-700 hover:text-emerald-900 font-bold">Reactivate</button>
                </form>
            """
        action_buttons += f"""
            <a href="/superadmin/school/reset-admin-password/{s['id']}" class="text-indigo-700 hover:text-indigo-900 font-bold ml-3">Reset Admin Password</a>
        """
        action_buttons += f"""
            <form action="/api/v1/superadmin/school/delete/{s['id']}" method="post" class="inline" onsubmit="return confirm('Permanently delete {esc(s['name'])}? This deletes ALL of its students, scores, staff, and settings. This cannot be undone.');">
                <button type="submit" class="text-rose-600 hover:text-rose-800 font-bold ml-3">Delete</button>
            </form>
        """

        rows_html += f"""
        <tr class="border-b border-slate-100 text-sm">
            <td class="p-4">
                <p class="font-bold text-slate-900">{esc(s['name'])}</p>
                <p class="text-xs text-slate-400">{esc(s['sub_county'])}</p>
            </td>
            <td class="p-4 text-center"><span class="text-xs font-bold px-2.5 py-1 rounded-full border {style_class}">{status_label}</span></td>
            <td class="p-4">
                <p class="text-xs font-semibold text-slate-700">{esc(s['admin_full_name'] or '—')}</p>
                {f"<p class='text-xs text-slate-500'><a href='tel:{esc(s['admin_phone'])}' class='hover:underline'>📞 {esc(s['admin_phone'])}</a></p>" if s['admin_phone'] else "<p class='text-xs text-slate-300 italic'>No phone on file</p>"}
                {f"<p class='text-[11px] text-slate-400'><a href='mailto:{esc(s['admin_email'])}' class='hover:underline'>{esc(s['admin_email'])}</a></p>" if s['admin_email'] else ""}
            </td>
            <td class="p-4 text-center font-semibold">{s['student_count']}</td>
            <td class="p-4 text-center font-semibold">{s['staff_count']}</td>
            <td class="p-4 text-center">KSh {float(s['wallet_balance']):,.2f}</td>
            <td class="p-4 text-xs text-slate-400">{s['created_at'].strftime('%d %b %Y') if s['created_at'] else '—'}</td>
            <td class="p-4 text-right text-xs">{action_buttons}</td>
        </tr>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html class="h-full">
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Super Admin Portal</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F8FAFC] text-slate-800 antialiased min-h-full">
        {TOAST_CONTAINER_HTML}
        <header class="bg-slate-900 text-white px-8 py-4 flex justify-between items-center">
            <h1 class="text-base font-bold tracking-tight">🛡️ Super Admin Portal</h1>
            <div class="flex items-center gap-2">
                <form action="/api/v1/superadmin/backup-now" method="post" onsubmit="return confirm('Trigger an on-demand database backup now? This runs in the background via GitHub Actions and does not affect any school\\'s live data.');">
                    <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 text-white px-3 py-2 rounded-xl text-xs font-bold transition">💾 Backup Now</button>
                </form>
                <a href="/logout" class="bg-white/10 hover:bg-white/20 text-white border border-white/20 px-3 py-2 rounded-xl text-xs transition">Log Out</a>
            </div>
        </header>

        {toast_trigger("Backup triggered — check the Actions tab in your GitHub repo for progress.") if backup_started else ""}
        {toast_trigger(backup_error, "error") if backup_error else ""}

        <div class="p-8 max-w-6xl mx-auto w-full">
            <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:#4f46e5;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Total Schools</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">{total_schools}</p>
                </div>
                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:#d97706;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Pending Approval</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">{pending_count}</p>
                </div>
                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:#059669;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Active</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">{active_count}</p>
                </div>
                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:#e11d48;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Deactivated</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">{deactivated_count}</p>
                </div>
                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:#0891b2;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Total Students</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">{total_students_all_schools}</p>
                </div>
                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-5 border-l-4" style="border-left-color:#7c3aed;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Total Staff</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">{total_staff_all_schools}</p>
                </div>
            </div>

            <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs overflow-hidden">
                <div class="p-5 border-b border-slate-100 bg-slate-50/40">
                    <h2 class="text-base font-bold text-slate-900">All Schools</h2>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left border-collapse">
                        <thead>
                            <tr class="bg-slate-50 text-slate-500 text-xs font-semibold border-b border-slate-100">
                                <th class="p-4">School</th>
                                <th class="p-4 text-center">Status</th>
                                <th class="p-4">Admin Contact</th>
                                <th class="p-4 text-center">Students</th>
                                <th class="p-4 text-center">Staff</th>
                                <th class="p-4 text-center">Wallet</th>
                                <th class="p-4">Registered</th>
                                <th class="p-4 text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows_html or "<tr><td colspan='8' class='text-center p-8 text-slate-400 text-sm italic'>No schools registered yet.</td></tr>"}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-6 mt-6">
                <h2 class="text-sm font-bold text-slate-800 mb-1">📣 Platform Announcements</h2>
                <p class="text-xs text-slate-400 mb-4">Active announcements show as a banner on every school admin's dashboard.</p>
                <form action="/api/v1/superadmin/announcements/add" method="post" class="flex gap-2 mb-4">
                    <input type="text" name="message" placeholder="e.g. Finance module coming soon!" class="flex-1 border p-2.5 rounded-xl text-sm" required maxlength="300">
                    <button type="submit" class="bg-indigo-800 hover:bg-indigo-900 text-white font-bold px-5 py-2.5 rounded-xl text-sm transition">+ Post</button>
                </form>
                <div class="space-y-2">
                    {"".join(f'''
                    <div class="flex items-center justify-between gap-3 p-3 rounded-xl border {'border-emerald-200 bg-emerald-50/50' if a['is_active'] else 'border-slate-100 bg-slate-50'}">
                        <div class="min-w-0">
                            <p class="text-sm {'text-slate-800 font-semibold' if a['is_active'] else 'text-slate-400 line-through'}">{esc(a['message'])}</p>
                            <p class="text-[11px] text-slate-400 mt-0.5">{a['created_at'].strftime('%d %b %Y, %H:%M') if a['created_at'] else ''} {'· Live' if a['is_active'] else '· Hidden'}</p>
                        </div>
                        <div class="flex items-center gap-2 shrink-0">
                            <form action="/api/v1/superadmin/announcements/toggle/{a['id']}" method="post">
                                <button type="submit" class="text-xs font-bold {'text-amber-700 hover:text-amber-900' if a['is_active'] else 'text-emerald-700 hover:text-emerald-900'}">{'Hide' if a['is_active'] else 'Show'}</button>
                            </form>
                            <form action="/api/v1/superadmin/announcements/delete/{a['id']}" method="post" onsubmit="return confirm('Delete this announcement permanently?');">
                                <button type="submit" class="text-xs font-bold text-rose-600 hover:text-rose-800">Delete</button>
                            </form>
                        </div>
                    </div>
                    ''' for a in announcements) or "<p class='text-slate-400 text-xs italic text-center py-4'>No announcements posted yet.</p>"}
                </div>
            </div>

            {support_contact_html()}
            <p class="text-center text-[11px] text-slate-500 pt-6 pb-2">Powered by <img src="{ELIMU_HUB_ICON_DATA_URI}" class="inline w-4 h-4 align-text-bottom rounded" alt=""> <span class="font-bold text-slate-300">Elimu Hub</span></p>
        </div>
    </body>
    </html>
    """)


@app.post("/api/v1/superadmin/school/approve/{school_id}")
def superadmin_approve_school(school_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE schools SET status = 'active' WHERE id = %s;", (school_id,))
            conn.commit()
            log_audit_action(cur, request, school_id, "school_approved", "Approved school registration")
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.post("/api/v1/superadmin/school/deactivate/{school_id}")
def superadmin_deactivate_school(school_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE schools SET status = 'deactivated' WHERE id = %s;", (school_id,))
            conn.commit()
            log_audit_action(cur, request, school_id, "school_deactivated", "Deactivated school")
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.post("/api/v1/superadmin/school/reactivate/{school_id}")
def superadmin_reactivate_school(school_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE schools SET status = 'active' WHERE id = %s;", (school_id,))
            conn.commit()
            log_audit_action(cur, request, school_id, "school_reactivated", "Reactivated school")
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.post("/api/v1/superadmin/backup-now")
def trigger_backup_now(request: Request):
    """Manually triggers the existing GitHub Actions backup workflow (which
    already runs pg_dump + uploads to Supabase nightly) instead of waiting
    for its schedule. This route only ever reads config and calls GitHub's
    API — it never touches the database directly, so it can't affect any
    school's data."""
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    if not GITHUB_PAT or not GITHUB_REPO:
        return RedirectResponse(
            url="/superadmin/dashboard?backup_error=" + urllib.parse.quote(
                "Backup Now isn't configured yet. Set GITHUB_PAT (a GitHub Personal Access Token with 'repo' and 'workflow' scope) and GITHUB_REPO (e.g. 'yourusername/report_form_engine') in Render's Environment tab."
            ),
            status_code=303,
        )

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_BACKUP_WORKFLOW_FILE}/dispatches"
    try:
        response = http_requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {GITHUB_PAT}",
                "Accept": "application/vnd.github+json",
            },
            json={"ref": "main"},
            timeout=10,
        )
    except http_requests.RequestException as e:
        return RedirectResponse(
            url="/superadmin/dashboard?backup_error=" + urllib.parse.quote(f"Could not reach GitHub: {e}"),
            status_code=303,
        )

    if response.status_code == 204:
        return RedirectResponse(url="/superadmin/dashboard?backup_started=1", status_code=303)

    return RedirectResponse(
        url="/superadmin/dashboard?backup_error=" + urllib.parse.quote(
            f"GitHub rejected the request (status {response.status_code}). Check that GITHUB_PAT is valid and GITHUB_REPO/workflow filename are correct."
        ),
        status_code=303,
    )


@app.post("/api/v1/superadmin/school/delete/{school_id}")
def superadmin_delete_school(school_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school_row = cur.fetchone()
            school_name = school_row[0] if school_row else f"school #{school_id}"

            # Cascades to school_settings, users (admin+staff), students, and
            # (via students) student_scores. Classes and learning_areas are
            # shared lookup tables, not school-specific, so they're untouched.
            cur.execute("DELETE FROM schools WHERE id = %s;", (school_id,))
            conn.commit()

            # Logged with school_id=NULL deliberately — audit_log.school_id
            # cascades on delete, so logging against the now-deleted school's
            # own id would erase this very record the instant it's inserted.
            log_audit_action(cur, request, None, "school_deleted", f"Permanently deleted school: {school_name} (was ID {school_id})")
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.post("/api/v1/superadmin/announcements/add")
def add_platform_announcement(request: Request, message: str = Form(...)):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Announcement message cannot be empty.")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO platform_announcements (message, is_active) VALUES (%s, TRUE);", (message,))
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.post("/api/v1/superadmin/announcements/toggle/{announcement_id}")
def toggle_platform_announcement(announcement_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE platform_announcements SET is_active = NOT is_active WHERE id = %s;", (announcement_id,))
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.post("/api/v1/superadmin/announcements/delete/{announcement_id}")
def delete_platform_announcement(announcement_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM platform_announcements WHERE id = %s;", (announcement_id,))
            conn.commit()
    return RedirectResponse(url="/superadmin/dashboard", status_code=303)


@app.get("/superadmin/school/reset-admin-password/{school_id}", response_class=HTMLResponse)
def superadmin_reset_admin_password_form(school_id: int, request: Request, done: str = None):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT id, email, full_name FROM users WHERE school_id = %s AND role = 'admin' ORDER BY id ASC LIMIT 1;", (school_id,))
            admin = cur.fetchone()

    result_html = ""
    if done == "1" and admin:
        # The generated password is passed through a one-time query param
        # from the POST handler's redirect and shown exactly once.
        new_password = request.query_params.get("pwd", "")
        result_html = f"""
        <div class="bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-lg mb-4">
            <p class="font-bold mb-1">New password generated:</p>
            <p class="font-mono text-base bg-white border border-emerald-300 rounded px-3 py-2 inline-block">{esc(new_password)}</p>
            <p class="text-xs mt-2">Copy this now and relay it to the admin securely — it will not be shown again. They can change it after logging in.</p>
        </div>
        """

    if not admin:
        admin_block = "<p class='text-sm text-rose-600'>No admin account found for this school.</p>"
    else:
        admin_block = f"""
        <p class="text-xs text-slate-500 mb-4">Admin account: <b>{esc(admin['full_name'] or admin['email'])}</b> ({esc(admin['email'])})</p>
        <form action="/api/v1/superadmin/school/reset-admin-password/{school_id}" method="post" onsubmit="return confirm('Generate a new password for this admin? Their current password will stop working immediately.');">
            <button type="submit" class="w-full bg-indigo-700 text-white p-3 rounded-lg font-black tracking-wide hover:bg-indigo-800 transition shadow-md">Generate New Password</button>
        </form>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Reset Admin Password</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-900 flex items-center justify-center min-h-screen font-sans p-6">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md border-t-8 border-indigo-700">
            <h2 class="text-xl font-black text-slate-800 mb-1">Reset Admin Password</h2>
            <p class="text-xs text-slate-400 mb-4">{esc(school['name'])}</p>
            {result_html}
            {admin_block}
            <div class="mt-4 text-center">
                <a href="/superadmin/dashboard" class="text-xs text-slate-400 hover:text-slate-600 hover:underline">← Back to Super Admin Portal</a>
            </div>
        </div>
    </body>
    </html>
    """


@app.post("/api/v1/superadmin/school/reset-admin-password/{school_id}")
def superadmin_reset_admin_password_submit(school_id: int, request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    new_password = secrets.token_urlsafe(9)  # ~12 readable chars, strong enough for a temp password
    hashed_password = get_password_hash(new_password)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE school_id = %s AND role = 'admin' ORDER BY id ASC LIMIT 1;", (school_id,))
            admin = cur.fetchone()
            if not admin:
                raise HTTPException(status_code=404, detail="No admin account found for this school.")
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s;", (hashed_password, admin['id']))
            conn.commit()

    return RedirectResponse(
        url=f"/superadmin/school/reset-admin-password/{school_id}?done=1&pwd={urllib.parse.quote(new_password)}",
        status_code=303
    )


@app.get("/admin/system/diagnostics/{school_id}", response_class=HTMLResponse)
def storage_diagnostics(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    configured = supabase_client is not None
    status_html = (
        "<span style='color:#059669;font-weight:bold;'>✅ Configured</span>" if configured
        else "<span style='color:#dc2626;font-weight:bold;'>❌ Not configured</span>"
    )
    url_present = "Set" if SUPABASE_URL else "Missing"
    key_present = "Set" if SUPABASE_KEY else "Missing"
    last_error_html = (
        f"<pre style='background:#fef2f2;color:#991b1b;padding:12px;border-radius:8px;white-space:pre-wrap;font-size:12px;'>{esc(_last_storage_error)}</pre>"
        if _last_storage_error else
        "<p style='color:#64748b;font-size:12px;'>No upload errors recorded since the app last started.</p>"
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Storage Diagnostics</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-8 font-sans">
        <div class="max-w-lg mx-auto bg-white rounded-2xl border shadow p-6 space-y-4">
            <h2 class="text-lg font-black text-slate-800">🔧 Logo Storage Diagnostics</h2>
            <div class="text-sm space-y-1">
                <p><b>Supabase Storage:</b> {status_html}</p>
                <p><b>SUPABASE_URL env var:</b> {url_present}</p>
                <p><b>Supabase key env var:</b> {key_present} <span class="text-slate-400">(accepts SUPABASE_KEY, SUPABASE_SECRET_KEY, or SUPABASE_SERVICE_ROLE_KEY)</span></p>
            </div>
            <div>
                <p class="text-xs font-bold uppercase text-slate-500 mb-1">Last upload error (this server process)</p>
                {last_error_html}
            </div>
            <p class="text-[11px] text-slate-400">
                If Supabase shows "Not configured", set SUPABASE_URL and a secret-level key in Render → Environment.
                Newer Supabase projects call this key <b>SUPABASE_SECRET_KEY</b> (shown as "secret key" in
                Project Settings → API Keys) instead of the older "service_role" naming — either name works here.
                Do not use the "publishable"/"anon" key; it doesn't have permission to write to storage. If it shows
                Configured but there's a recorded error, that error message is exactly what Supabase's API
                returned when the upload was attempted (e.g. bucket not found, or an access-policy rejection).
            </p>
            <a href="/admin/dashboard/{school_id}" class="text-xs font-bold text-indigo-700 hover:underline">← Back to Dashboard</a>
        </div>
    </body>
    </html>
    """


@app.get("/staff/dashboard/{school_id}", response_class=HTMLResponse)
def staff_dashboard(school_id: int, request: Request, user_id: int = None, student_added: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()

            staff_email = None
            staff_name = None
            effective_user_id = user_id or request.cookies.get("session_user_id")
            if effective_user_id:
                cur.execute("SELECT email, full_name FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (effective_user_id, school_id))
                staff_user = cur.fetchone()
                if staff_user:
                    staff_email = staff_user['email']
                    staff_name = staff_user['full_name']

            cur.execute("""
                SELECT DISTINCT c.id, c.grade_name, s.stream, c.education_level
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC;
            """, (school_id,))
            classes = cur.fetchall()

    st = settings or {'active_term': 'Term 1', 'active_cycle': 'End Term', 'is_single_stream': False}

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"""
        <div class='w-11 h-11 rounded-xl bg-slate-50 border border-slate-200 flex items-center justify-center p-1.5 shadow-2xs'>
            <img src='{final_src}' class='max-w-full max-h-full object-contain' />
        </div>
        """

    class_blocks = []
    for c in classes:
        is_stream_blank = not c['stream'] or c['stream'].strip() == "" or c['stream'].upper() == "SINGLE STREAM"
        if is_stream_blank:
            display_title = c['grade_name']
            stream_param = "SINGLE STREAM"
        else:
            display_title = f"{c['grade_name']} — Stream: {esc(c['stream'])}"
            stream_param = c['stream']

        encoded_grade = urllib.parse.quote(c['grade_name'])
        encoded_stream = urllib.parse.quote(stream_param)
        encoded_level = urllib.parse.quote(c['education_level'])

        class_blocks.append(f"""
            <div class='bg-white border border-slate-200/80 p-5 rounded-2xl shadow-xs hover:shadow-md transition-all flex flex-col justify-between group'>
                <div>
                    <span class='text-[10px] bg-slate-100 text-slate-600 px-2.5 py-1 rounded-md font-bold uppercase tracking-wider'>{c['education_level']}</span>
                    <h3 class='text-base font-black text-slate-800 mt-2.5 group-hover:text-slate-900'>{display_title}</h3>
                </div>
                <div class='grid grid-cols-2 sm:grid-cols-3 gap-2 mt-5'>
                    <a href='/staff/bulk-entry/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' class='bg-indigo-900 hover:bg-indigo-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Bulk Entry</a>
                    <a href='/admin/students/roster/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-slate-700 hover:bg-slate-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Class List</a>
                    <a href='/api/v1/reports/bulk-print/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-emerald-600 text-white text-center text-xs py-2 rounded-xl font-semibold hover:bg-emerald-700 transition shadow-xs'>Bulk Print</a>
                    <a href='/admin/reports/merit-list/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}' target='_blank' class='bg-violet-700 hover:bg-violet-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Merit List</a>
                    <a href='/admin/reports/subject-analysis/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-amber-600 hover:bg-amber-700 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Subj. Analysis</a>
                    <a href='/admin/reports/top10/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-rose-600 hover:bg-rose-700 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Top 10</a>
                    <a href='/admin/reports/top-subject/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-cyan-700 hover:bg-cyan-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Top/Subject</a>
                </div>
            </div>
        """)
    class_blocks_html = "".join(class_blocks)

    welcome_html = f"<p class='text-[11px] text-indigo-700 font-bold'>Welcome, {esc(staff_name.split(' ')[0])}</p>" if staff_name else ""
    identity_html = f"<p class='text-xs text-slate-500'>{esc(staff_email)}</p>" if staff_email else ""

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html class="h-full">
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Staff Portal - {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F8FAFC] text-slate-800 antialiased min-h-full flex flex-col relative">
        {TOAST_CONTAINER_HTML}

        <header class="bg-white border-b border-slate-200/80 px-8 py-4 flex justify-between items-center sticky top-0 z-40 backdrop-blur-md bg-white/90 shadow-2xs">
            <div class="flex items-center space-x-4">
                {logo_html}
                <div>
                    <h1 class="text-base font-bold text-slate-900 tracking-tight">{esc(school['name'])}</h1>
                    {welcome_html}
                    {identity_html}
                </div>
            </div>
            <div class="flex items-center space-x-3 text-xs font-semibold">
                <span class="bg-indigo-50 text-indigo-700 border border-indigo-100 px-3 py-2 rounded-xl">Staff Portal</span>
                <span class="bg-indigo-900 text-white px-3 py-2 rounded-xl shadow-xs">{st['active_term']} • {st['active_cycle']}</span>
                <a href="/timetable/dashboard/{school_id}" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">📅 Timetable</a>
                <a href="/logout" class="bg-white hover:bg-slate-100 text-slate-500 border border-slate-200 px-3 py-2 rounded-xl transition">Log Out</a>
            </div>
        </header>

        {toast_trigger("Student registered successfully!") if student_added else ""}

        <div class="p-8 max-w-6xl mx-auto w-full flex-1">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400">🏫 Your Classroom Cohorts</h2>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                {class_blocks_html or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No classes have been set up for this school yet.</p>"}
            </div>
            {support_contact_html()}
            <p class="text-center text-[11px] text-slate-300 pt-6 pb-2">Powered by <img src="{ELIMU_HUB_ICON_DATA_URI}" class="inline w-4 h-4 align-text-bottom rounded" alt=""> <span class="font-bold text-slate-400">Elimu Hub</span></p>
        </div>
    </body>
    </html>
    """)


@app.get("/admin/students/roster/{school_id}", response_class=HTMLResponse)
def print_class_roster(school_id: int, grade_name: str, education_level: str, stream: str, request: Request):
    session_school_id = request.cookies.get("session_school_id")
    if not session_school_id:
        return RedirectResponse(url="/login?error=Authentication+required.", status_code=303)
    if str(session_school_id) != str(school_id):
        raise HTTPException(
            status_code=403,
            detail="Access Denied: You do not have administrative privileges for this institution."
        )

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("""
                SELECT s.admission_number, s.first_name, s.middle_name, s.last_name, s.stream
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                  AND (%s = 'SINGLE STREAM' OR s.stream = %s)
                ORDER BY s.admission_number ASC;
            """, (school_id, grade_name, education_level, stream, stream))
            roster_students = cur.fetchall()

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:64px;height:64px;object-fit:contain;' />"

    def _full_name(st):
        parts = [st['first_name'], st.get('middle_name'), st['last_name']]
        return " ".join(esc(p) for p in parts if p)

    rows_html = "".join(
        f"<tr><td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;color:#94a3b8;'>{i}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;font-family:monospace;'>{esc(st['admission_number'])}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;'>{_full_name(st)}</td></tr>"
        for i, st in enumerate(roster_students, start=1)
    )

    class_title = grade_name if stream == "SINGLE STREAM" else f"{grade_name} — {stream}"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Class Roster — {esc(class_title)}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 32px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th {{ text-align:left; padding:8px 12px; background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:12px; text-transform:uppercase; color:#64748b; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#059669;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #059669;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'])}</h1>
                <p style="margin:2px 0 0;font-size:12px;color:#64748b;">{esc(class_title)} — Class Roster ({len(roster_students)} students)</p>
            </div>
        </div>
        <table>
            <thead><tr><th style="text-align:center;">S.No</th><th>Adm No.</th><th>Full Name</th></tr></thead>
            <tbody>{rows_html or "<tr><td colspan='3' style='padding:20px;text-align:center;color:#94a3b8;'>No students in this class.</td></tr>"}</tbody>
        </table>
    </body>
    </html>
    """


@app.get("/admin/reports/merit-list/{school_id}", response_class=HTMLResponse)
def print_merit_list(school_id: int, grade_name: str, education_level: str, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            st = settings or {'active_term': 'Term 1', 'active_cycle': 'End Term', 'active_year': 2026}

            # Whole grade, every stream combined — matches how this report is
            # actually used at the school (one merit list per grade, not per stream).
            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.middle_name, s.last_name, s.stream
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (school_id, grade_name, education_level))
            students = cur.fetchall()

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            score_map = {}
            if students:
                student_ids = [s['id'] for s in students]
                cur.execute("""
                    SELECT student_id, learning_area_id, raw_score
                    FROM student_scores
                    WHERE student_id = ANY(%s) AND cycle_name = %s;
                """, (student_ids, st['active_cycle']))
                for row in cur.fetchall():
                    score_map.setdefault(row['student_id'], {})[row['learning_area_id']] = float(row['raw_score'])

    total_subjects = len(subjects)

    # Per-student computed metrics for this single exam sitting
    computed = []
    for s in students:
        s_scores = score_map.get(s['id'], {})
        total_marks = 0.0
        total_points = 0
        subjects_entered = 0
        subject_cells = {}
        for sub in subjects:
            score = s_scores.get(sub['id'])
            if score is None:
                subject_cells[sub['id']] = None
            else:
                metrics = evaluate_performance_metrics(score)
                subject_cells[sub['id']] = (score, metrics['pld'])
                total_marks += score
                total_points += metrics['points']
                subjects_entered += 1
        avg_marks = (total_marks / subjects_entered) if subjects_entered else 0.0
        avg_points = (total_points / subjects_entered) if subjects_entered else 0.0
        overall_points_key = min(8, max(1, round(avg_points))) if subjects_entered else 0
        overall_level = POINTS_TO_PLD.get(overall_points_key, "N/A")
        computed.append({
            'student': s,
            'subject_cells': subject_cells,
            'subjects_entered': subjects_entered,
            'total_marks': total_marks,
            'avg_marks': avg_marks,
            'total_points': total_points,
            'avg_points': avg_points,
            'overall_level': overall_level,
        })

    # Rank by total POINTS (not marks) — points reflect performance level
    # (EE1..BE2) per subject, which is the correct CBC ranking basis.
    def _rank_by_total_points(rows):
        ranked = sorted(rows, key=lambda r: r['total_points'], reverse=True)
        positions = {}
        last_points = None
        last_pos = 0
        for i, r in enumerate(ranked, start=1):
            if r['total_points'] != last_points:
                last_pos = i
                last_points = r['total_points']
            positions[r['student']['id']] = last_pos
        return positions

    overall_positions = _rank_by_total_points(computed)
    stream_groups = {}
    for row in computed:
        stream_groups.setdefault(row['student']['stream'], []).append(row)
    stream_positions = {}
    for stream_name, rows in stream_groups.items():
        stream_positions.update(_rank_by_total_points(rows))

    # Class-wide averages per subject, for the footer summary table
    subject_footer = []
    for sub in subjects:
        vals = [c['subject_cells'][sub['id']][0] for c in computed if c['subject_cells'][sub['id']] is not None]
        if vals:
            avg_mark = sum(vals) / len(vals)
            avg_pts = sum(evaluate_performance_metrics(v)['points'] for v in vals) / len(vals)
            level_key = min(8, max(1, round(avg_pts)))
            level = POINTS_TO_PLD.get(level_key, "N/A")
        else:
            avg_mark, avg_pts, level = 0.0, 0.0, "N/A"
        subject_footer.append({'name': sub['name'], 'avg_mark': avg_mark, 'avg_pts': avg_pts, 'level': level})

    class_average_marks = (sum(c['total_marks'] for c in computed) / len(computed)) if computed else 0.0

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:64px;height:64px;object-fit:contain;' />"

    exam_code = f"{grade_name.replace(' ', '').upper()}{st.get('active_year', 2026)}{str(st['active_cycle']).upper().replace(' ', '')}"

    subject_header_cells = "".join(f"<th style='text-align:center;'>{esc(abbreviate_subject(sub['name']))}</th>" for sub in subjects)

    # Display order: by overall position (rank 1 first) — this is a merit
    # list, so it should read top-to-bottom by performance, not roster order.
    display_order = sorted(computed, key=lambda r: overall_positions[r['student']['id']])

    body_rows = []
    for i, row in enumerate(display_order, start=1):
        s = row['student']
        subject_cells_html = ""
        for sub in subjects:
            cell = row['subject_cells'][sub['id']]
            if cell is None:
                subject_cells_html += "<td style='text-align:center;color:#cbd5e1;'>-</td>"
            else:
                score, pld = cell
                subject_cells_html += f"<td style='text-align:center;white-space:nowrap;'>{score:.0f} {pld}</td>"

        body_rows.append(f"""
            <tr>
                <td style='text-align:center;'>{i}</td>
                <td style='font-family:monospace;'>{esc(s['admission_number'])}</td>
                <td>{esc(full_student_name(s))}</td>
                <td style='text-align:center;'>{esc(s['stream'])}</td>
                <td style='text-align:center;font-weight:bold;'>{stream_positions.get(s['id'], '-')}</td>
                <td style='text-align:center;font-weight:bold;'>{overall_positions.get(s['id'], '-')}</td>
                <td style='text-align:center;color:#94a3b8;'>—</td>
                <td style='text-align:center;color:#94a3b8;'>—</td>
                {subject_cells_html}
                <td style='text-align:center;'>{row['subjects_entered']}</td>
                <td style='text-align:center;font-weight:bold;'>{row['total_marks']:.0f}</td>
                <td style='text-align:center;'>{row['avg_marks']:.1f}</td>
                <td style='text-align:center;font-weight:bold;'>{row['total_points']}</td>
                <td style='text-align:center;'>{row['avg_points']:.2f}</td>
                <td style='text-align:center;font-weight:bold;'>{row['overall_level']}</td>
            </tr>
        """)
    rows_html = "".join(body_rows)

    footer_subject_cells = "".join(
        f"<th style='text-align:center;'>{esc(abbreviate_subject(f['name']))}</th>" for f in subject_footer
    )
    footer_avg_marks_cells = "".join(
        f"<td style='text-align:center;'>{f['avg_mark']:.2f}%</td>" for f in subject_footer
    )
    footer_avg_pts_cells = "".join(
        f"<td style='text-align:center;'>{f['avg_pts']:.4f} {f['level']}</td>" for f in subject_footer
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Merit List — {esc(grade_name)}</title>
        <style>
            @page {{ size: landscape; margin: 10mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; font-size: 11px; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
            th, td {{ padding: 4px 6px; border-bottom: 1px solid #e2e8f0; white-space: nowrap; }}
            th {{ text-align:left; background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:9px; text-transform:uppercase; color:#64748b; }}
            .header-fields td {{ border: none; padding: 2px 10px 2px 0; font-size: 11px; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #4f46e5;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'])}</h1>
                <p style="margin:2px 0 0;font-size:13px;font-weight:bold;">REPORT: STUDENTS' PERFORMANCE MERIT LIST</p>
            </div>
        </div>
        <table class="header-fields" style="margin-top:8px;">
            <tr>
                <td><b>CLASS:</b> {esc(grade_name)}</td>
                <td><b>TERM:</b> {esc(str(st['active_term']))}</td>
                <td><b>YEAR:</b> {esc(str(st.get('active_year', 2026)))}</td>
                <td><b>EXAM NAME:</b> {esc(str(st['active_cycle']).upper())}</td>
            </tr>
            <tr>
                <td><b>EXAM CODE:</b> {esc(exam_code)}</td>
                <td colspan="3"><b>STUDENTS:</b> {len(computed)}</td>
            </tr>
        </table>
        <table>
            <thead>
                <tr>
                    <th>S.No</th><th>Adm No.</th><th>Student Name</th><th style="text-align:center;">Stream</th>
                    <th style="text-align:center;">Stream Pos</th><th style="text-align:center;">Overall Pos</th>
                    <th style="text-align:center;">Prv Str Pos</th><th style="text-align:center;">Prv Ovr Pos</th>
                    {subject_header_cells}
                    <th style="text-align:center;">Sub. Entry</th><th style="text-align:center;">Total Marks</th>
                    <th style="text-align:center;">Avg Marks</th><th style="text-align:center;">Total Points</th>
                    <th style="text-align:center;">Avg Points</th><th style="text-align:center;">Level</th>
                </tr>
            </thead>
            <tbody>{rows_html or f"<tr><td colspan='{14 + total_subjects}' style='padding:20px;text-align:center;color:#94a3b8;'>No students found for this grade.</td></tr>"}</tbody>
        </table>

        <div style="margin-top:24px; font-weight:bold; font-size:13px;">CLASS AVERAGE MARKS: {class_average_marks:.1f}</div>
        <table style="margin-top:6px;">
            <thead><tr><th>Subject</th>{footer_subject_cells}</tr></thead>
            <tbody>
                <tr><td><b>Avg. Marks</b></td>{footer_avg_marks_cells}</tr>
                <tr><td><b>Avg. Points</b></td>{footer_avg_pts_cells}</tr>
            </tbody>
        </table>

        <p style="margin-top:16px; font-size:10px; color:#64748b;">
            — Student position is assigned using Total Marks.<br>
            — Student performance level is calculated using the student's average points.<br>
            — "Prv Str Pos" / "Prv Ovr Pos" (previous exam positions) are not yet tracked by this system and are shown blank.
        </p>
    </body>
    </html>
    """


@app.get("/admin/reports/top10/{school_id}", response_class=HTMLResponse)
def print_top10_per_stream(school_id: int, grade_name: str, education_level: str, stream: str, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            st = settings or {'active_term': 'Term 1', 'active_cycle': 'End Term', 'active_year': 2026}

            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.middle_name, s.last_name
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED');
            """, (school_id, grade_name, education_level, stream))
            students = cur.fetchall()

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = cur.fetchall()

            score_map = {}
            if students:
                student_ids = [s['id'] for s in students]
                cur.execute("""
                    SELECT student_id, learning_area_id, raw_score
                    FROM student_scores
                    WHERE student_id = ANY(%s) AND cycle_name = %s;
                """, (student_ids, st['active_cycle']))
                for row in cur.fetchall():
                    score_map.setdefault(row['student_id'], {})[row['learning_area_id']] = float(row['raw_score'])

    computed = []
    for s in students:
        s_scores = score_map.get(s['id'], {})
        total_marks, total_points, subjects_entered = 0.0, 0, 0
        for sub in subjects:
            score = s_scores.get(sub['id'])
            if score is not None:
                total_marks += score
                total_points += evaluate_performance_metrics(score)['points']
                subjects_entered += 1
        avg_points = (total_points / subjects_entered) if subjects_entered else 0.0
        overall_level = POINTS_TO_PLD.get(min(8, max(1, round(avg_points))), "N/A") if subjects_entered else "N/A"
        computed.append({
            'student': s, 'total_marks': total_marks, 'total_points': total_points,
            'avg_marks': (total_marks / subjects_entered) if subjects_entered else 0.0,
            'overall_level': overall_level,
        })

    top10 = sorted(computed, key=lambda r: r['total_points'], reverse=True)[:10]

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:64px;height:64px;object-fit:contain;' />"

    class_title = grade_name if stream == "SINGLE STREAM" else f"{grade_name} — Stream {stream}"

    rows_html = "".join(f"""
        <tr>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{i}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;font-family:monospace;'>{esc(r['student']['admission_number'])}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;'>{esc(full_student_name(r['student']))}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{r['total_marks']:.0f}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{r['avg_marks']:.1f}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{r['total_points']}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{r['overall_level']}</td>
        </tr>
    """ for i, r in enumerate(top10, start=1))

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Top 10 — {esc(class_title)}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 32px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th {{ text-align:left; padding:8px 12px; background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:12px; text-transform:uppercase; color:#64748b; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #4f46e5;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'])}</h1>
                <p style="margin:2px 0 0;font-size:12px;color:#64748b;">Top 10 Students — {esc(class_title)} ({st['active_cycle']}, {st['active_term']} {st.get('active_year', '')})</p>
            </div>
        </div>
        {"<p class='no-print' style='background:#fffbeb;border:1px solid #fde68a;color:#92400e;padding:10px 14px;border-radius:8px;font-size:12px;margin-top:12px;'>⚠️ No scores found for the <b>" + esc(str(st['active_cycle'])) + "</b> cycle for this stream. This report only looks at whichever exam cycle is currently marked active in Settings (Assessment Phase). If scores were entered under a different cycle (e.g. Opener/Midterm), either switch Assessment Phase to match, or enter scores for the currently active cycle.</p>" if not top10 else ""}
        <table>
            <thead>
                <tr>
                    <th style="text-align:center;">Pos.</th><th>Adm No.</th><th>Full Name</th>
                    <th style="text-align:center;">Total Marks</th><th style="text-align:center;">Avg Marks</th>
                    <th style="text-align:center;">Total Points</th><th style="text-align:center;">Level</th>
                </tr>
            </thead>
            <tbody>{rows_html or "<tr><td colspan='7' style='padding:20px;text-align:center;color:#94a3b8;'>No scores recorded yet for this stream.</td></tr>"}</tbody>
        </table>
    </body>
    </html>
    """


@app.get("/admin/reports/top-subject/{school_id}", response_class=HTMLResponse)
def print_top_student_per_subject(school_id: int, grade_name: str, education_level: str, stream: str, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            st = settings or {'active_term': 'Term 1', 'active_cycle': 'End Term', 'active_year': 2026}

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("""
                SELECT sc.learning_area_id, sc.raw_score, s.id AS student_id, s.admission_number, s.first_name, s.middle_name, s.last_name
                FROM student_scores sc
                JOIN students s ON sc.student_id = s.id
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                  AND sc.cycle_name = %s AND (s.status IS NULL OR s.status != 'GRADUATED');
            """, (school_id, grade_name, education_level, stream, st['active_cycle']))
            all_scores = cur.fetchall()

    best_by_subject = {}
    for row in all_scores:
        lid = row['learning_area_id']
        score = float(row['raw_score'])
        if lid not in best_by_subject or score > best_by_subject[lid]['score']:
            best_by_subject[lid] = {'score': score, 'student': row}

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:64px;height:64px;object-fit:contain;' />"

    class_title = grade_name if stream == "SINGLE STREAM" else f"{grade_name} — Stream {stream}"

    rows_html = ""
    for sub in subjects:
        best = best_by_subject.get(sub['id'])
        if best:
            top_student = best['student']
            pld = evaluate_performance_metrics(best['score'])['pld']
            rows_html += f"""
            <tr>
                <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;font-weight:bold;'>{esc(sub['name'])}</td>
                <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;'>{esc(full_student_name(top_student))}</td>
                <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;font-family:monospace;text-align:center;'>{esc(top_student['admission_number'])}</td>
                <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{best['score']:.0f}%</td>
                <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{pld}</td>
            </tr>
            """
        else:
            rows_html += f"""
            <tr>
                <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;font-weight:bold;'>{esc(sub['name'])}</td>
                <td colspan='4' style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;color:#94a3b8;font-style:italic;'>No scores recorded yet</td>
            </tr>
            """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Top Student Per Subject — {esc(class_title)}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 32px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th {{ text-align:left; padding:8px 12px; background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:12px; text-transform:uppercase; color:#64748b; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #4f46e5;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'])}</h1>
                <p style="margin:2px 0 0;font-size:12px;color:#64748b;">Top Student Per Subject — {esc(class_title)} ({st['active_cycle']}, {st['active_term']} {st.get('active_year', '')})</p>
            </div>
        </div>
        {"<p class='no-print' style='background:#fffbeb;border:1px solid #fde68a;color:#92400e;padding:10px 14px;border-radius:8px;font-size:12px;margin-top:12px;'>⚠️ No scores found for the <b>" + esc(str(st['active_cycle'])) + "</b> cycle for this stream. This report only looks at whichever exam cycle is currently marked active in Settings (Assessment Phase). If scores were entered under a different cycle (e.g. Opener/Midterm), either switch Assessment Phase to match, or enter scores for the currently active cycle.</p>" if not all_scores else ""}
        <table>
            <thead>
                <tr><th>Subject</th><th>Top Student</th><th style="text-align:center;">Adm No.</th><th style="text-align:center;">Score</th><th style="text-align:center;">Level</th></tr>
            </thead>
            <tbody>{rows_html or "<tr><td colspan='5' style='padding:20px;text-align:center;color:#94a3b8;'>No subjects configured for this level.</td></tr>"}</tbody>
        </table>
    </body>
    </html>
    """


@app.get("/admin/reports/marks-supervision/{school_id}", response_class=HTMLResponse)
def marks_entry_supervision(school_id: int, request: Request):
    """Read-only overview for admins: which classes/subjects have marks
    entered for the currently active cycle, and which are still missing —
    lets an admin supervise entry progress across the whole school without
    opening each class's Bulk Entry page individually. Purely a report —
    it only reads existing data, so it can't affect or risk anything."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            active_cycle = settings['active_cycle'] if settings else "End Term"

            cur.execute("""
                SELECT c.grade_name, c.education_level, st.stream,
                       la.id AS subject_id, la.name AS subject_name,
                       COUNT(DISTINCT st.id) AS total_students,
                       COUNT(DISTINCT sc.student_id) AS entered_count
                FROM students st
                JOIN classes c ON st.class_id = c.id
                JOIN learning_areas la ON la.education_level = c.education_level
                LEFT JOIN student_scores sc
                       ON sc.student_id = st.id AND sc.learning_area_id = la.id AND sc.cycle_name = %s
                WHERE st.school_id = %s AND (st.status IS NULL OR st.status != 'GRADUATED')
                GROUP BY c.grade_name, c.education_level, st.stream, la.id, la.name
                ORDER BY c.education_level, c.grade_name, st.stream;
            """, (active_cycle, school_id))
            rows = cur.fetchall()

    # Organize into: education_level -> (grade, stream) -> ordered subject list -> (entered, total)
    levels = {}
    for r in rows:
        level_key = r['education_level']
        class_key = (r['grade_name'], r['stream'])
        levels.setdefault(level_key, {}).setdefault(class_key, {})[r['subject_name']] = (r['entered_count'], r['total_students'])

    level_sections_html = ""
    for level in ["Lower Primary", "Upper Primary", "Junior School"]:
        if level not in levels:
            continue
        class_map = levels[level]
        # Consistent subject column order, matching how report cards order them.
        all_subject_names = sorted({name for subj_map in class_map.values() for name in subj_map.keys()})
        try:
            all_subject_names = sort_subjects_for_display([{"name": n} for n in all_subject_names], level)
            all_subject_names = [s["name"] for s in all_subject_names]
        except Exception:
            pass  # fall back to alphabetical if the sort helper doesn't like a plain list

        header_cells = "".join(f"<th style='padding:8px 6px;text-align:center;font-size:10.5px;'>{esc(name)}</th>" for name in all_subject_names)

        body_rows = ""
        for (grade_name, stream), subj_map in sorted(class_map.items()):
            class_label = grade_name if (not stream or stream == "SINGLE STREAM") else f"{grade_name} — {stream}"
            cells = ""
            for name in all_subject_names:
                entered, total = subj_map.get(name, (0, 0))
                if total == 0:
                    cells += "<td style='text-align:center;padding:6px;color:#cbd5e1;'>-</td>"
                elif entered == total:
                    cells += f"<td style='text-align:center;padding:6px;background:#f0fdf4;color:#166534;font-weight:bold;font-size:11px;'>✅ {entered}/{total}</td>"
                elif entered == 0:
                    cells += f"<td style='text-align:center;padding:6px;background:#fef2f2;color:#991b1b;font-weight:bold;font-size:11px;'>❌ {entered}/{total}</td>"
                else:
                    cells += f"<td style='text-align:center;padding:6px;background:#fffbeb;color:#92400e;font-weight:bold;font-size:11px;'>◐ {entered}/{total}</td>"
            body_rows += f"<tr style='border-bottom:1px solid #f1f5f9;'><td style='padding:8px;font-weight:bold;white-space:nowrap;'>{esc(class_label)}</td>{cells}</tr>"

        level_sections_html += f"""
        <div class="bg-white rounded-2xl border shadow-xs overflow-hidden mb-6">
            <div class="px-5 py-3 border-b bg-slate-50/60">
                <h2 class="text-sm font-bold text-slate-800">{esc(level)}</h2>
            </div>
            <div class="overflow-x-auto">
                <table style="width:100%;border-collapse:collapse;">
                    <thead><tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0;"><th style="padding:8px;text-align:left;font-size:11px;color:#64748b;">Class</th>{header_cells}</tr></thead>
                    <tbody>{body_rows}</tbody>
                </table>
            </div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Marks Entry Supervision</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">🔍 Marks Entry Supervision — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Showing entry status for the currently active cycle: <b>{esc(active_cycle)}</b>. ✅ complete · ◐ partial · ❌ not started.</p>
            </div>
            <a href="/admin/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back</a>
        </header>
        <div class="p-4 sm:p-8 max-w-6xl mx-auto">
            {level_sections_html or "<p class='text-slate-400 text-sm italic text-center py-16 bg-white border border-dashed rounded-2xl'>No students registered yet.</p>"}
        </div>
    </body>
    </html>
    """


@app.get("/admin/audit-log/{school_id}", response_class=HTMLResponse)
def view_audit_log(school_id: int, request: Request):
    """Read-only view of recent actions taken on this school's account —
    who registered a student, who changed settings, who deactivated a staff
    member, etc. Purely a report; it only reads existing log entries."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("""
                SELECT action, actor_label, details, created_at FROM audit_log
                WHERE school_id = %s
                ORDER BY created_at DESC
                LIMIT 200;
            """, (school_id,))
            entries = cur.fetchall()

    action_labels = {
        "student_added": ("👤 Student Added", "text-emerald-700"),
        "student_edited": ("✏️ Student Edited", "text-indigo-700"),
        "student_deleted": ("🗑 Student Deleted", "text-rose-700"),
        "staff_added": ("🧑‍🏫 Staff Added", "text-emerald-700"),
        "staff_activated": ("✅ Staff Activated", "text-emerald-700"),
        "staff_deactivated": ("⛔ Staff Deactivated", "text-amber-700"),
        "staff_deleted": ("🗑 Staff Deleted", "text-rose-700"),
        "settings_updated": ("⚙️ Settings Updated", "text-slate-700"),
        "marks_saved": ("📝 Marks Saved", "text-indigo-700"),
    }

    rows_html = ""
    for e in entries:
        label, color = action_labels.get(e['action'], (e['action'], "text-slate-700"))
        rows_html += f"""
        <div class="flex items-start justify-between gap-3 py-2.5 border-b border-slate-50 last:border-0">
            <div class="min-w-0">
                <p class="text-xs font-bold {color}">{esc(label)}</p>
                <p class="text-xs text-slate-500 mt-0.5">{esc(e['details'] or '')}</p>
                <p class="text-[11px] text-slate-400 mt-0.5">by {esc(e['actor_label'] or 'Unknown')}</p>
            </div>
            <span class="text-[11px] text-slate-400 whitespace-nowrap">{e['created_at'].strftime('%d %b %Y, %H:%M') if e['created_at'] else ''}</span>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Activity Log</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📋 Activity Log — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">The last 200 actions taken on this school's account.</p>
            </div>
            <a href="/admin/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto">
            <div class="bg-white rounded-2xl border shadow-xs p-5">
                {rows_html or "<p class='text-slate-400 text-xs italic text-center py-8'>No activity recorded yet.</p>"}
            </div>
        </div>
    </body>
    </html>
    """


@app.get("/admin/reports/subject-analysis/{school_id}", response_class=HTMLResponse)
def print_subject_analysis(school_id: int, grade_name: str, education_level: str, stream: str, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            st = settings or {'active_term': 'Term 1', 'active_cycle': 'End Term'}

            cur.execute("""
                SELECT s.id
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                  AND (%s = 'SINGLE STREAM' OR s.stream = %s);
            """, (school_id, grade_name, education_level, stream, stream))
            student_ids = [row['id'] for row in cur.fetchall()]

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (education_level,))
            subjects = cur.fetchall()

            score_map = {}
            if student_ids:
                cur.execute("""
                    SELECT student_id, learning_area_id, cycle_name, raw_score
                    FROM student_scores
                    WHERE student_id = ANY(%s);
                """, (student_ids,))
                for row in cur.fetchall():
                    score_map.setdefault(row['student_id'], {}).setdefault(row['learning_area_id'], {})[row['cycle_name']] = float(row['raw_score'])

    subject_stats = []
    for sub in subjects:
        subject_means = []
        level_counts = {'EE': 0, 'ME': 0, 'AE': 0, 'BE': 0}
        for sid in student_ids:
            cycles = score_map.get(sid, {}).get(sub['id'], {})
            if cycles:
                m = sum(cycles.values()) / len(cycles)
                subject_means.append(m)
                pld = evaluate_performance_metrics(m)['pld']
                bucket = pld[:2]
                if bucket in level_counts:
                    level_counts[bucket] += 1
        subject_mean = sum(subject_means) / len(subject_means) if subject_means else 0
        subject_stats.append({
            'name': sub['name'],
            'mean': subject_mean,
            'entries': len(subject_means),
            'levels': level_counts,
        })

    subject_stats.sort(key=lambda x: x['mean'], reverse=True)

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:64px;height:64px;object-fit:contain;' />"

    rows_html = "".join(
        f"""<tr>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;font-weight:bold;'>{esc(sub['name'])}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;font-weight:bold;'>{sub['mean']:.1f}%</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{evaluate_performance_metrics(sub['mean'])['pld'] if sub['entries'] else '-'}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{sub['levels']['EE']}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{sub['levels']['ME']}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{sub['levels']['AE']}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{sub['levels']['BE']}</td>
            <td style='padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:center;'>{sub['entries']}/{len(student_ids)}</td>
        </tr>"""
        for sub in subject_stats
    )

    class_title = grade_name if stream == "SINGLE STREAM" else f"{grade_name} — {stream}"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
        <title>Elimu Hub | Subject Analysis — {esc(class_title)}</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 32px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th {{ text-align:left; padding:8px 12px; background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:12px; text-transform:uppercase; color:#64748b; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#0d9488;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #0d9488;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'])}</h1>
                <p style="margin:2px 0 0;font-size:12px;color:#64748b;">{esc(class_title)} — Subject Analysis ({st['active_term']} • {st['active_cycle']}) — {len(student_ids)} students</p>
            </div>
        </div>
        <table>
            <thead><tr>
                <th>Subject</th><th style="text-align:center;">Mean Score</th><th style="text-align:center;">Level</th>
                <th style="text-align:center;">EE</th><th style="text-align:center;">ME</th><th style="text-align:center;">AE</th><th style="text-align:center;">BE</th>
                <th style="text-align:center;">Entries</th>
            </tr></thead>
            <tbody>{rows_html or "<tr><td colspan='8' style='padding:20px;text-align:center;color:#94a3b8;'>No subjects or scores found for this class.</td></tr>"}</tbody>
        </table>
        <p style="font-size:10px;color:#94a3b8;margin-top:12px;">EE = Exceeding Expectations · ME = Meeting Expectations · AE = Approaching Expectations · BE = Below Expectations. Counts reflect students with at least one score recorded for that subject.</p>
    </body>
    </html>
    """

# --- GET View Routes for Administration Subsystems ---
@app.get("/admin/student/new/{school_id}", response_class=HTMLResponse)
def add_student_view(school_id: int, request: Request):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT is_single_stream FROM school_settings WHERE school_id = %s;", (school_id,))
            settings_row = cur.fetchone()
            is_single_stream = bool(settings_row['is_single_stream']) if settings_row else False

    stream_field_html = (
        "<div class='sm:col-span-2 bg-slate-50 border border-slate-200 rounded p-2.5 text-xs text-slate-500'>ℹ️ This school is in <b>Single Stream Mode</b> — no stream assignment is needed.</div>"
        if is_single_stream else
        "<div><label class=\"text-xs font-bold text-slate-600\">Class Stream Assignment</label><input type=\"text\" name=\"stream\" placeholder=\"e.g. N\" class=\"w-full border p-2.5 rounded mt-1 text-base\" required></div>"
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Add New Student Record</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 flex items-center justify-center min-h-screen p-4">
        <div class="bg-white p-6 sm:p-8 rounded-2xl border shadow-md w-full max-w-lg">
            <h2 class="text-xl font-bold mb-4 text-slate-800">Add New Learner Profile</h2>
            <form action="/api/v1/students/add/{school_id}" method="post" class="space-y-4" onsubmit="var b=this.querySelector('button[type=submit]'); if(b){{b.disabled=true; b.textContent='Saving...'; b.style.opacity='0.7';}}">
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div><label class="text-xs font-bold text-slate-600">First Name</label><input type="text" name="first_name" class="w-full border p-2.5 rounded mt-1 text-base" required></div>
                    <div><label class="text-xs font-bold text-slate-600">Middle Name</label><input type="text" name="middle_name" class="w-full border p-2.5 rounded mt-1 text-base"></div>
                    <div><label class="text-xs font-bold text-slate-600">Surname</label><input type="text" name="last_name" class="w-full border p-2.5 rounded mt-1 text-base" required></div>
                </div>
                <div><label class="text-xs font-bold text-slate-600">Admission Number</label><input type="text" inputmode="numeric" name="admission_number" class="w-full border p-2.5 rounded mt-1 text-base" required></div>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs font-bold text-slate-600">Education Track Segment</label>
                        <select name="class_id" class="w-full border p-2.5 rounded mt-1 bg-white text-sm font-medium text-slate-800" required>
                            <option value="" disabled selected>Select Grade...</option>
                            <option value="1">Grade 1</option>
                            <option value="2">Grade 2</option>
                            <option value="3">Grade 3</option>
                            <option value="4">Grade 4</option>
                            <option value="5">Grade 5</option>
                            <option value="6">Grade 6</option>
                            <option value="7">Grade 7</option>
                            <option value="8">Grade 8</option>
                            <option value="9">Grade 9</option>
                        </select>
                    </div>
                    {stream_field_html}
                </div>
                <div class="flex flex-col sm:flex-row gap-3 pt-2">
                    <button type="submit" class="bg-emerald-700 text-white font-bold py-3 px-4 rounded hover:bg-emerald-800 transition text-center">Save Student</button>
                    <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 text-slate-700 py-3 px-4 rounded hover:bg-slate-300 font-bold transition text-center">Cancel</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    """

@app.get("/staff/register-panel/{school_id}", response_class=HTMLResponse)
def staff_registration_panel(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Add Staff — {esc(school['name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-indigo-950 flex items-center justify-center min-h-screen font-sans p-6">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-sm border-t-8 border-indigo-700">
            <h2 class="text-xl font-black text-slate-800 mb-1">Add Staff Member</h2>
            <p class="text-xs text-slate-400 mb-6">{esc(school['name'])}</p>
            <form action="/api/v1/staff/add/{school_id}" method="post" class="space-y-4" onsubmit="var b=this.querySelector('button[type=submit]'); if(b){{b.disabled=true; b.textContent='Saving...'; b.style.opacity='0.7';}}">
                <div>
                    <label class="text-xs font-bold uppercase tracking-wider text-slate-600">Full Name</label>
                    <input type="text" name="full_name" class="w-full border border-slate-200 p-2.5 rounded-lg text-sm mt-1" required>
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="text-xs font-bold uppercase tracking-wider text-slate-600">TSC No.</label>
                        <input type="text" name="tsc_number" class="w-full border border-slate-200 p-2.5 rounded-lg text-sm mt-1" required>
                    </div>
                    <div>
                        <label class="text-xs font-bold uppercase tracking-wider text-slate-600">Phone Number</label>
                        <input type="tel" name="phone_number" placeholder="07XXXXXXXX" class="w-full border border-slate-200 p-2.5 rounded-lg text-sm mt-1" required>
                    </div>
                </div>
                <div>
                    <label class="text-xs font-bold uppercase tracking-wider text-slate-600">Staff Email Address</label>
                    <input type="email" name="email" class="w-full border border-slate-200 p-2.5 rounded-lg text-sm mt-1" required>
                </div>
                <div>
                    <label class="text-xs font-bold uppercase tracking-wider text-slate-600">Initial Password</label>
                    <input type="password" name="password" minlength="6" class="w-full border border-slate-200 p-2.5 rounded-lg text-sm mt-1" required>
                    <p class="text-[10px] text-slate-400 mt-1">At least 6 characters. They can change it after logging in.</p>
                </div>
                <div class="flex items-center justify-between pt-2">
                    <a href="/admin/dashboard/{school_id}" class="text-slate-500 font-bold hover:underline text-xs">← Back to Dashboard</a>
                    <button type="submit" class="bg-indigo-700 text-white px-6 py-3 rounded-lg font-black tracking-wide hover:bg-indigo-800 transition shadow-md text-xs">Create Account</button>
                </div>
            </form>
        </div>
    </body>
    </html>
    """
@app.post("/api/v1/staff/toggle-status/{staff_id}/{school_id}")
def toggle_staff_active_status(staff_id: int, school_id: int, request: Request):
    """Safely disables or enables a staff account without wiping historical records."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT full_name, email, is_verified FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (staff_id, school_id))
            staff_row = cur.fetchone()

            # Flips the current boolean value of is_verified (acting as our active flag)
            cur.execute("""
                UPDATE users 
                SET is_verified = NOT is_verified 
                WHERE id = %s AND school_id = %s AND role = 'staff';
            """, (staff_id, school_id))
            conn.commit()

            if staff_row:
                new_state = "deactivated" if staff_row['is_verified'] else "activated"
                log_audit_action(cur, request, school_id, f"staff_{new_state}", f"{new_state.capitalize()} {staff_row['full_name'] or staff_row['email']}")
                conn.commit()
            
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)


@app.post("/api/v1/staff/delete/{staff_id}/{school_id}")
def delete_staff_permanently(staff_id: int, school_id: int, request: Request):
    """Hard deletes a staff record. Use cautiously."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT full_name, email FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (staff_id, school_id))
            staff_row = cur.fetchone()

            cur.execute("DELETE FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (staff_id, school_id))
            conn.commit()

            if staff_row:
                log_audit_action(cur, request, school_id, "staff_deleted", f"Deleted staff account: {staff_row[0] or staff_row[1]}")
                conn.commit()
            
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.get("/admin/scores/manage/{school_id}", response_class=HTMLResponse)
def manage_individual_scores_view(school_id: int, student_id: int, request: Request):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student target missing.")
            
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (student['education_level'],))
            subjects = cur.fetchall()
            
            cur.execute("""
                SELECT ss.id as score_id, la.name as subject_name, ss.cycle_name, ss.raw_score 
                FROM student_scores ss
                JOIN learning_areas la ON ss.learning_area_id = la.id
                WHERE ss.student_id = %s;
            """, (student_id,))
            existing_scores = cur.fetchall()

    subject_options = "".join([f"<option value='{s['id']}'>{s['name']}</option>" for s in subjects])
    score_rows = "".join([f"""
        <tr class='border-b text-xs'>
            <td class='p-2 font-bold'>{s['subject_name']}</td>
            <td class='p-2'>{s['cycle_name']}</td>
            <td class='p-2 font-black text-emerald-800'>{float(s['raw_score'])}%</td>
            <td class='p-2 flex gap-2'>
                <form action='/api/v1/scores/delete/{school_id}' method='post' onsubmit="return confirm('Drop this evaluation parameter entirely?');">
                    <input type='hidden' name='score_id' value='{s['score_id']}'>
                    <input type='hidden' name='student_id' value='{student_id}'>
                    <button type='submit' class='text-red-600 font-bold hover:underline'>Drop Score</button>
                </form>
            </td>
        </tr>
    """ for s in existing_scores])

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Edit Matrix for {esc(student['first_name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-50 p-8 min-h-screen max-w-3xl mx-auto space-y-6">
        <div class="bg-white p-6 rounded-2xl border shadow-xs flex justify-between items-center">
            <div>
                <h1 class="text-xl font-black">Score Management Engine Matrix</h1>
                <p class="text-xs text-slate-500 mt-1">Student context: <strong>{esc(full_student_name(student))} ({esc(student['admission_number'])})</strong></p>
            </div>
            <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 px-4 py-1.5 rounded-lg text-xs font-bold hover:bg-slate-300">Return Deck</a>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3">
                <h3 class="font-bold border-b pb-2 text-sm text-slate-700">✏️ Commit/Update Specific Assessment Area</h3>
                <form action="/api/v1/scores/upsert/{school_id}" method="post" class="space-y-3 text-xs">
                    <input type="hidden" name="student_id" value="{student_id}">
                    <div>
                        <label class="font-bold block text-slate-500">Learning Area</label>
                        <select name="learning_area_id" class="w-full border p-2 rounded mt-1">{subject_options}</select>
                    </div>
                    <div>
                        <label class="font-bold block text-slate-500">Phase Cycle</label>
                        <select name="cycle_name" class="w-full border p-2 rounded mt-1">
                            <option value="Opener">Opener</option>
                            <option value="Midterm">Midterm</option>
                            <option value="End Term">End Term</option>
                        </select>
                    </div>
                    <div>
                        <label class="font-bold block text-slate-500">Raw Mark Value (0 - 100%)</label>
                        <input type="number" step="0.01" min="0" max="100" name="raw_score" class="w-full border p-2 rounded mt-1" required>
                    </div>
                    <button type="submit" class="bg-slate-900 text-white py-2 px-4 rounded font-bold hover:bg-black transition">Commit Performance Mark</button>
                </form>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <h3 class="font-bold p-4 bg-slate-50 border-b text-sm text-slate-700">📋 Logged Scores for Student</h3>
                <table class="w-full text-left">
                    <thead>
                        <tr class="bg-slate-100 text-[10px] uppercase font-bold text-slate-500 border-b"><th class="p-2">Area</th><th class="p-2">Cycle</th><th class="p-2">Score</th><th class="p-2">Action</th></tr>
                    </thead>
                    <tbody>{score_rows or "<tr><td colspan='4' class='text-center p-4 text-xs italic text-slate-400'>No values captured.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
# --- 1. FIXED BULK SHEET ENTRY WORKSPACE ---
@app.get("/staff/bulk-entry/{school_id}", response_class=HTMLResponse)
def educators_bulk_entry_grid(
    school_id: int, 
    request: Request,
    grade_name: str, 
    stream: str, 
    education_level: str, 
    learning_area_id: int = None, 
    cycle_name: str = "End Term",
    saved: int = None,
    skipped: int = None,
):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            
            # Fetch relevant subjects matched by the educational segment level
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (education_level,))
            subjects = cur.fetchall()
            
            selected_area_id = learning_area_id or (subjects[0]['id'] if subjects else None)
            selected_subject_name = next((sub['name'] for sub in subjects if sub['id'] == selected_area_id), "")
            is_paper_mode = is_paper_based_subject(selected_subject_name, education_level)

            # FIXED: Added explicit JOIN onto classes table to resolve missing 'education_level' column runtime issue
            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.middle_name, s.last_name 
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s 
                  AND c.grade_name = %s 
                  AND s.stream = %s 
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (school_id, grade_name, stream))
            students = cur.fetchall()

            score_map = {}
            paper_map = {}
            paper1_max, paper2_max = 30, 50
            if selected_area_id and is_paper_mode:
                cur.execute("""
                    SELECT student_id, paper1_marks, paper1_max, paper2_marks, paper2_max FROM paper_based_scores
                    WHERE learning_area_id = %s AND cycle_name = %s;
                """, (selected_area_id, cycle_name))
                rows = cur.fetchall()
                for r in rows:
                    paper_map[r['student_id']] = r
                # Reuse whatever "out of" was last configured for this
                # subject/cycle — scan every row rather than trusting just
                # the first one, since a student scored on only one paper
                # has the other paper's max stored as NULL on their row.
                first_p1_max = next((r['paper1_max'] for r in rows if r['paper1_max'] is not None), None)
                first_p2_max = next((r['paper2_max'] for r in rows if r['paper2_max'] is not None), None)
                if first_p1_max is not None:
                    paper1_max = float(first_p1_max)
                if first_p2_max is not None:
                    paper2_max = float(first_p2_max)
            elif selected_area_id:
                cur.execute("""
                    SELECT student_id, raw_score FROM student_scores 
                    WHERE learning_area_id = %s AND cycle_name = %s;
                """, (selected_area_id, cycle_name))
                for scr in cur.fetchall():
                    score_map[scr['student_id']] = float(scr['raw_score'])

    subject_options = "".join([f"<option value='{sub['id']}' {'selected' if sub['id'] == selected_area_id else ''}>{sub['name']}</option>" for sub in subjects])

    student_rows = ""
    for s in students:
        search_key = f"{full_student_name(s)} {s['admission_number']}".lower()
        if is_paper_mode:
            p = paper_map.get(s['id'])
            p1_val = p['paper1_marks'] if (p and p['paper1_marks'] is not None) else ""
            p2_val = p['paper2_marks'] if (p and p['paper2_marks'] is not None) else ""
            input_html = f"""
            <div class="flex items-center gap-2 shrink-0">
                <input type="number" inputmode="decimal" step="0.01" min="0" name="paper1_{s['id']}" value="{p1_val}" class="border-2 p-2 rounded-xl w-16 focus:border-emerald-600 font-bold text-center text-sm" placeholder="P1">
                <span class="text-slate-300 text-xs">/</span>
                <input type="number" inputmode="decimal" step="0.01" min="0" name="paper2_{s['id']}" value="{p2_val}" class="border-2 p-2 rounded-xl w-16 focus:border-emerald-600 font-bold text-center text-sm" placeholder="P2">
            </div>
            """
        else:
            existing_val = score_map.get(s['id'], "")
            input_html = f'<input type="number" inputmode="decimal" step="0.01" min="0" max="100" name="score_{s["id"]}" value="{existing_val}" class="border-2 p-2.5 rounded-xl w-24 shrink-0 focus:border-emerald-600 font-bold text-center text-base" placeholder="-%">'

        student_rows += f"""
        <div class="student-row flex items-center justify-between gap-3 p-3.5 border-b last:border-0" data-search="{esc(search_key)}">
            <div class="min-w-0">
                <p class="font-bold text-slate-800 text-sm truncate">{esc(full_student_name(s))}</p>
                <p class="text-xs text-slate-400 font-mono">#{esc(s['admission_number'])}</p>
            </div>
            {input_html}
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Bulk Sheet Entry Deck</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 p-3 sm:p-8 min-h-screen max-w-4xl mx-auto space-y-4 sm:space-y-6">
        <div class="bg-white p-4 sm:p-6 rounded-2xl border shadow-xs flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3">
            <div>
                <h1 class="text-lg sm:text-xl font-black text-slate-900">⚡ Bulk Marks Management Interface</h1>
                <p class="text-xs text-slate-500 mt-1">Cohort Segment target: <strong>{esc(grade_name)} — {esc(education_level)} (Stream {esc(stream)})</strong></p>
            </div>
            <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 px-4 py-2.5 rounded-lg text-xs font-black hover:bg-slate-300 text-center">Exit Workspace</a>
        </div>

        {f'<div class="bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl">✅ {saved} score{"s" if saved != 1 else ""} saved successfully.' + (f' <span class="text-amber-700">({skipped} entr{"ies" if skipped != 1 else "y"} skipped — check for out-of-range or invalid values.)</span>' if skipped else '') + '</div>' if saved is not None else ''}

        <div class="bg-white p-4 sm:p-6 rounded-2xl border shadow-xs">
            <form method="get" action="/staff/bulk-entry/{school_id}" class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                <div>
                    <label class="font-bold text-slate-500">Target Learning Subject</label>
                    <select name="learning_area_id" onchange="this.form.submit()" class="w-full border p-3 rounded-xl mt-1 font-semibold text-sm">{subject_options}</select>
                </div>
                <div>
                    <label class="font-bold text-slate-500">Evaluation Phase</label>
                    <select name="cycle_name" onchange="this.form.submit()" class="w-full border p-3 rounded-xl mt-1 font-semibold text-sm">
                        <option value="Opener" {"selected" if cycle_name == 'Opener' else ""}>Opener Phase</option>
                        <option value="Midterm" {"selected" if cycle_name == 'Midterm' else ""}>Midterm Cycle</option>
                        <option value="End Term" {"selected" if cycle_name == 'End Term' else ""}>End Term Synthesis</option>
                    </select>
                </div>
                <div class="hidden sm:flex items-end text-slate-400 text-[11px] italic pb-2">Changing dropdown values auto-updates student listing map.</div>
            </form>
        </div>

        <form action="/api/v1/scores/bulk-save/{school_id}" method="post" class="bg-white rounded-2xl border shadow-xs overflow-hidden" onsubmit="var b=this.querySelector('button[type=submit]'); if(b){{b.disabled=true; b.textContent='Saving...'; b.style.opacity='0.7';}}">
            <input type="hidden" name="grade_name" value="{esc(grade_name)}">
            <input type="hidden" name="education_level" value="{esc(education_level)}">
            <input type="hidden" name="stream" value="{esc(stream)}">
            <input type="hidden" name="learning_area_id" value="{selected_area_id}">
            <input type="hidden" name="cycle_name" value="{cycle_name}">
            <input type="hidden" name="is_paper_mode" value="{'1' if is_paper_mode else '0'}">

            {f'''<div class="p-3.5 bg-indigo-50 border-b border-indigo-100 flex flex-wrap items-center gap-3 text-xs">
                <span class="font-bold text-indigo-800">📄 {esc(selected_subject_name)} is assessed as two papers —</span>
                <label class="flex items-center gap-1.5 font-semibold text-indigo-700">Paper 1 out of
                    <input type="number" name="paper1_max" value="{paper1_max:.0f}" min="1" class="border-2 border-indigo-200 p-1.5 rounded-lg w-16 text-center font-bold">
                </label>
                <label class="flex items-center gap-1.5 font-semibold text-indigo-700">Paper 2 out of
                    <input type="number" name="paper2_max" value="{paper2_max:.0f}" min="1" class="border-2 border-indigo-200 p-1.5 rounded-lg w-16 text-center font-bold">
                </label>
                <span class="text-indigo-400 italic">The two papers are combined into one percentage automatically on save.</span>
            </div>''' if is_paper_mode else ""}

            <div class="p-3 border-b bg-white">
                <input type="text" id="studentSearchBox" oninput="filterStudentRows(this.value)" placeholder="🔎 Search by name or admission number..." class="w-full border-2 p-2.5 rounded-xl text-sm focus:border-emerald-600" autocomplete="off">
            </div>
            <div class="px-3.5 py-2.5 bg-slate-50 text-slate-500 text-[10px] font-bold uppercase tracking-wider border-b flex justify-between">
                <span>Learner</span><span>{"Paper 1 / Paper 2" if is_paper_mode else "Score %"}</span>
            </div>
            <div id="studentRowsContainer">{student_rows or "<p class='text-center p-6 text-slate-400 italic text-xs'>No registered class matching criterion.</p>"}</div>
            <p id="noSearchResults" class="hidden text-center p-6 text-slate-400 italic text-xs">No learners match that search.</p>

            {f'<div class="p-4 bg-slate-50 border-t"><button type="submit" class="w-full bg-[#046A38] hover:bg-emerald-900 text-white font-bold py-3.5 px-6 rounded-xl text-sm shadow-md">Batch Commit Class Sheet</button></div>' if students else ""}
        </form>

        <script>
            function filterStudentRows(query) {{
                const q = query.trim().toLowerCase();
                const rows = document.querySelectorAll('#studentRowsContainer .student-row');
                let visibleCount = 0;
                rows.forEach(function(row) {{
                    const match = row.getAttribute('data-search').includes(q);
                    row.style.display = match ? '' : 'none';
                    if (match) visibleCount++;
                }});
                document.getElementById('noSearchResults').classList.toggle('hidden', visibleCount !== 0 || rows.length === 0);
            }}
        </script>
    </body>
    </html>
    """


@app.get("/api/v1/reports/bulk-print/{school_id}", response_class=HTMLResponse)
def output_batch_class_report_forms(school_id: int, grade_name: str, education_level: str, stream: str):
    # Utilizing connection context manager/pool cleanly
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Look up institutional profiles dynamically
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            
            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            
            st = settings or {'active_year': 2026, 'active_term': 'Term 1', 'active_cycle': 'End Term', 'opening_date': 'TBD', 'closing_date': 'TBD'}
            theme = fetch_theme_styles(school.get('theme_color', 'emerald') if school else 'emerald')

            if not school:
                raise HTTPException(status_code=404, detail="Institution Tenant context missing.")

            # 🌟 Fixed Subject Average Aggregation to align perfectly with report card loop calculations
            cur.execute("""
                WITH subject_averages AS (
                    SELECT 
                        sc.student_id,
                        sc.learning_area_id,
                        AVG(sc.raw_score) AS subject_avg
                    FROM student_scores sc
                    WHERE sc.cycle_name IN ('Opener', 'Midterm', 'End Term')
                    GROUP BY sc.student_id, sc.learning_area_id
                ),
                student_mean_scores AS (
                    SELECT 
                        s.id AS student_id,
                        s.admission_number,
                        s.first_name,
                        s.middle_name,
                        s.last_name,
                        s.knec_lan,
                        s.stream,
                        c.grade_name,
                        COALESCE(AVG(sa.subject_avg), 0) AS final_calculated_mean,
                        COALESCE(SUM(
                            CASE
                                WHEN sa.subject_avg >= 90 THEN 8
                                WHEN sa.subject_avg >= 76 THEN 7
                                WHEN sa.subject_avg >= 60 THEN 6
                                WHEN sa.subject_avg >= 50 THEN 5
                                WHEN sa.subject_avg >= 40 THEN 4
                                WHEN sa.subject_avg >= 30 THEN 3
                                WHEN sa.subject_avg >= 20 THEN 2
                                WHEN sa.subject_avg >= 0 THEN 1
                                ELSE 0
                            END
                        ), 0) AS total_points
                    FROM students s
                    JOIN classes c ON s.class_id = c.id
                    LEFT JOIN subject_averages sa ON s.id = sa.student_id
                    WHERE s.school_id = %s 
                      AND c.grade_name = %s
                      AND (s.status IS NULL OR s.status != 'GRADUATED')
                    GROUP BY s.id, s.admission_number, s.first_name, s.middle_name, s.last_name, s.knec_lan, s.stream, c.grade_name
                ),
                cohort_rankings AS (
                    SELECT 
                        *,
                        RANK() OVER (
                            PARTITION BY grade_name, stream 
                            ORDER BY total_points DESC
                        ) AS stream_position,
                        COUNT(*) OVER (
                            PARTITION BY grade_name, stream
                        ) AS total_in_stream,
                        
                        RANK() OVER (
                            PARTITION BY grade_name 
                            ORDER BY total_points DESC
                        ) AS grade_position,
                        COUNT(*) OVER (
                            PARTITION BY grade_name
                        ) AS total_in_grade
                    FROM student_mean_scores
                )
                SELECT * FROM cohort_rankings
                WHERE stream = %s
                ORDER BY stream_position ASC, admission_number ASC;
            """, (school_id, grade_name, stream))
            students = cur.fetchall()
            
            if not students:
                return f"""
                <div style="font-family:'Plus Jakarta Sans',Arial,sans-serif; text-align:center; padding:80px 20px; background:#F7F9F8; min-height:100vh;">
                    <p style="font-size:15px; color:#475569; margin-bottom:20px;">No students are registered in this class yet, so there's nothing to generate report cards for.</p>
                    <a href="/admin/dashboard/{school_id}" style="background:#1e1b4b; color:white; padding:12px 24px; border-radius:10px; font-weight:bold; text-decoration:none; font-size:13px;">← Back to Dashboard</a>
                </div>
                """

            # Only show a column for an exam cycle if it's actually been keyed
            # in anywhere for this batch — e.g. if only End Term has been
            # entered so far, the report shows just that one column instead
            # of two empty ones for Opener/Midterm.
            student_ids_in_batch = [s['student_id'] for s in students]
            cur.execute("""
                SELECT DISTINCT cycle_name FROM student_scores
                WHERE student_id = ANY(%s) AND cycle_name IN ('Opener', 'Midterm', 'End Term');
            """, (student_ids_in_batch,))
            cycles_with_data = {r['cycle_name'] for r in cur.fetchall()}
            show_opener = 'Opener' in cycles_with_data
            show_midterm = 'Midterm' in cycles_with_data
            show_endterm = 'End Term' in cycles_with_data
            # Safety net: if somehow nothing has been entered anywhere yet,
            # still show all three so the report isn't a table with zero
            # exam columns at all.
            if not (show_opener or show_midterm or show_endterm):
                show_opener = show_midterm = show_endterm = True

            # 2. Extract curriculum guidelines dynamically based on structural segment parameters
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (education_level,))
            subjects = cur.fetchall()

            # The subject table is the one part of this report whose height
            # scales with the school's own data (4 subjects for Lower Primary
            # vs 9 for Junior School) — a single fixed row padding that looks
            # good and fits on one page for 4 subjects will push a 9-subject
            # report past the bottom of the page. Scale row padding down as
            # subject count goes up so every education level reliably fits
            # on exactly one page.
            _n_subjects = len(subjects)
            if _n_subjects <= 5:
                row_vpad, row_font, desc_font = "16px", "12.5px", "11.5px"
            elif _n_subjects <= 7:
                row_vpad, row_font, desc_font = "11px", "11.5px", "11px"
            else:
                row_vpad, row_font, desc_font = "8px", "11px", "10.5px"

            report_cards_html = []

            # Fetch every student's scores in this batch in a single query,
            # instead of one query per student — for a class of 35, that's
            # 1 database round-trip instead of 35 held sequentially on the
            # same connection, which matters a lot once several staff are
            # printing report cards for different classes at the same time.
            all_scores_by_student = {}
            if student_ids_in_batch:
                cur.execute(
                    "SELECT student_id, learning_area_id, cycle_name, raw_score FROM student_scores WHERE student_id = ANY(%s);",
                    (student_ids_in_batch,)
                )
                for sc in cur.fetchall():
                    all_scores_by_student.setdefault(sc['student_id'], []).append(sc)

            # A non-blocking alert (not a hard stop) listing any student who
            # has zero marks entered for one or more subjects this term —
            # so whoever's printing notices before handing out incomplete
            # report cards, rather than after.
            missing_marks_by_student = {}
            for s in students:
                subjects_with_scores = {sc['learning_area_id'] for sc in all_scores_by_student.get(s['student_id'], [])}
                missing_subject_names = [sub['name'] for sub in subjects if sub['id'] not in subjects_with_scores]
                if missing_subject_names:
                    missing_marks_by_student[s['student_id']] = {
                        'name': full_student_name(s),
                        'admission_number': s['admission_number'],
                        'missing_subjects': missing_subject_names,
                    }

            missing_marks_banner = ""
            if missing_marks_by_student:
                missing_rows = "".join(
                    f"<li><b>{esc(info['name'])}</b> (#{esc(info['admission_number'])}) — missing: {esc(', '.join(info['missing_subjects']))}</li>"
                    for info in missing_marks_by_student.values()
                )
                missing_marks_banner = f"""
                <div class="no-print" style="max-width:223mm; margin:0 auto 16px auto; background:#fffbeb; border:1px solid #fde68a; color:#92400e; padding:14px 18px; border-radius:10px; font-family:'Plus Jakarta Sans',Arial,sans-serif;">
                    <p style="margin:0 0 8px; font-weight:800; font-size:13px;">⚠️ {len(missing_marks_by_student)} student(s) in this class are missing marks for one or more subjects:</p>
                    <ul style="margin:0; padding-left:18px; font-size:12px; line-height:1.6;">{missing_rows}</ul>
                    <p style="margin:8px 0 0; font-size:11px; color:#78350f;">This is just a heads-up — report cards below still print normally, using whichever marks are actually entered.</p>
                </div>
                """

            for s in students:
                scores = all_scores_by_student.get(s['student_id'], [])
                
                score_map = {}
                for sc in scores:
                    if sc['learning_area_id'] not in score_map:
                        score_map[sc['learning_area_id']] = {}
                    score_map[sc['learning_area_id']][sc['cycle_name']] = float(sc['raw_score'])

                rows_markup = ""
                total_evaluated_weight = 0
                total_subjects_count = 0
                accumulated_scale_points = 0
                
                # Performance tracking metrics across assessment milestones
                opener_sum, midterm_sum, endterm_sum = 0, 0, 0
                op_count, mid_count, end_count = 0, 0, 0

                for sub in subjects:
                    op = score_map.get(sub['id'], {}).get('Opener')
                    mid = score_map.get(sub['id'], {}).get('Midterm')
                    end = score_map.get(sub['id'], {}).get('End Term')

                    if op is not None:
                        opener_sum += op; op_count += 1
                    if mid is not None:
                        midterm_sum += mid; mid_count += 1
                    if end is not None:
                        endterm_sum += end; end_count += 1

                    active_cycles = [v for v in [op, mid, end] if v is not None]
                    if active_cycles:
                        weighted_total = sum(active_cycles) / len(active_cycles)
                        meta = evaluate_performance_metrics(weighted_total)
                        pld, pts, descriptor = meta['pld'], f"{meta['points']} Pt", meta['desc']
                        
                        total_evaluated_weight += weighted_total
                        total_subjects_count += 1
                        accumulated_scale_points += meta['points']
                    else:
                        pld, pts, descriptor = "-", "-", "-"

                    op_str = f"{op:.1f}%" if op is not None else "0%"
                    mid_str = f"{mid:.1f}%" if mid is not None else "0%"
                    end_str = f"{end:.1f}%" if end is not None else "0%"
                    weighted_str = f"{weighted_total:.1f}%" if active_cycles else "0%"

                    exam_body_cells = (
                        (f'<td style="padding: {row_vpad} 6px; border: 1px solid #222; text-align:center; font-size:{row_font};">{op_str}</td>' if show_opener else "") +
                        (f'<td style="padding: {row_vpad} 6px; border: 1px solid #222; text-align:center; font-size:{row_font};">{mid_str}</td>' if show_midterm else "") +
                        (f'<td style="padding: {row_vpad} 6px; border: 1px solid #222; text-align:center; font-size:{row_font};">{end_str}</td>' if show_endterm else "")
                    )

                    rows_markup += f"""
                    <tr>
                        <td style="padding: {row_vpad} 6px; border: 1px solid #222; font-weight:bold; font-size:{row_font};">{sub['name']}</td>
                        {exam_body_cells}
                        <td style="padding: {row_vpad} 6px; border: 1px solid #222; text-align:center; background:#f9f9f9; font-weight:bold; font-size:{row_font};">{weighted_str}</td>
                        <td style="padding: {row_vpad} 6px; border: 1px solid #222; text-align:center; font-weight:bold; font-size:{row_font};">{pld}</td>
                        <td style="padding: {row_vpad} 6px; border: 1px solid #222; text-align:center; font-weight:bold; font-size:{row_font};">{pts}</td>
                        <td style="padding: {row_vpad} 6px; border: 1px solid #222; font-size:{desc_font}; line-height: 1.2;">{descriptor}</td>
                    </tr>
                    """

                avg_summary_percentage = total_evaluated_weight / total_subjects_count if total_subjects_count > 0 else 0.0
                summary_meta = evaluate_performance_metrics(avg_summary_percentage)

                # Compute baseline averages safely for graph generation
                op_avg = (opener_sum / op_count) if op_count > 0 else 0.0
                mid_avg = (midterm_sum / mid_count) if mid_count > 0 else 0.0
                end_avg = (endterm_sum / end_count) if end_count > 0 else 0.0

                report_logo_src = school.get('logo_url')
                if report_logo_src:
                    report_final_logo_src = report_logo_src if report_logo_src.startswith("http") else f"/{report_logo_src.lstrip('/')}"
                    logo_markup = f'<img src="{report_final_logo_src}" style="width:105px; height:105px; object-fit:contain; margin-right:16px;" />'
                else:
                    logo_markup = f'<div style="width:105px; height:105px; border:3px solid {theme["hex"]}; display:flex; align-items:center; justify-content:center; font-weight:bold; margin-right:16px; font-size:14px;">CREST</div>'

                exam_header_cells = (
                    ('<th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">Opener</th>' if show_opener else "") +
                    ('<th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">Midterm</th>' if show_midterm else "") +
                    ('<th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">End Term</th>' if show_endterm else "")
                )

                report_cards_html.append(f"""
                <div class="report-card-container" style="background: white; padding: 24px; border: 5px solid {theme['hex']}; border-radius: 12px; width: 199mm; min-height: 250mm; max-height: 282mm; overflow: hidden; box-sizing: border-box; margin: 0 auto; font-family: 'Plus Jakarta Sans', Arial, sans-serif; display: flex; flex-direction: column; justify-content: space-between;">
                    <div>
                        <div style="display: flex; align-items: center; border-bottom: 4px double {theme['hex']}; padding-bottom: 8px; margin-bottom: 12px;">
                            {logo_markup}
                            <div style="flex-grow:1; text-align:center;">
                                <h1 style="color:{theme['hex']}; font-size:28px; font-weight:900; margin:0 0 2px 0; text-transform:uppercase; letter-spacing:0.5px;">{esc(school['name'])}</h1>
                                <p style="margin:2px 0; font-size:12px; color:#222;"><b>Location Address:</b> {esc(school['physical_address'])} &nbsp;|&nbsp; <b>Sub-County:</b> {esc(school['sub_county'])}</p>
                                <div style="background:{theme['hex']}; color:white; font-weight:bold; font-size:13px; padding:4px; margin-top:6px; text-transform:uppercase; letter-spacing:1px; border-radius:4px;">Official {st['active_cycle']} Progress Analytics Report</div>
                            </div>
                        </div>

                        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:4px 12px; background:#f8fafc; border:1px solid #cbd5e1; padding:10px; border-radius:6px; font-size:11px; margin-bottom:12px; line-height:1.4;">
                            <div><b>Learner Name:</b> <span style="font-weight:bold; text-transform:uppercase;">{esc(full_student_name(s))}</span></div>
                            <div><b>Admission Identifier Number:</b> <span style="font-weight:bold;">{esc(s['admission_number'])}</span></div>
                            <div><b>Education Bracket:</b> {esc(grade_name)} ({esc(education_level)}) — Stream: <b>{esc(stream)}</b></div>
                            <div><b>KNEC Assessment Identifier (LAN):</b> {s['knec_lan'] or 'N/A'}</div>
                            <div><b>Calendar Timeline Context:</b> Year {st['active_year']} — {st['active_term']}</div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px;">
                            <div style="border: 1px dashed #475569; padding: 4px 8px; border-radius: 6px; background: #f8fafc; text-align: center;">
                                <span style="font-size: 10px; text-transform: uppercase; font-weight: bold; color: #64748b;">Position In Stream</span>
                                <div style="font-size: 15px; font-weight: 900; color: #1e3a8a; margin-top: 1px;">
                                    {s['stream_position']} <span style="font-size: 11px; font-weight: normal; color: #475569;">out of {s['total_in_stream']}</span>
                                </div>
                            </div>
                            <div style="border: 1px dashed #059669; padding: 4px 8px; border-radius: 6px; background: #f0fdf4; text-align: center;">
                                <span style="font-size: 10px; text-transform: uppercase; font-weight: bold; color: #059669;">Overall Position In Grade</span>
                                <div style="font-size: 15px; font-weight: 900; color: #065f46; margin-top: 1px;">
                                    {s['grade_position']} <span style="font-size: 11px; font-weight: normal; color: #475569;">out of {s['total_in_grade']}</span>
                                </div>
                            </div>
                        </div>

                        <table style="width:100%; border-collapse:collapse; font-size:11px;">
                            <thead>
                                <tr style="background:{theme['hex']}; color:white; text-transform:uppercase; font-size:10.5px;">
                                    <th style="padding:6px; border:1px solid #222; text-align:left;">CBE Learning Domain Area</th>
                                    {exam_header_cells}
                                    <th style="padding:6px; border:1px solid #222; width:90px; text-align:center;">Weighted Avg</th>
                                    <th style="padding:6px; border:1px solid #222; width:70px; text-align:center;">CBE Code</th>
                                    <th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">Scale Pts</th>
                                    <th style="padding:6px; border:1px solid #222; text-align:left;">Competence Descriptor Status</th>
                                </tr>
                            </thead>
                            <tbody>{rows_markup}</tbody>
                        </table>
                    </div>

                    <div style="margin-top: 18px;">
                        <div style="display: grid; grid-template-columns: 280px 1fr; gap: 16px; align-items: center; margin-bottom: 12px;">
                            <div style="border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px; background: #f8fafc; text-align: center;">
                                <span style="font-size: 11px; font-weight: bold; text-transform: uppercase; color: #475569; display: block; margin-bottom: 6px;">Performance Milestone Graph</span>
                                <svg viewBox="0 0 200 80" style="width: 100%; height: 92px; overflow: visible;">
                                    <line x1="20" y1="10" x2="190" y2="10" stroke="#e2e8f0" stroke-width="0.5" />
                                    <line x1="20" y1="35" x2="190" y2="35" stroke="#e2e8f0" stroke-width="0.5" />
                                    <line x1="20" y1="60" x2="190" y2="60" stroke="#cbd5e1" stroke-width="1" />
                                    
                                    <text x="5" y="13" font-size="7" fill="#64748b" font-family="sans-serif">100%</text>
                                    <text x="5" y="38" font-size="7" fill="#64748b" font-family="sans-serif">50%</text>
                                    <text x="8" y="63" font-size="7" fill="#64748b" font-family="sans-serif">0%</text>
                                    
                                    <path d="M 40 {60 - (op_avg * 0.5)} L 105 {60 - (mid_avg * 0.5)} L 170 {60 - (end_avg * 0.5)}" fill="none" stroke="{theme['hex']}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                                    
                                    <circle cx="40" cy="{60 - (op_avg * 0.5)}" r="3.5" fill="#0f172a" />
                                    <circle cx="105" cy="{60 - (mid_avg * 0.5)}" r="3.5" fill="#0f172a" />
                                    <circle cx="170" cy="{60 - (end_avg * 0.5)}" r="3.5" fill="#0f172a" />
                                    
                                    <text x="28" y="73" font-size="7.5" font-weight="bold" fill="#334155" font-family="sans-serif">Opener ({op_avg:.1f}%)</text>
                                    <text x="90" y="73" font-size="7.5" font-weight="bold" fill="#334155" font-family="sans-serif">Mid ({mid_avg:.1f}%)</text>
                                    <text x="155" y="73" font-size="7.5" font-weight="bold" fill="#334155" font-family="sans-serif">End ({end_avg:.1f}%)</text>
                                </svg>
                            </div>

                            <div style="border:1px solid {theme['hex']}; background:#f4faf6; padding:16px; border-radius:8px; display:flex; flex-direction:column; justify-content:center; gap:12px; height:110px; box-sizing:border-box;">
                                <div style="display:flex; justify-content:space-between; font-weight:bold; font-size:13px;">
                                    <span>Cumulative Scale Points:</span>
                                    <span style="color:{theme['hex']}; font-weight:800;">{accumulated_scale_points} Pts</span>
                                </div>
                                <div style="display:flex; justify-content:space-between; font-weight:bold; font-size:13px;">
                                    <span>Mean Performance Score:</span>
                                    <span style="font-weight:800;">{avg_summary_percentage:.1f}%</span>
                                </div>
                                <div style="display:flex; justify-content:space-between; font-weight:bold; font-size:13px;">
                                    <span>Aggregated Summary Level:</span>
                                    <span style="background:white; padding:2px 8px; border:1px solid #333; border-radius:4px; color:{theme['hex']}; font-weight:800; font-size:12px;">{summary_meta['pld']}</span>
                                </div>
                            </div>
                        </div>

                        <div style="border: 1px solid #cbd5e1; padding: 14px; border-radius: 8px; background: #fafafa; font-size: 12px; line-height: 1.6;">
                            <div style="padding-bottom:8px; margin-bottom:8px; border-bottom:1px dashed #e2e8f0;"><b>Class Instructor Remarks:</b> {esc(generate_teacher_comment(s['first_name'], summary_meta['pld']))}</div>
                            <div><b>Headteacher Institutional Verdict:</b> {esc(generate_headteacher_comment(summary_meta['pld']))}</div>
                        </div>

                        <div style="display:flex; justify-content:space-between; margin-top: 8px; padding-top: 6px; border-top: 1.5px solid #cbd5e1; font-size: 11px; font-style: italic; color: #475569;">
                            <div><b>Current Term Closing Date:</b> {st['closing_date']}</div>
                            <div><b>Next Term Opening Date:</b> {st['opening_date']}</div>
                        </div>
                    </div>
                </div>
                """)

            joined_report_pages = "\n".join(report_cards_html)
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}">
                <title>Elimu Hub | Print Out Queue Pipeline</title>
                <link rel="preconnect" href="https://fonts.googleapis.com">
                <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
                <style>
                    * {{ box-sizing: border-box; }}
                    html, body {{ margin: 0; padding: 0; width: 100%; font-family: 'Plus Jakarta Sans', Arial, sans-serif; }}
                    
                    .report-card-container {{
                        page-break-inside: avoid !important;
                    }}
                    
                    .report-card-container:not(:last-child) {{
                        page-break-after: always !important;
                    }}
                    
                    @media print {{
                        @page {{
                            size: A4 portrait;
                            margin: 0;
                        }}
                        .no-print {{ display: none !important; }}
                        body {{ background: white !important; padding: 0 !important; }}
                        .report-card-container {{
                            border-radius: 0 !important;
                            box-shadow: none !important;
                            padding: 20px 26px !important;
                            width: 199mm !important;
                            min-height: 267mm !important;
                            max-height: 282mm !important;
                            overflow: hidden !important;
                            margin: 6mm auto !important;
                            border-width: 6px !important;
                        }}
                    }}
                </style>
            </head>
            <body style="background:#64748b; padding:30px 20px; margin:0;">
                <div class="no-print" style="max-width:199mm; margin: 0 auto 20px auto; text-align:right;">
                    <button onclick="window.print()" style="background:#0f172a; color:white; border:none; padding:11px 22px; font-weight:bold; font-size:13px; border-radius:6px; cursor:pointer; box-shadow:0 3px 6px rgba(0,0,0,0.15);">🖨️ Commit Print Batch to Paper</button>
                </div>
                {missing_marks_banner}
                {joined_report_pages}
            </body>
            </html>
            """
@app.post("/api/v1/settings/update/{school_id}")
def update_settings_endpoint(
    school_id: int, 
    request: Request,
    active_term: str = Form(...), 
    active_cycle: str = Form(...), 
    opening_date: str = Form(...), 
    closing_date: str = Form(...), 
    theme_color: str = Form(...),
    is_single_stream: str = Form(None),
):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    # Constrain free-text-ish fields to known-good values so a crafted POST
    # can't smuggle unexpected data (defense in depth beyond output escaping).
    allowed_terms = {"Term 1", "Term 2", "Term 3"}
    allowed_cycles = {"Opener", "Midterm", "End Term"}
    allowed_themes = {"emerald", "blue", "indigo", "purple", "slate"}

    if active_term not in allowed_terms:
        raise HTTPException(status_code=400, detail="Invalid academic term selected.")
    if active_cycle not in allowed_cycles:
        raise HTTPException(status_code=400, detail="Invalid assessment cycle selected.")
    if theme_color not in allowed_themes:
        theme_color = "emerald"

    # A checkbox only appears in form data when it's checked — its absence
    # here correctly means "unchecked", not "leave unchanged".
    is_single_stream_bool = bool(is_single_stream)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO school_settings (school_id, active_term, active_cycle, opening_date, closing_date, is_single_stream)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id) DO UPDATE 
                SET active_term = EXCLUDED.active_term, 
                    active_cycle = EXCLUDED.active_cycle, 
                    opening_date = EXCLUDED.opening_date, 
                    closing_date = EXCLUDED.closing_date,
                    is_single_stream = EXCLUDED.is_single_stream;
            """, (school_id, active_term, active_cycle, opening_date, closing_date, is_single_stream_bool))
            
            # Sync the modern Tailwind color layout across the institution node
            cur.execute("UPDATE schools SET theme_color = %s WHERE id = %s;", (theme_color, school_id))
            conn.commit()
            log_audit_action(cur, request, school_id, "settings_updated", f"Term={active_term}, Cycle={active_cycle}, Theme={theme_color}, SingleStream={is_single_stream_bool}")
            conn.commit()
            
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.post("/api/v1/students/add/{school_id}")
def backend_add_student(
    school_id: int, 
    request: Request,
    first_name: str = Form(...), 
    middle_name: str = Form(""),
    last_name: str = Form(...), 
    admission_number: str = Form(...), 
    class_id: int = Form(...), 
    stream: str = Form(None)
):
    # Clean up the string input value
    raw_stream = stream.strip().upper() if stream else ""
    
    # If it's left blank, assign the standard "SINGLE STREAM" token flag
    if not raw_stream or raw_stream == "":
        processed_stream = "SINGLE STREAM"
    else:
        # If they wrote '2N' instead of just 'N', strip off the number part gracefully
        processed_stream = raw_stream.replace("GRADE", "").replace(str(class_id), "").strip()
        if not processed_stream:
            processed_stream = "SINGLE STREAM"

    admission_number = admission_number.strip().upper()
    first_name = first_name.strip()
    middle_name = middle_name.strip() or None
    last_name = last_name.strip()
    if not admission_number or not first_name or not last_name:
        raise HTTPException(status_code=400, detail="First name, surname, and admission number are required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # The `students` table requires education_level (NOT NULL); derive
            # it from the chosen class rather than leaving it unset, which
            # would otherwise raise an unhandled NOT NULL violation.
            cur.execute("SELECT education_level FROM classes WHERE id = %s;", (class_id,))
            class_row = cur.fetchone()
            if not class_row:
                raise HTTPException(status_code=400, detail="The selected grade/class does not exist.")
            education_level = class_row[0]

            try:
                cur.execute("""
                    INSERT INTO students (school_id, admission_number, first_name, middle_name, last_name, class_id, stream, education_level, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE');
                """, (school_id, admission_number, first_name, middle_name, last_name, class_id, processed_stream, education_level))
                conn.commit()
                log_audit_action(cur, request, school_id, "student_added", f"Registered {first_name} {last_name} (Adm #{admission_number})")
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                raise HTTPException(status_code=400, detail=f"A student with admission number '{admission_number}' already exists in this school. Please use a different admission number.")

    return RedirectResponse(url=with_query_param(get_dashboard_url(request, school_id), "student_added", "1"), status_code=303)


@app.get("/admin/school/profile/{school_id}", response_class=HTMLResponse)
def school_profile_view(school_id: int, request: Request, saved: str = None):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            admin_user_id = request.cookies.get("session_user_id")
            cur.execute("SELECT * FROM users WHERE id = %s AND school_id = %s AND role = 'admin';", (admin_user_id, school_id))
            admin = cur.fetchone()

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | School Profile</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-2xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">🏫 School Profile</h2>
                <p class="text-xs text-slate-400 mt-1">Edit your school's details and your own administrator account details.</p>
            </div>

            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl'>✅ Profile updated successfully.</div>" if saved else ""}

            <form action="/api/v1/school/profile/update/{school_id}" method="post" class="bg-white p-6 rounded-2xl border shadow-xs space-y-5">
                <div>
                    <h3 class="text-xs font-bold uppercase tracking-wider text-indigo-700 mb-3">School Details</h3>
                    <div class="space-y-3">
                        <div>
                            <label class="text-xs font-bold text-slate-600 block mb-1">School Name</label>
                            <input type="text" name="school_name" value="{esc(school['name'])}" class="w-full border p-2.5 rounded-xl text-sm" required>
                        </div>
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                                <label class="text-xs font-bold text-slate-600 block mb-1">Sub-County</label>
                                <input type="text" name="sub_county" value="{esc(school['sub_county'])}" class="w-full border p-2.5 rounded-xl text-sm" required>
                            </div>
                            <div>
                                <label class="text-xs font-bold text-slate-600 block mb-1">Physical Address</label>
                                <input type="text" name="physical_address" value="{esc(school['physical_address'])}" class="w-full border p-2.5 rounded-xl text-sm" required>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="pt-4 border-t">
                    <h3 class="text-xs font-bold uppercase tracking-wider text-violet-700 mb-3">Administrator Account</h3>
                    <div class="space-y-3">
                        <div>
                            <label class="text-xs font-bold text-slate-600 block mb-1">Full Name</label>
                            <input type="text" name="admin_full_name" value="{esc(admin['full_name'] or '') if admin else ''}" class="w-full border p-2.5 rounded-xl text-sm" required>
                        </div>
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                                <label class="text-xs font-bold text-slate-600 block mb-1">Email (used to log in)</label>
                                <input type="email" name="admin_email" value="{esc(admin['email']) if admin else ''}" class="w-full border p-2.5 rounded-xl text-sm" required>
                            </div>
                            <div>
                                <label class="text-xs font-bold text-slate-600 block mb-1">Phone Number</label>
                                <input type="text" name="admin_phone_number" value="{esc(admin['phone_number'] or '') if admin else ''}" class="w-full border p-2.5 rounded-xl text-sm">
                            </div>
                        </div>
                        <p class="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-2.5">⚠️ Changing your email changes what you log in with — make sure it's correct and that you'll remember it. Your password stays the same; use "Forgot your password?" on the login page if you ever need to reset it.</p>
                    </div>
                </div>

                <button type="submit" class="w-full bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-3 rounded-xl text-sm transition">Save Changes</button>
            </form>

            <a href="/admin/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Dashboard</a>
        </div>
    </body>
    </html>
    """


@app.post("/api/v1/school/profile/update/{school_id}")
def update_school_profile(
    school_id: int,
    request: Request,
    school_name: str = Form(...),
    sub_county: str = Form(...),
    physical_address: str = Form(...),
    admin_full_name: str = Form(...),
    admin_email: str = Form(...),
    admin_phone_number: str = Form(""),
):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    school_name = school_name.strip()
    sub_county = sub_county.strip()
    physical_address = physical_address.strip()
    admin_full_name = admin_full_name.strip()
    admin_email = admin_email.strip().lower()
    admin_phone_number = admin_phone_number.strip()

    if not school_name or not sub_county or not physical_address:
        raise HTTPException(status_code=400, detail="School name, sub-county, and address are all required.")
    if not admin_full_name or not admin_email:
        raise HTTPException(status_code=400, detail="Your full name and email are required.")

    admin_user_id = request.cookies.get("session_user_id")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE schools SET name = %s, sub_county = %s, physical_address = %s WHERE id = %s;
            """, (school_name, sub_county, physical_address, school_id))

            try:
                cur.execute("""
                    UPDATE users SET full_name = %s, email = %s, phone_number = %s
                    WHERE id = %s AND school_id = %s AND role = 'admin';
                """, (admin_full_name, admin_email, admin_phone_number, admin_user_id, school_id))
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                raise HTTPException(status_code=400, detail=f"Another account is already using the email '{admin_email}'. Please use a different email.")

    return RedirectResponse(url=f"/admin/school/profile/{school_id}?saved=1", status_code=303)


@app.get("/admin/student/edit/{school_id}/{student_id}", response_class=HTMLResponse)
def edit_student_view(school_id: int, student_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student not found.")

            cur.execute("SELECT id, grade_name FROM classes ORDER BY id ASC;")
            classes = cur.fetchall()

            cur.execute("SELECT is_single_stream FROM school_settings WHERE school_id = %s;", (school_id,))
            settings_row = cur.fetchone()
            is_single_stream = bool(settings_row['is_single_stream']) if settings_row else False

    grade_options = "".join(
        f"<option value='{c['id']}' {'selected' if c['id'] == student['class_id'] else ''}>{esc(c['grade_name'])}</option>"
        for c in classes
    )
    display_stream = "" if student['stream'] == "SINGLE STREAM" else student['stream']

    stream_field_html = (
        "<div class='sm:col-span-2 bg-slate-50 border border-slate-200 rounded p-2.5 text-xs text-slate-500'>ℹ️ This school is in <b>Single Stream Mode</b> — no stream assignment is needed.</div>"
        if is_single_stream else
        f"<div><label class=\"text-xs font-bold text-slate-600\">Class Stream Assignment</label><input type=\"text\" name=\"stream\" value=\"{esc(display_stream)}\" placeholder=\"e.g. N\" class=\"w-full border p-2.5 rounded mt-1 text-base\"></div>"
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><link rel="icon" href="{ELIMU_HUB_ICON_DATA_URI}"><title>Elimu Hub | Edit Student Record</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 flex items-center justify-center min-h-screen p-4">
        <div class="bg-white p-6 sm:p-8 rounded-2xl border shadow-md w-full max-w-lg">
            <h2 class="text-xl font-bold mb-4 text-slate-800">Edit Learner Profile</h2>
            <form action="/api/v1/students/edit/{school_id}/{student_id}" method="post" class="space-y-4">
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div><label class="text-xs font-bold text-slate-600">First Name</label><input type="text" name="first_name" value="{esc(student['first_name'])}" class="w-full border p-2.5 rounded mt-1 text-base" required></div>
                    <div><label class="text-xs font-bold text-slate-600">Middle Name</label><input type="text" name="middle_name" value="{esc(student.get('middle_name') or '')}" class="w-full border p-2.5 rounded mt-1 text-base"></div>
                    <div><label class="text-xs font-bold text-slate-600">Surname</label><input type="text" name="last_name" value="{esc(student['last_name'])}" class="w-full border p-2.5 rounded mt-1 text-base" required></div>
                </div>
                <div><label class="text-xs font-bold text-slate-600">Admission Number</label><input type="text" inputmode="numeric" name="admission_number" value="{esc(student['admission_number'])}" class="w-full border p-2.5 rounded mt-1 text-base" required></div>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs font-bold text-slate-600">Education Track Segment</label>
                        <select name="class_id" class="w-full border p-2.5 rounded mt-1 bg-white text-sm font-medium text-slate-800" required>{grade_options}</select>
                    </div>
                    {stream_field_html}
                </div>
                <div class="flex flex-col sm:flex-row gap-3 pt-2">
                    <button type="submit" class="bg-emerald-700 text-white font-bold py-3 px-4 rounded hover:bg-emerald-800 transition text-center">Save Changes</button>
                    <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 text-slate-700 py-3 px-4 rounded hover:bg-slate-300 font-bold transition text-center">Cancel</a>
                </div>
            </form>
            <form action="/api/v1/students/delete/{school_id}/{student_id}" method="post" class="mt-4 pt-4 border-t" onsubmit="return confirm('Permanently delete {esc(full_student_name(student))}? This also deletes all of their recorded scores. This cannot be undone.');">
                <button type="submit" class="w-full bg-rose-50 border border-rose-200 text-rose-700 font-bold py-2.5 px-4 rounded-lg hover:bg-rose-100 transition text-sm">🗑 Delete Student Permanently</button>
            </form>
        </div>
    </body>
    </html>
    """


@app.post("/api/v1/students/edit/{school_id}/{student_id}")
def backend_edit_student(
    school_id: int,
    student_id: int,
    request: Request,
    first_name: str = Form(...),
    middle_name: str = Form(""),
    last_name: str = Form(...),
    admission_number: str = Form(...),
    class_id: int = Form(...),
    stream: str = Form(None)
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    raw_stream = stream.strip().upper() if stream else ""
    if not raw_stream:
        processed_stream = "SINGLE STREAM"
    else:
        processed_stream = raw_stream.replace("GRADE", "").replace(str(class_id), "").strip()
        if not processed_stream:
            processed_stream = "SINGLE STREAM"

    admission_number = admission_number.strip().upper()
    first_name = first_name.strip()
    middle_name = middle_name.strip() or None
    last_name = last_name.strip()
    if not admission_number or not first_name or not last_name:
        raise HTTPException(status_code=400, detail="First name, surname, and admission number are required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Student not found.")

            cur.execute("SELECT education_level FROM classes WHERE id = %s;", (class_id,))
            class_row = cur.fetchone()
            if not class_row:
                raise HTTPException(status_code=400, detail="The selected grade/class does not exist.")
            education_level = class_row[0]

            try:
                cur.execute("""
                    UPDATE students
                    SET first_name = %s, middle_name = %s, last_name = %s, admission_number = %s,
                        class_id = %s, stream = %s, education_level = %s
                    WHERE id = %s AND school_id = %s;
                """, (first_name, middle_name, last_name, admission_number, class_id, processed_stream, education_level, student_id, school_id))
                conn.commit()
                log_audit_action(cur, request, school_id, "student_edited", f"Edited {first_name} {last_name} (Adm #{admission_number})")
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                raise HTTPException(status_code=400, detail="Another student in this school already has that admission number.")

    return RedirectResponse(url=get_dashboard_url(request, school_id), status_code=303)


@app.post("/api/v1/students/delete/{school_id}/{student_id}")
def backend_delete_student(school_id: int, student_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT first_name, last_name, admission_number FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            deleted_student = cur.fetchone()

            # Cascades to student_scores via the existing foreign key.
            cur.execute("DELETE FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            conn.commit()

            if deleted_student:
                log_audit_action(cur, request, school_id, "student_deleted", f"Deleted {deleted_student[0]} {deleted_student[1]} (Adm #{deleted_student[2]})")
                conn.commit()

    return RedirectResponse(url=get_dashboard_url(request, school_id), status_code=303)
@app.post("/api/v1/staff/add/{school_id}")
def add_staff_node(
    school_id: int,
    request: Request,
    full_name: str = Form(...),
    tsc_number: str = Form(...),
    phone_number: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    email = email.strip().lower()
    full_name = full_name.strip()
    tsc_number = tsc_number.strip()
    phone_number = phone_number.strip()
    if not email:
        raise HTTPException(status_code=400, detail="A staff email address is required.")
    if not full_name:
        raise HTTPException(status_code=400, detail="Staff full name is required.")
    if not tsc_number:
        raise HTTPException(status_code=400, detail="Staff TSC number is required.")
    if not phone_number:
        raise HTTPException(status_code=400, detail="Staff phone number is required.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")

    safe_staff_password = password[:72]
    hashed_password = get_password_hash(safe_staff_password)
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE email = %s;", (email,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="That email is already registered to an account.")
            cur.execute("""
                INSERT INTO users (email, password_hash, role, school_id, is_verified, full_name, tsc_number, phone_number)
                VALUES (%s, %s, 'staff', %s, FALSE, %s, %s, %s);
            """, (email, hashed_password, school_id, full_name, tsc_number, phone_number))
            conn.commit()
            log_audit_action(cur, request, school_id, "staff_added", f"Registered staff account for {full_name} ({email})")
            conn.commit()
    return RedirectResponse(url=f"/admin/dashboard/{school_id}?staff_added=1", status_code=303)

@app.post("/api/v1/staff/toggle-verification/{school_id}")
def toggle_staff_verification(school_id: int, request: Request, user_id: int = Form(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_verified = NOT is_verified WHERE id = %s AND school_id = %s AND role = 'staff';", (user_id, school_id))
            conn.commit()
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.post("/api/v1/scores/upsert/{school_id}")
def upsert_individual_score(school_id: int, student_id: int = Form(...), learning_area_id: int = Form(...), cycle_name: str = Form(...), raw_score: float = Form(...)):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=403, detail="Cross-tenant execution attempt blocked.")
            
            cur.execute("""
                INSERT INTO student_scores (student_id, learning_area_id, cycle_name, raw_score)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (student_id, learning_area_id, cycle_name) DO UPDATE SET raw_score = EXCLUDED.raw_score;
            """, (student_id, learning_area_id, cycle_name, raw_score))
            conn.commit()
    return RedirectResponse(url=f"/admin/scores/manage/{school_id}?student_id={student_id}", status_code=303)

@app.post("/api/v1/scores/delete/{school_id}")
def drop_individual_score(school_id: int, score_id: int = Form(...), student_id: int = Form(...)):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=403, detail="Cross-tenant execution attempt blocked.")
            cur.execute("DELETE FROM student_scores WHERE id = %s AND student_id = %s;", (score_id, student_id))
            conn.commit()
    return RedirectResponse(url=f"/admin/scores/manage/{school_id}?student_id={student_id}", status_code=303)

@app.post("/api/v1/scores/bulk-save/{school_id}")
async def batch_save_class_marks_matrix(school_id: int, request: Request):
    # Declared `async def` so we can await the form parse directly instead of
    # spinning up a nested event loop with asyncio.run() inside a sync route
    # (which is wasteful and can misbehave under some ASGI server setups).
    form_data = await request.form()

    try:
        learning_area_id = int(form_data.get('learning_area_id'))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid learning area must be selected before saving.")

    cycle_name = (form_data.get('cycle_name') or "").strip()
    if not cycle_name:
        raise HTTPException(status_code=400, detail="An assessment cycle must be selected before saving.")

    is_paper_mode = form_data.get("is_paper_mode") == "1"
    skipped_entries = 0

    if is_paper_mode:
        try:
            paper1_max = float(form_data.get("paper1_max") or 0)
            paper2_max = float(form_data.get("paper2_max") or 0)
        except ValueError:
            raise HTTPException(status_code=400, detail="Paper 1/Paper 2 'out of' values must be numbers.")
        if paper1_max <= 0 or paper2_max <= 0:
            raise HTTPException(status_code=400, detail="Paper 1 and Paper 2 'out of' values must be greater than zero.")

        # Collect each student's two paper marks first, since both fields
        # need to be present together to compute a combined percentage.
        student_papers = {}
        for key, val in form_data.items():
            if not key.startswith("paper1_") and not key.startswith("paper2_"):
                continue
            try:
                sid = int(key.split("_")[1])
                mark = float(val) if str(val).strip() != "" else None
            except (IndexError, ValueError):
                continue
            entry = student_papers.setdefault(sid, {})
            entry["paper1" if key.startswith("paper1_") else "paper2"] = mark

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for sid, marks in student_papers.items():
                    p1, p2 = marks.get("paper1"), marks.get("paper2")
                    if p1 is None and p2 is None:
                        continue  # nothing entered for this student this session

                    if p1 is not None and not (0 <= p1 <= paper1_max):
                        skipped_entries += 1
                        continue
                    if p2 is not None and not (0 <= p2 <= paper2_max):
                        skipped_entries += 1
                        continue

                    cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (sid, school_id))
                    if not cur.fetchone():
                        skipped_entries += 1
                        continue

                    # A school can give just one paper for now and add the
                    # other later — whichever paper(s) are actually present
                    # determine the percentage, so a lone Paper 1 mark is
                    # scored out of Paper 1's own max, not held back waiting
                    # for a Paper 2 that may not exist yet. Once Paper 2 is
                    # entered later (editing this same subject/cycle again),
                    # this recomputes using both, exactly as a true combined
                    # score should.
                    if p1 is not None and p2 is not None:
                        percentage = round((p1 + p2) / (paper1_max + paper2_max) * 100, 2)
                    elif p1 is not None:
                        percentage = round(p1 / paper1_max * 100, 2)
                    else:
                        percentage = round(p2 / paper2_max * 100, 2)

                    cur.execute("""
                        INSERT INTO paper_based_scores (student_id, learning_area_id, cycle_name, paper1_marks, paper1_max, paper2_marks, paper2_max)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (student_id, learning_area_id, cycle_name)
                        DO UPDATE SET paper1_marks = EXCLUDED.paper1_marks, paper1_max = EXCLUDED.paper1_max,
                                      paper2_marks = EXCLUDED.paper2_marks, paper2_max = EXCLUDED.paper2_max;
                    """, (sid, learning_area_id, cycle_name, p1, paper1_max, p2, paper2_max))

                    # The resulting percentage is written into student_scores
                    # exactly like any normal single-mark entry — report
                    # cards, rankings, and every other report keep reading
                    # this the same way, with no idea one or two papers were involved.
                    cur.execute("""
                        INSERT INTO student_scores (student_id, learning_area_id, cycle_name, raw_score)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (student_id, learning_area_id, cycle_name) DO UPDATE SET raw_score = EXCLUDED.raw_score;
                    """, (sid, learning_area_id, cycle_name, percentage))
                conn.commit()

        if skipped_entries:
            logger.warning(f"Bulk paper-score save for school {school_id} skipped {skipped_entries} invalid/mismatched entries.")

        saved_count = len([1 for m in student_papers.values() if m.get("paper1") is not None or m.get("paper2") is not None]) - skipped_entries

        redirect_params = urllib.parse.urlencode({
            "grade_name": form_data.get("grade_name", ""),
            "education_level": form_data.get("education_level", ""),
            "stream": form_data.get("stream", ""),
            "learning_area_id": form_data.get("learning_area_id", ""),
            "cycle_name": cycle_name,
            "saved": saved_count,
            "skipped": skipped_entries,
        })
        return RedirectResponse(url=f"/staff/bulk-entry/{school_id}?{redirect_params}", status_code=303)

    skipped_entries = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, val in form_data.items():
                if not (key.startswith("score_") and str(val).strip() != ""):
                    continue

                # Guard against malformed field names or non-numeric scores so
                # one bad row doesn't abort the whole batch commit.
                try:
                    target_student_id = int(key.split("_")[1])
                    raw_score = float(val)
                except (IndexError, ValueError):
                    skipped_entries += 1
                    continue

                if not (0 <= raw_score <= 100):
                    skipped_entries += 1
                    continue

                cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (target_student_id, school_id))
                if cur.fetchone():
                    cur.execute("""
                        INSERT INTO student_scores (student_id, learning_area_id, cycle_name, raw_score)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (student_id, learning_area_id, cycle_name) DO UPDATE SET raw_score = EXCLUDED.raw_score;
                    """, (target_student_id, learning_area_id, cycle_name, raw_score))
                else:
                    skipped_entries += 1
            conn.commit()

    if skipped_entries:
        logger.warning(f"Bulk score save for school {school_id} skipped {skipped_entries} invalid/mismatched entries.")

    saved_count = sum(
        1 for key, val in form_data.items()
        if key.startswith("score_") and str(val).strip() != ""
    ) - skipped_entries

    if saved_count > 0:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                log_audit_action(cur, request, school_id, "marks_saved", f"Saved {saved_count} score(s) — {form_data.get('grade_name', '')} {form_data.get('stream', '')}, cycle {cycle_name}")
                conn.commit()

    redirect_params = urllib.parse.urlencode({
        "grade_name": form_data.get("grade_name", ""),
        "education_level": form_data.get("education_level", ""),
        "stream": form_data.get("stream", ""),
        "learning_area_id": form_data.get("learning_area_id", ""),
        "cycle_name": cycle_name,
        "saved": saved_count,
        "skipped": skipped_entries,
    })
    return RedirectResponse(url=f"/staff/bulk-entry/{school_id}?{redirect_params}", status_code=303)

@app.post("/api/v1/wallet/stkpush/{school_id}")
def process_simulated_mpesa_stk_push(school_id: int, request: Request, phone_number: str = Form(...), amount: float = Form(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    phone_number = phone_number.strip()
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Top-up amount must be greater than zero.")
    if not re.fullmatch(r"[0-9+\s]{7,15}", phone_number):
        raise HTTPException(status_code=400, detail="Please provide a valid phone number.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE schools SET wallet_balance = wallet_balance + %s WHERE id = %s;", (amount, school_id))
            conn.commit()

    # Encode the user-supplied phone number as a JSON string literal (not just
    # HTML-escaped) before splicing it into inline JavaScript, since it sits
    # inside a `<script>` block where HTML-escaping alone would not stop
    # someone from breaking out of the quoted string.
    import json as _json
    safe_phone_js = _json.dumps(phone_number)
    return HTMLResponse(f"""
    <script>
        alert('STK Push Triggered to ' + {safe_phone_js} + ' successfully! Mock transaction completed.');
        window.location.href='/admin/dashboard/{school_id}';
    </script>
    """)

@app.post("/api/v1/school/promote-classes/{school_id}")
def promote_school_classes(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    # Map current class_id to the next consecutive class_id
    # 1: Grade 1 -> 2: Grade 2, ..., 9: Grade 9
    promotion_map = {
        1: 2,
        2: 3,
        3: 4,
        4: 5,
        5: 6,
        6: 7,
        7: 8,
        8: 9
    }
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Step 1: Safely graduate Grade 9 out of the active pool
            cur.execute("""
                UPDATE students 
                SET class_id = NULL, status = 'GRADUATED' 
                WHERE school_id = %s AND class_id = 9;
            """, (school_id,))
            
            # Step 2: Update remaining cohorts starting from the top down
            for current_class, next_class in sorted(promotion_map.items(), reverse=True):
                cur.execute("""
                    UPDATE students 
                    SET class_id = %s 
                    WHERE school_id = %s AND class_id = %s 
                      AND (status IS NULL OR status != 'GRADUATED');
                """, (next_class, school_id, current_class))
                
            conn.commit()
            
    # Redirect cleanly back to the administrative control panel
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)
