"""
shared.py — dependency-free helpers used across the app.

This module exists specifically to avoid a circular import between
main.py and timetable_routes.py: both import from here, neither imports
from the other. Anything added here should stay self-contained (only
stdlib / third-party imports, no dependency on main.py's globals).
"""

import os
import html
import bcrypt
import psycopg2
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

# Used throughout the HTML-rendering routes to escape user-supplied text
# before splicing it into inline HTML (prevents XSS).
esc = html.escape

# Moved here from main.py so a new route module (e.g.
# student_portal_routes.py) can hash/verify a password without importing
# from main.py directly — that would create exactly the circular import
# this file exists to avoid. Behavior is unchanged; every existing call
# site in main.py now imports these from here instead of defining them
# locally.
def get_password_hash(password: str) -> str:
    """Hashes a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a password against a stored hash."""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


# Moved here from main.py for the same reason as get_password_hash above —
# student_portal_routes.py needs these too (to let a class teacher, not
# just an admin, manage their own students' parent contacts), and
# importing from main.py directly would create a circular import.
def _normalize_stream_for_match(stream) -> str:
    """Treats 'SINGLE STREAM' (the placeholder this app uses throughout
    its URLs/forms for a class with no real sub-streams) as equivalent to
    a blank/NULL stream value — teacher_subject_assignments and
    class_teachers always store a literal stream string, while
    students.stream is often genuinely blank/NULL for a school with no
    real sub-streams. Comparing these directly without normalizing is
    exactly the bug class that broke scheme-of-work visibility earlier in
    this project; every access-control check here goes through this."""
    return None if (not stream or not stream.strip() or stream.strip().upper() == "SINGLE STREAM") else stream.strip()


def get_teacher_class_keys(cur, school_id: int, user_id: int) -> set:
    """Every (grade_name, education_level, normalized_stream) this staff
    member is connected to — either as the assigned class (homeroom)
    teacher, or as a subject teacher with at least one teaching
    assignment there (from the timetable module's own teacher_subject_
    assignments table — the single source of truth for "who teaches
    what", reused here rather than building a second one). Only call
    this to restrict role == 'staff' — admins and super admins are never
    restricted by this."""
    cur.execute("SELECT grade_name, education_level, stream FROM class_teachers WHERE school_id = %s AND teacher_user_id = %s;", (school_id, user_id))
    rows = list(cur.fetchall())
    cur.execute("SELECT DISTINCT grade_name, education_level, stream FROM teacher_subject_assignments WHERE school_id = %s AND staff_user_id = %s;", (school_id, user_id))
    rows += list(cur.fetchall())
    return {(r['grade_name'], r['education_level'], _normalize_stream_for_match(r['stream'])) for r in rows}


def teacher_can_access_class(class_keys: set, grade_name: str, education_level: str, stream: str) -> bool:
    """Checks a specific class against a teacher's allowed set, built by
    get_teacher_class_keys."""
    return (grade_name, education_level, _normalize_stream_for_match(stream)) in class_keys


def is_teacher_of_this_class(cur, school_id: int, user_id: int, grade_name: str, education_level: str, stream: str) -> bool:
    """Is this staff member the assigned class (homeroom) teacher for this
    exact class? A class teacher gets full access to every subject for
    their own class — unlike a subject-only teacher, who's restricted to
    just what they're assigned to teach, and (in student_portal_routes.py)
    is NOT allowed to edit a student's parent contact details — that's
    kept to the homeroom teacher and admins only, not every subject
    teacher who happens to teach a class once a week."""
    target_stream = _normalize_stream_for_match(stream)
    cur.execute("SELECT stream FROM class_teachers WHERE school_id = %s AND teacher_user_id = %s AND grade_name = %s AND education_level = %s;", (school_id, user_id, grade_name, education_level))
    return any(_normalize_stream_for_match(r['stream']) == target_stream for r in cur.fetchall())

# --- Database Setup ---
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in Render environment variables.")

# Connection pool — reuses a small set of open connections instead of
# opening/closing a new one on every request. Uses ThreadedConnectionPool
# (not SimpleConnectionPool) because FastAPI's sync routes run across
# multiple worker threads. Sized generously for 4-8 schools; adjust via
# env vars if you scale well beyond that.
#
# Created lazily (on first actual use) rather than at import time — so if
# Neon happens to be unreachable at the exact moment the app starts up, the
# app still boots, and the pool just gets created on the first request that
# needs it.
_db_pool = None

def _get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2_pool.ThreadedConnectionPool(
            int(os.getenv("DB_POOL_MIN_CONN", "2")),
            int(os.getenv("DB_POOL_MAX_CONN", "20")),
            dsn=DATABASE_URL,
        )
    return _db_pool

@contextmanager
def get_db_connection():
    pool = _get_pool()
    conn = None
    last_err = None

    # Actively verify the connection is alive before handing it to a route.
    # A dead-but-not-marked-closed connection is the actual cause of the
    # "SSL SYSCALL error: EOF detected" / "bad record mac" crashes — Neon can
    # silently kill an idle connection at the network layer without psycopg2
    # noticing until the next real query runs. A cheap SELECT 1 here catches
    # that upfront and swaps in a fresh connection instead of crashing the
    # actual request.
    #
    # Retries bumped from 3 to 5, and the catch widened to also include
    # InterfaceError: after Neon suspends an idle compute, EVERY connection
    # the pool is currently holding can go stale at once, not just one —
    # with a pool as small as DB_POOL_MIN_CONN=2, 3 attempts isn't
    # guaranteed to cycle through all of them before a genuinely fresh one
    # gets created. InterfaceError ("connection already closed") is a
    # second, separate exception psycopg2 can raise for a dead connection,
    # depending on exactly when the network drop is noticed — a ping that
    # only caught OperationalError could still hand out a connection that
    # raises InterfaceError instead.
    for _ in range(5):
        candidate = pool.getconn()
        if candidate.closed:
            try:
                pool.putconn(candidate, close=True)
            except psycopg2_pool.PoolError:
                pass
            continue
        try:
            with candidate.cursor() as ping_cur:
                ping_cur.execute("SELECT 1;")
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as ping_err:
            last_err = ping_err
            try:
                pool.putconn(candidate, close=True)
            except psycopg2_pool.PoolError:
                pass
            continue
        conn = candidate
        break

    if conn is None:
        raise last_err or psycopg2.OperationalError("Could not obtain a healthy database connection.")

    try:
        yield conn
    except Exception:
        # If something went wrong mid-transaction, roll back before the
        # connection goes back in the pool so the next borrower doesn't
        # inherit a half-finished transaction. Guard against the connection
        # itself having died during the request (rollback would then raise).
        try:
            if not conn.closed:
                conn.rollback()
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            pass
        raise
    finally:
        # Never return a dead connection to the pool — that just hands the
        # same crash to the next request. Discard it so the pool opens a
        # fresh one next time it's needed.
        try:
            if conn.closed:
                pool.putconn(conn, close=True)
            else:
                pool.putconn(conn)
        except psycopg2_pool.PoolError:
            pass


def get_current_session_user(request: Request):
    """Looks up the actual logged-in user's role and school_id from the
    database via session_user_id, rather than trusting the session_role /
    session_school_id cookie values directly. Cookies (even httponly ones)
    can be edited client-side via browser dev tools, so authorization
    decisions must never trust their contents — only use session_user_id
    as a lookup key. Returns a dict {id, role, school_id, is_verified} or
    None if there's no valid session."""
    user_id = request.cookies.get("session_user_id")
    if not user_id:
        return None
    try:
        user_id = int(user_id)
    except ValueError:
        return None
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, role, school_id, is_verified FROM users WHERE id = %s;", (user_id,))
            return cur.fetchone()

def require_school_session(request: Request, school_id: int):
    """Confirms the request belongs to a real, currently valid account tied
    to this school. Returns a redirect Response if unauthenticated, or None
    if OK to proceed."""
    user = get_current_session_user(request)
    if not user:
        return RedirectResponse(url="/login?error=Authentication+required.", status_code=303)
    if user['role'] != 'superadmin' and str(user['school_id']) != str(school_id):
        raise HTTPException(
            status_code=403,
            detail="Access Denied: You do not have privileges for this institution."
        )
    return None

def require_admin_session(request: Request, school_id: int):
    """Like require_school_session, but also blocks staff-role accounts —
    verified from the database, not from a client-supplied cookie value."""
    user = get_current_session_user(request)
    if not user:
        return RedirectResponse(url="/login?error=Authentication+required.", status_code=303)
    if user['role'] != 'superadmin' and str(user['school_id']) != str(school_id):
        raise HTTPException(
            status_code=403,
            detail="Access Denied: You do not have privileges for this institution."
        )
    if user['role'] == 'staff':
        raise HTTPException(
            status_code=403,
            detail="Access Denied: Administrator privileges required for this action."
        )
    return None

def with_query_param(base_url: str, key: str, value: str) -> str:
    """Appends a query param to a URL, correctly using '?' or '&' depending
    on whether the URL (e.g. from get_dashboard_url) already has one."""
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{key}={value}"

def get_dashboard_url(request: Request, school_id: int) -> str:
    """Returns the correct 'home' dashboard URL for whoever is logged in —
    staff go back to their own portal, not the admin-only dashboard."""
    user = get_current_session_user(request)
    if user and user['role'] == 'staff':
        return f"/staff/dashboard/{school_id}?user_id={user['id']}"
    return f"/admin/dashboard/{school_id}"

def require_superadmin_session(request: Request):
    """Gates the super admin portal — not tied to any single school_id.
    Verified from the database, not from a client-supplied cookie value."""
    user = get_current_session_user(request)
    if not user or user['role'] != 'superadmin':
        return RedirectResponse(url="/login?error=Authentication+required.", status_code=303)
    return None


# --- Subject ordering / display helpers ---
SUBJECT_DISPLAY_ORDER = {
    'Junior School': ['English', 'Kiswahili', 'Mathematics', 'Integrated science.', 'Creative arts and sports.', 'Social studies', 'Christian religious education', 'Agriculture', 'pretechnical studies.'],
    'Upper Primary': ['English', 'Kiswahili', 'Mathematics', 'Integrated science.', 'Creative arts and sports.', 'Social studies', 'Christian religious education', 'Agriculture'],
    'Lower Primary': ['ENGLISH', 'LUGHA', 'MATHEMATICS', 'INTEGRATED SCIENCE'],
}

SUBJECT_ABBREVIATIONS = {
    'mathematics': 'MAT', 'english': 'ENG', 'kiswahili': 'KIS', 'lugha': 'LUGHA',
    'integrated science.': 'INT/SC', 'integrated science': 'INT/SC',
    'creative arts and sports.': 'CAS', 'creative arts and sports': 'CAS',
    'social studies': 'SST', 'christian religious education': 'C.R.E',
    'agriculture': 'AGRI', 'pretechnical studies.': 'PRE TECH', 'pretechnical studies': 'PRE TECH',
}

def full_student_name(student_row) -> str:
    """Builds 'First Middle Surname' from a student row, correctly handling
    students with no middle name on file (no double-space, no 'None')."""
    parts = [student_row.get('first_name'), student_row.get('middle_name'), student_row.get('last_name')]
    return " ".join(p for p in parts if p)

def sort_subjects_for_display(subjects, education_level):
    """Orders a list of {'id', 'name'} learning-area rows using the canonical
    subject sequence for that education level, so report columns read in a
    familiar order instead of raw alphabetical/DB order."""
    order = SUBJECT_DISPLAY_ORDER.get(education_level, [])
    order_index = {name.lower(): i for i, name in enumerate(order)}
    return sorted(subjects, key=lambda s: (order_index.get(s['name'].lower(), len(order)), s['name']))

def abbreviate_subject(name: str) -> str:
    key = name.strip().lower()
    return SUBJECT_ABBREVIATIONS.get(key, (name[:3].upper() if len(name) >= 3 else name.upper()))
